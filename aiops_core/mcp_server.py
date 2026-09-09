"""``aiops-mcp`` stdio MCP Server：把 aiops_core 的决策能力挂给 Codex / Trae。

设计原则（与 PR-3 硬约束一致）：
- **只暴露决策类工具**：Skill 匹配 / 计划渲染 / Runbook 抽取 / 指纹 / 报告渲染。
  证据采集类（HTTP 探测 / 日志查询 / Prom 拉数）由宿主 Agent 自己用 shell + 自带工具
  完成，避免我们重复实现且引入网络依赖。
- **stdio 传输**：Codex / Trae / Claude Desktop 三大主流客户端都主推 stdio；HTTP MCP
  只有 ``[server]`` extra 才有意义。
- **输入 / 输出仅使用 JSON-Serializable 结构**：不返回 pydantic 模型或 dataclass，
  确保跨语言宿主消费无痛。
- **失败如实报告**：Skill 不命中不 fake，Runbook 缺失原样返回空 list；LLM 由宿主决定。

启动方式：
    aiops-mcp                    # stdio，供 Codex / Trae 配置
    python -m aiops_core.mcp_server   # 同上

需要 ``[mcp]`` extra（``fastmcp``）。缺失时抛 :class:`aiops_core._optional.OptionalDependencyMissing`。
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from aiops_core._optional import OptionalDependencyMissing, require_module
from aiops_core.planner.from_skill import plan_from_skill
from aiops_core.report.renderer import render_diagnosis_report
from aiops_core.skills import get_skill_registry
from aiops_core.skills.runbook_retriever import get_runbook_retriever
from aiops_core.triggers.fingerprint import alert_fingerprint
from aiops_core.triggers.normalize import extract_alerts, normalize_alert

_SERVER_NAME = "aiops-copilot"


def _first_alert(alert_payload: dict[str, Any]) -> dict[str, Any]:
    """Alertmanager webhook 常见地包一层 ``alerts: [...]``；本函数负责摊平。"""
    alerts = extract_alerts(alert_payload) or [alert_payload]
    return alerts[0]


def _skill_match_impl(
    alert_payload: dict[str, Any],
    top_k: int = 3,
) -> dict[str, Any]:
    """核心业务逻辑：对一个 Alertmanager 载荷做 top-k Skill 匹配。

    抽成独立函数以便：
    1. FastMCP 装饰包一层调这个；
    2. 单元测试无需拉起 stdio server，可直接调；
    3. 后续 CLI（PR-4）同样复用。
    """
    alert = _first_alert(alert_payload)
    normalized = normalize_alert(alert)
    query = (normalized["title"] + " " + normalized["description"]).strip()
    matches = get_skill_registry().match(
        query=query,
        alert_labels=normalized["labels"],
        k=int(top_k),
    )
    return {
        "query": query,
        "labels": normalized["labels"],
        "matches": [
            {
                "skill_id": m.skill.skill_id,
                "title": m.skill.title,
                "domain": m.skill.domain,
                "score": m.score,
                "reasons": list(m.reasons),
                "runbook_refs": list(m.skill.runbook_refs),
                "required_tools": list(m.skill.required_tools),
                "disabled_reason": m.skill.disabled_reason,
            }
            for m in matches
        ],
    }


def _skill_plan_impl(
    skill_id: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按指定 skill_id 渲染可执行 PlanStep DAG（不走 LLM）。"""
    pack = get_skill_registry().get(skill_id)
    if pack is None:
        return {
            "skill_id": skill_id,
            "error": "skill_id 不存在",
            "steps": [],
        }
    return plan_from_skill(pack, context=context or {})


def _runbook_lookup_impl(
    doc_ids: list[str],
    query: str,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """从 ``aiops-docs/`` 抽取相关 h2 小节，供宿主 Agent 作为 Prompt 上下文。"""
    if not doc_ids or not query:
        return []
    retriever = get_runbook_retriever()
    sections = retriever.extract_relevant(
        doc_ids=list(doc_ids),
        query=str(query),
        top_k=int(top_k),
    )
    return [
        {
            "doc_id": s.doc_id,
            "doc_title": s.doc_title,
            "section_title": s.section_title,
            "content": s.content,
        }
        for s in sections
    ]


def _alert_fingerprint_impl(alert_payload: dict[str, Any]) -> dict[str, Any]:
    """按旧版 Blake2b 逻辑生成告警指纹；用于外部去重 / 归并 / 关联事件。"""
    alert = _first_alert(alert_payload)
    return {
        "fingerprint": alert_fingerprint(alert),
        "normalized": normalize_alert(alert),
    }


def _render_report_impl(
    skill_match: dict[str, Any] | None = None,
    plan: list[dict[str, Any]] | None = None,
    evidence_by_step: dict[str, Any] | None = None,
    verifications: list[dict[str, Any]] | None = None,
    trigger: dict[str, Any] | None = None,
    runbook_hits: list[dict[str, Any]] | None = None,
) -> str:
    """渲染最终 markdown 诊断报告。所有入参可选，缺失章节按占位处理。"""
    return render_diagnosis_report(
        skill_match=skill_match,
        plan=plan,
        evidence_by_step=evidence_by_step,
        verifications=verifications,
        trigger=trigger,
        runbook_hits=runbook_hits,
    )


def build_server() -> Any:
    """构造 FastMCP 实例并注册 5 个决策工具。

    延迟 import ``fastmcp`` 以让 ``import aiops_core.mcp_server`` 在纯净 venv 下也不
    立刻抛错——只有在真正启动 server / 构造工具集时才要求 ``[mcp]`` extra。
    """
    fastmcp_mod = require_module("fastmcp", extras="mcp")
    fast_mcp_cls = getattr(fastmcp_mod, "FastMCP", None)
    if fast_mcp_cls is None:  # pragma: no cover - fastmcp 版本不兼容才会走到
        raise OptionalDependencyMissing(
            "已安装的 fastmcp 版本不兼容：缺少 `FastMCP` 符号。"
            "\n    pip install 'aiops-copilot[mcp]' --upgrade"
        )

    mcp = fast_mcp_cls(_SERVER_NAME)

    @mcp.tool()
    def skill_match(alert_payload: dict[str, Any], top_k: int = 3) -> dict[str, Any]:
        """按告警载荷返回 top_k 命中的 Skill（含 score / reasons / runbook_refs）。

        Args:
            alert_payload: Alertmanager 风格 JSON；可以是单条 alert 或 ``{"alerts": [...]}``。
            top_k: 保留前几条命中，默认 3。
        """
        return _skill_match_impl(alert_payload, top_k=top_k)

    @mcp.tool()
    def skill_plan(
        skill_id: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按 skill_id 生成可执行 PlanStep DAG（不走 LLM）。

        Args:
            skill_id: :func:`skill_match` 返回的 ``skill_id``。
            context: 参数模板渲染上下文（如 ``{"labels": {...}}``），可选。
        """
        return _skill_plan_impl(skill_id, context=context)

    @mcp.tool()
    def runbook_lookup(
        doc_ids: list[str],
        query: str,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        """从 ``aiops-docs/`` 抽取与 query 最相关的 h2 小节，供作 Prompt 上下文。

        Args:
            doc_ids: 需要查阅的 wiki 文档 id 列表（对应 ``aiops-docs/<doc_id>.md``）。
            query: 检索关键词，通常是 skill.title / step.description。
            top_k: 返回前几段，默认 3。
        """
        return _runbook_lookup_impl(doc_ids, query, top_k=top_k)

    @mcp.tool()
    def alert_fingerprint_tool(alert_payload: dict[str, Any]) -> dict[str, Any]:
        """按旧版 Blake2b 逻辑生成告警指纹，用于外部去重 / 归并。

        Args:
            alert_payload: Alertmanager 载荷（单条或含 ``alerts`` 列表）。
        """
        return _alert_fingerprint_impl(alert_payload)

    @mcp.tool()
    def render_report(
        skill_match: dict[str, Any] | None = None,
        plan: list[dict[str, Any]] | None = None,
        evidence_by_step: dict[str, Any] | None = None,
        verifications: list[dict[str, Any]] | None = None,
        trigger: dict[str, Any] | None = None,
        runbook_hits: list[dict[str, Any]] | None = None,
    ) -> str:
        """把 Skill 匹配 + 计划 + 证据 + Runbook 命中渲染成 markdown 诊断报告。

        所有入参可选：缺失章节以占位说明呈现，避免宿主为不同场景写多份 prompt。
        """
        return _render_report_impl(
            skill_match=skill_match,
            plan=plan,
            evidence_by_step=evidence_by_step,
            verifications=verifications,
            trigger=trigger,
            runbook_hits=runbook_hits,
        )

    return mcp


def main(argv: list[str] | None = None) -> int:
    """``aiops-mcp`` 的进程入口；默认以 stdio 方式挂给宿主。

    这里只暴露一个 ``--transport`` 参数用于本地调试；stdio 之外的传输仅在
    ``[server]`` extra 下有意义（HTTP MCP 靠 FastAPI 才有价值）。
    """
    parser = argparse.ArgumentParser(prog="aiops-mcp")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "http"],
        help="传输协议；默认 stdio（Codex/Trae 挂载用），http 仅调试用。",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="仅 http 模式生效。",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8090,
        help="仅 http 模式生效。",
    )
    args = parser.parse_args(argv)

    server = build_server()
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="http", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
