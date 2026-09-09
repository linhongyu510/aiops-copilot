"""从匹配到的 Skill 生成可执行 PlanStep DAG（不走 LLM）。

给 stdio MCP `skill_plan` 工具与 CLI `plan` 命令共享。
"""

from __future__ import annotations

from typing import Any

from aiops_core.skills.models import SkillPack
from aiops_core.skills.registry import SkillMatch


def plan_from_skill(
    skill: SkillPack | SkillMatch,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 Skill.steps 转成执行计划视图。

    Args:
        skill: SkillPack 或 SkillMatch。
        context: 参数模板渲染上下文（可含 alert labels、业务上下文），传入时会
            对 ``tool_args_template`` 做一次预渲染便于展示；不传则原样保留占位符。
    """
    if isinstance(skill, SkillMatch):
        pack = skill.skill
        match_score = skill.score
        match_reasons = list(skill.reasons)
    else:
        pack = skill
        match_score = None
        match_reasons = []

    steps = pack.to_plan_step_dicts()
    if context is not None:
        from aiops_core.skills.renderer import render_args

        for step in steps:
            template = step.get("tool_args_template")
            if template:
                step["tool_args_rendered"] = render_args(template, context)

    return {
        "skill_id": pack.skill_id,
        "title": pack.title,
        "version": pack.version,
        "domain": pack.domain,
        "runbook_refs": list(pack.runbook_refs),
        "match_score": match_score,
        "match_reasons": match_reasons,
        "steps": steps,
        "verifications": [
            {
                "tool": v.tool,
                "args": dict(v.args),
                "success_expr": v.success_expr,
                "description": v.description,
            }
            for v in pack.verifications
        ],
    }
