"""把 (skill_match, plan, evidence, verifications) 渲染成 markdown 诊断报告。

零依赖、纯函数、确定性输出：便于 CI 回放与人工审阅。
"""

from __future__ import annotations

from typing import Any

from aiops_core.evidence.reader import EvidenceItem, aggregate_evidence


def render_diagnosis_report(
    *,
    skill_match: dict[str, Any] | None = None,
    plan: list[dict[str, Any]] | None = None,
    evidence_by_step: dict[str, Any] | None = None,
    verifications: list[dict[str, Any]] | None = None,
    trigger: dict[str, Any] | None = None,
    runbook_hits: list[dict[str, Any]] | None = None,
) -> str:
    """按固定章节渲染 markdown 诊断报告。

    所有入参均可选；缺失章节会以"（无）"或占位说明呈现，避免调用方为不同
    形态写多个渲染路径。
    """
    lines: list[str] = ["# 诊断报告"]

    lines.append("")
    lines.append("## 触发")
    lines.append(_render_trigger(trigger))

    lines.append("")
    lines.append("## 匹配到的 Skill")
    lines.append(_render_skill_match(skill_match))

    lines.append("")
    lines.append("## 执行计划")
    lines.append(_render_plan(plan))

    lines.append("")
    lines.append("## 证据摘要")
    lines.append(_render_evidence(evidence_by_step, plan))

    lines.append("")
    lines.append("## 验收清单")
    lines.append(_render_verifications(verifications))

    lines.append("")
    lines.append("## Runbook 参考")
    lines.append(_render_runbook_hits(runbook_hits, skill_match))

    return "\n".join(lines).rstrip() + "\n"


def _render_trigger(trigger: dict[str, Any] | None) -> str:
    if not trigger:
        return "_未提供触发信息_"
    labels = trigger.get("labels") or {}
    parts = [
        f"- **标题**：{trigger.get('title') or '(未命名)'}",
    ]
    if trigger.get("description"):
        parts.append(f"- **描述**：{trigger['description']}")
    if labels:
        rendered = ", ".join(f"`{k}={v}`" for k, v in sorted(labels.items()))
        parts.append(f"- **Labels**：{rendered}")
    if trigger.get("starts_at"):
        parts.append(f"- **开始时间**：{trigger['starts_at']}")
    fingerprint = trigger.get("fingerprint")
    if fingerprint:
        parts.append(f"- **指纹**：`{fingerprint}`")
    return "\n".join(parts)


def _render_skill_match(skill_match: dict[str, Any] | None) -> str:
    if not skill_match:
        return "_未命中任何 Skill；Planner 将走 LLM 兜底（当前 CLI/MCP 模式下不会执行）_"
    parts = [
        f"- **skill_id**：`{skill_match.get('skill_id', '?')}`",
        f"- **标题**：{skill_match.get('title', '(未命名)')}",
    ]
    score = skill_match.get("match_score")
    if score is not None:
        parts.append(f"- **匹配度**：{score}")
    reasons = skill_match.get("match_reasons") or []
    if reasons:
        parts.append("- **命中理由**：" + "；".join(str(r) for r in reasons))
    return "\n".join(parts)


def _render_plan(plan: list[dict[str, Any]] | None) -> str:
    if not plan:
        return "_无计划步骤_"
    lines: list[str] = []
    for index, step in enumerate(plan, start=1):
        step_id = step.get("step_id", f"s{index}")
        desc = step.get("description", "(空描述)")
        tool = step.get("tool_hint") or "(未指定)"
        deps = step.get("depends_on") or []
        deps_str = ", ".join(str(d) for d in deps) if deps else "无"
        lines.append(f"{index}. **[{step_id}]** {desc}")
        lines.append(f"   - 建议工具：`{tool}`")
        lines.append(f"   - 依赖：{deps_str}")
        template = step.get("tool_args_rendered") or step.get("tool_args_template")
        if template:
            lines.append(f"   - 参数：`{template}`")
    return "\n".join(lines)


def _render_evidence(
    evidence_by_step: dict[str, Any] | None,
    plan: list[dict[str, Any]] | None,
) -> str:
    items: list[EvidenceItem] = aggregate_evidence(evidence_by_step or {})
    if not items:
        return "_未采集到证据（宿主 Agent 未回传或 CLI 采用 --evidence none）_"
    step_desc_map: dict[str, str] = {}
    for step in plan or []:
        step_id = step.get("step_id")
        if step_id:
            step_desc_map[str(step_id)] = str(step.get("description") or "")

    lines: list[str] = []
    for item in items:
        desc = step_desc_map.get(item.step_id, "")
        header = f"### [{item.step_id}]"
        if desc:
            header += f" {desc}"
        lines.append(header)
        if item.tool:
            lines.append(f"- 工具：`{item.tool}`")
        lines.append(f"- 状态：`{item.status}`")
        if item.error:
            lines.append(f"- 错误：{item.error}")
        summary = item.summary or "(空)"
        lines.append("- 摘要：")
        lines.append("")
        lines.append("  ```")
        for text_line in summary.splitlines() or [summary]:
            lines.append(f"  {text_line}")
        lines.append("  ```")
    return "\n".join(lines)


def _render_verifications(verifications: list[dict[str, Any]] | None) -> str:
    if not verifications:
        return "_无验收步骤_"
    lines: list[str] = []
    for index, item in enumerate(verifications, start=1):
        tool = item.get("tool") or "(未指定)"
        expr = item.get("success_expr") or ""
        desc = item.get("description") or ""
        line = f"{index}. `{tool}` → `{expr}`"
        if desc:
            line += f"  ——  {desc}"
        lines.append(line)
    return "\n".join(lines)


def _render_runbook_hits(
    runbook_hits: list[dict[str, Any]] | None,
    skill_match: dict[str, Any] | None,
) -> str:
    if runbook_hits:
        lines: list[str] = []
        for hit in runbook_hits:
            doc_id = hit.get("doc_id", "")
            section = hit.get("section_title", "")
            content = (hit.get("content") or "").strip()
            lines.append(f"### {doc_id} / {section}")
            if content:
                for text_line in content.splitlines():
                    lines.append(f"> {text_line}")
        return "\n".join(lines)
    refs = (skill_match or {}).get("runbook_refs") or []
    if refs:
        return "本次未抽取具体片段，参见：\n" + "\n".join(f"- `aiops-docs/{r}.md`" for r in refs)
    return "_Skill 未挂载 runbook_refs_"
