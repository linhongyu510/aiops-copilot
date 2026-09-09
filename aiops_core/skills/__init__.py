"""Skill 抽象包（零依赖版本）。"""

from aiops_core.skills.context import (
    SkillTaskContext,
    get_current_skill_context,
    reset_current_skill_context,
    set_current_skill_context,
    use_skill_context,
)
from aiops_core.skills.models import (
    SkillPack,
    SkillPrecondition,
    SkillStep,
    SkillTrigger,
    SkillVerification,
)
from aiops_core.skills.registry import (
    SkillMatch,
    SkillRegistry,
    get_skill_registry,
    register_legacy_playbook_provider,
    register_tools_catalog_provider,
    skill_registry,
)
from aiops_core.skills.renderer import (
    build_render_context,
    render_args,
    render_string,
)
from aiops_core.skills.verifier import evaluate_success_expr

__all__ = [
    "SkillPack",
    "SkillTrigger",
    "SkillPrecondition",
    "SkillStep",
    "SkillVerification",
    "SkillMatch",
    "SkillRegistry",
    "get_skill_registry",
    "register_legacy_playbook_provider",
    "register_tools_catalog_provider",
    "skill_registry",
    "render_args",
    "render_string",
    "build_render_context",
    "evaluate_success_expr",
    "SkillTaskContext",
    "get_current_skill_context",
    "set_current_skill_context",
    "reset_current_skill_context",
    "use_skill_context",
]
