"""aiops_core：零依赖的 OnCall 内核。

设计目标：
- 不 import ``langchain*`` / ``fastapi`` / ``langgraph`` / ``pymilvus``；
- 仅依赖 ``pydantic`` / ``loguru`` / ``pyyaml`` / 标准库；
- 可被 stdio MCP server 与本地 CLI 共享；也被完整版 app/ 层通过 re-export
  兼容层继续使用，避免历史 import 路径断链。

顶层入口：
- :func:`aiops_core.skills.get_skill_registry` / :class:`SkillPack`
- :func:`aiops_core.triggers.alert_fingerprint` / :func:`normalize_alert`
- :func:`aiops_core.planner.plan_from_skill` / :func:`normalize_plan`
- :func:`aiops_core.evidence.aggregate_evidence` / :class:`EvidenceItem`
- :func:`aiops_core.report.render_diagnosis_report`
"""

from aiops_core._optional import OptionalDependencyMissing, has_module, require_module
from aiops_core.evidence import EvidenceItem, aggregate_evidence, normalize_evidence
from aiops_core.planner import (
    normalize_plan,
    plan_from_skill,
    ready_batch,
    step_description,
    step_id_of,
)
from aiops_core.report import render_diagnosis_report
from aiops_core.skills import (
    SkillMatch,
    SkillPack,
    SkillPrecondition,
    SkillRegistry,
    SkillStep,
    SkillTaskContext,
    SkillTrigger,
    SkillVerification,
    build_render_context,
    evaluate_success_expr,
    get_skill_registry,
    render_args,
    render_string,
)
from aiops_core.triggers import alert_fingerprint, extract_alerts, normalize_alert


def report_environment() -> dict[str, bool]:
    """打印各 extras 是否可用；给 stdio MCP / CLI 快速自检用。"""
    checks = {
        "llm": "langchain_openai",
        "server": "fastapi",
        "rag": "pymilvus",
        "state": "psycopg",
        "obs": "opentelemetry",
        "mcp": "fastmcp",
    }
    return {name: has_module(module) for name, module in checks.items()}

__all__ = [
    # skills
    "SkillMatch",
    "SkillPack",
    "SkillPrecondition",
    "SkillRegistry",
    "SkillStep",
    "SkillTaskContext",
    "SkillTrigger",
    "SkillVerification",
    "build_render_context",
    "evaluate_success_expr",
    "get_skill_registry",
    "render_args",
    "render_string",
    # triggers
    "alert_fingerprint",
    "extract_alerts",
    "normalize_alert",
    # planner
    "normalize_plan",
    "plan_from_skill",
    "ready_batch",
    "step_description",
    "step_id_of",
    # evidence
    "EvidenceItem",
    "aggregate_evidence",
    "normalize_evidence",
    # report
    "render_diagnosis_report",
]
