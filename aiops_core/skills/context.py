"""当前任务的 Skill 上下文（零依赖版本）。

从 ``app.agent.skills.context`` 抽出，字面复制。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class SkillTaskContext:
    """当前任务归属的 Skill / 步骤 / 事件。所有字段可选。"""

    skill_id: str = ""
    skill_step_id: str = ""
    incident_id: str = ""


_EMPTY = SkillTaskContext()

_current_skill_ctx: ContextVar[SkillTaskContext] = ContextVar(
    "aiops_current_skill_ctx", default=_EMPTY
)


def get_current_skill_context() -> SkillTaskContext:
    return _current_skill_ctx.get()


def set_current_skill_context(ctx: SkillTaskContext):
    return _current_skill_ctx.set(ctx)


def reset_current_skill_context(token) -> None:
    if token is not None:
        _current_skill_ctx.reset(token)


@contextmanager
def use_skill_context(
    *,
    skill_id: str = "",
    skill_step_id: str = "",
    incident_id: str = "",
    merge: bool = True,
):
    """临时写入 skill 上下文。"""
    if merge:
        current = _current_skill_ctx.get()
        ctx = SkillTaskContext(
            skill_id=skill_id or current.skill_id,
            skill_step_id=skill_step_id or current.skill_step_id,
            incident_id=incident_id or current.incident_id,
        )
    else:
        ctx = SkillTaskContext(
            skill_id=skill_id, skill_step_id=skill_step_id, incident_id=incident_id
        )
    token = _current_skill_ctx.set(ctx)
    try:
        yield ctx
    finally:
        _current_skill_ctx.reset(token)
