"""当前任务的 Skill 上下文（re-export from ``aiops_core.skills.context``）。"""

from aiops_core.skills.context import (  # noqa: F401
    SkillTaskContext,
    get_current_skill_context,
    reset_current_skill_context,
    set_current_skill_context,
    use_skill_context,
)

__all__ = [
    "SkillTaskContext",
    "get_current_skill_context",
    "reset_current_skill_context",
    "set_current_skill_context",
    "use_skill_context",
]
