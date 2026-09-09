"""``aiops-cli``：一次性本地 OnCall 入口，无需常驻服务。

PR-4 引入。设计原则：
- **零外部依赖即可跑通主链路**：所有子命令仅依赖内核已启用的必装依赖
  （``pydantic`` / ``loguru``）+ Python 标准库；
- **可脚本化 / CI 化**：`aiops-cli oncall alert.json --evidence file evidence.json`
  可以在纯净 CI 里跑；
- **三种证据源**：
  - ``--evidence none``（默认）：不采证据，只输出 Skill / Plan / Runbook 提示；
  - ``--evidence file <path>``：从 JSON 回放证据（``{step_id: evidence}``）；
  - ``--evidence local``：调用 ``aiops_core.evidence.local_backends`` 里的零依赖后端
    （urllib + subprocess + 本地文件），足以覆盖 http_probe / read_file / shell 三类。
- **LLM 完全可选**：CLI 主链路不 import ``openai`` / ``langchain``。宿主用什么模型是
  宿主的自由；若确需，用户显式 ``--llm`` flag 才 lazy import ``[llm]`` extra。

子命令一览：
    aiops-cli match       <alert.json> [--top-k 3]
    aiops-cli plan        <alert.json> [--skill-id ...] [--top-k 1]
    aiops-cli runbook     <doc_id ...> --query "..." [--top-k 3]
    aiops-cli fingerprint <alert.json>
    aiops-cli oncall      <alert.json> [--evidence none|file|local]
                                        [--evidence-file path.json]
                                        [--skill-id ...] [--top-k 1] [--runbook-top-k 3]
                                        [--output report.md]
    aiops-cli env
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from aiops_core.evidence.reader import EvidenceItem, normalize_evidence
from aiops_core.planner.from_skill import plan_from_skill
from aiops_core.report.renderer import render_diagnosis_report
from aiops_core.skills import get_skill_registry
from aiops_core.skills.runbook_retriever import get_runbook_retriever
from aiops_core.triggers.fingerprint import alert_fingerprint
from aiops_core.triggers.normalize import extract_alerts, normalize_alert

# --------------------------- 内部工具 ---------------------------


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print_json(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _first_alert(payload: Any) -> dict[str, Any]:
    """Alertmanager Webhook 常见地包一层 ``{"alerts": [...]}``；本函数负责摊平。"""
    if not isinstance(payload, dict):
        return {"labels": {}, "annotations": {}}
    alerts = extract_alerts(payload) or [payload]
    return alerts[0]


def _match_top(
    alert: dict[str, Any],
    *,
    k: int = 1,
) -> tuple[list[Any], dict[str, Any]]:
    normalized = normalize_alert(alert)
    query = (normalized["title"] + " " + normalized["description"]).strip()
    matches = get_skill_registry().match(
        query=query,
        alert_labels=normalized["labels"],
        k=int(k),
    )
    return matches, normalized


# --------------------------- 证据源 ---------------------------


def _collect_evidence(
    plan_steps: list[dict[str, Any]],
    *,
    mode: str,
    evidence_file: str,
) -> dict[str, Any]:
    """按 ``mode`` 采证据；返回 ``{step_id: EvidenceItem.to_dict()}``。"""
    if mode == "none":
        return {}
    if mode == "file":
        if not evidence_file:
            raise SystemExit("--evidence file 需要同时指定 --evidence-file <path>")
        raw = _load_json(evidence_file)
        if not isinstance(raw, dict):
            raise SystemExit(f"证据文件 {evidence_file} 必须是 {{step_id: ...}} 的 JSON dict")
        result: dict[str, Any] = {}
        for step in plan_steps:
            step_id = str(step.get("step_id"))
            if step_id in raw:
                result[step_id] = normalize_evidence(step_id, raw[step_id]).to_dict()
        # 允许证据文件包含计划外的 step_id（宿主 Agent 可能补齐了额外证据）
        for step_id, item in raw.items():
            if step_id not in result:
                result[str(step_id)] = normalize_evidence(str(step_id), item).to_dict()
        return result
    if mode == "local":
        from aiops_core.evidence.local_backends import collect_local_evidence

        result_local: dict[str, Any] = {}
        for step in plan_steps:
            item: EvidenceItem = collect_local_evidence(step)
            result_local[item.step_id] = item.to_dict()
        return result_local
    raise SystemExit(f"未知 --evidence 模式：{mode}")


# --------------------------- 子命令 ---------------------------


def _cmd_match(args: argparse.Namespace) -> int:
    payload = _load_json(args.alert)
    alert = _first_alert(payload)
    matches, normalized = _match_top(alert, k=args.top_k)
    _print_json(
        {
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
    )
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    payload = _load_json(args.alert)
    alert = _first_alert(payload)
    matches, normalized = _match_top(alert, k=max(args.top_k, 1))
    registry = get_skill_registry()
    if args.skill_id:
        pack = registry.get(args.skill_id)
        if pack is None:
            print(f"skill_id 不存在: {args.skill_id}", file=sys.stderr)
            return 2
        plan = plan_from_skill(pack, context={"labels": normalized["labels"]})
    else:
        if not matches:
            print("未匹配到任何 Skill", file=sys.stderr)
            return 3
        plan = plan_from_skill(matches[0], context={"labels": normalized["labels"]})
    _print_json(plan)
    return 0


def _cmd_runbook(args: argparse.Namespace) -> int:
    retriever = get_runbook_retriever()
    sections = retriever.extract_relevant(
        doc_ids=args.doc_ids,
        query=args.query,
        top_k=args.top_k,
    )
    _print_json(
        [
            {
                "doc_id": s.doc_id,
                "doc_title": s.doc_title,
                "section_title": s.section_title,
                "content": s.content,
            }
            for s in sections
        ]
    )
    return 0


def _cmd_fingerprint(args: argparse.Namespace) -> int:
    payload = _load_json(args.alert)
    alert = _first_alert(payload)
    print(alert_fingerprint(alert))
    return 0


def _cmd_oncall(args: argparse.Namespace) -> int:
    payload = _load_json(args.alert)
    alert = _first_alert(payload)
    matches, normalized = _match_top(alert, k=1)

    registry = get_skill_registry()
    plan_dict: dict[str, Any] | None = None
    if args.skill_id:
        pack = registry.get(args.skill_id)
        if pack is None:
            print(f"skill_id 不存在: {args.skill_id}", file=sys.stderr)
            return 2
        plan_dict = plan_from_skill(pack, context={"labels": normalized["labels"]})
    elif matches:
        plan_dict = plan_from_skill(matches[0], context={"labels": normalized["labels"]})

    plan_steps: list[dict[str, Any]] = (plan_dict or {}).get("steps") or []

    # Runbook 命中：优先使用 skill 的 runbook_refs，其次退化
    runbook_hits: list[dict[str, Any]] = []
    if plan_dict:
        refs = plan_dict.get("runbook_refs") or []
        if refs:
            retriever = get_runbook_retriever()
            sections = retriever.extract_relevant(
                doc_ids=list(refs),
                query=plan_dict.get("title", "") or normalized["title"],
                top_k=args.runbook_top_k,
            )
            runbook_hits = [
                {
                    "doc_id": s.doc_id,
                    "doc_title": s.doc_title,
                    "section_title": s.section_title,
                    "content": s.content,
                }
                for s in sections
            ]

    evidence_by_step = _collect_evidence(
        plan_steps,
        mode=args.evidence,
        evidence_file=args.evidence_file,
    )

    trigger_view = {
        "title": normalized["title"],
        "description": normalized["description"],
        "labels": normalized["labels"],
        "starts_at": normalized["starts_at"],
        "fingerprint": alert_fingerprint(alert),
    }
    report = render_diagnosis_report(
        skill_match=plan_dict,
        plan=plan_steps,
        evidence_by_step=evidence_by_step,
        verifications=(plan_dict or {}).get("verifications"),
        trigger=trigger_view,
        runbook_hits=runbook_hits,
    )

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"报告已写入：{args.output}", file=sys.stderr)
    else:
        sys.stdout.write(report)
    return 0


def _cmd_env(_: argparse.Namespace) -> int:
    from aiops_core import report_environment

    env = report_environment()
    _print_json(env)
    return 0


# --------------------------- 入口 ---------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aiops-cli",
        description="一次性本地 OnCall 入口；默认零外部依赖。",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_match = sub.add_parser("match", help="按告警 JSON 匹配 top-k Skill")
    p_match.add_argument("alert", help="Alertmanager 告警 JSON 文件路径")
    p_match.add_argument("--top-k", type=int, default=3)
    p_match.set_defaults(func=_cmd_match)

    p_plan = sub.add_parser("plan", help="按告警或指定 skill_id 生成 PlanStep DAG")
    p_plan.add_argument("alert", help="Alertmanager 告警 JSON 文件路径")
    p_plan.add_argument("--skill-id", default="", help="指定 skill_id，跳过匹配")
    p_plan.add_argument("--top-k", type=int, default=1)
    p_plan.set_defaults(func=_cmd_plan)

    p_runbook = sub.add_parser("runbook", help="从 aiops-docs/ 抽取 h2 片段")
    p_runbook.add_argument("doc_ids", nargs="+", help="doc_id 列表（对应 aiops-docs/<id>.md）")
    p_runbook.add_argument("--query", required=True)
    p_runbook.add_argument("--top-k", type=int, default=3)
    p_runbook.set_defaults(func=_cmd_runbook)

    p_fp = sub.add_parser("fingerprint", help="按旧版逻辑生成告警指纹")
    p_fp.add_argument("alert", help="Alertmanager 告警 JSON 文件路径")
    p_fp.set_defaults(func=_cmd_fingerprint)

    p_oncall = sub.add_parser(
        "oncall",
        help="一站式：匹配 Skill → 拉 Runbook → 采证据 → 渲染 markdown 诊断报告",
    )
    p_oncall.add_argument("alert", help="Alertmanager 告警 JSON 文件路径")
    p_oncall.add_argument("--skill-id", default="", help="指定 skill_id，跳过匹配")
    p_oncall.add_argument(
        "--evidence",
        choices=["none", "file", "local"],
        default="none",
        help="证据来源：none 默认；file 从 JSON 回放；local 用零依赖本地后端采集",
    )
    p_oncall.add_argument(
        "--evidence-file",
        default="",
        help="`--evidence file` 模式下的证据 JSON 路径（{step_id: raw}）",
    )
    p_oncall.add_argument("--top-k", type=int, default=1)
    p_oncall.add_argument(
        "--runbook-top-k",
        type=int,
        default=3,
        help="Runbook 片段抽取的 top_k",
    )
    p_oncall.add_argument(
        "--output",
        default="",
        help="报告输出文件路径；缺省时写 stdout",
    )
    p_oncall.set_defaults(func=_cmd_oncall)

    p_env = sub.add_parser(
        "env",
        help="打印各 extras 是否可用（用于诊断 aiops-copilot[...] 安装状态）",
    )
    p_env.set_defaults(func=_cmd_env)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
