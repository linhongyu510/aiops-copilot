"""Skill Pack 抽象：把领域预案/关键词/参数模板收敛为可执行单元。

Skill 是运行时单元（含触发条件/前置条件/计划模板/参数占位/验证清单），
Playbook 是历史文本资产；命名保持区分，不再互换使用。

内核实现已抽到 :mod:`aiops_core.skills`，本包只做 re-export，保持既有
``from app.agent.skills import ...`` 语义不断链。
"""

from app.agent.skills import registry as _registry_module
from app.agent.skills.context import (
    SkillTaskContext,
    get_current_skill_context,
    reset_current_skill_context,
    set_current_skill_context,
    use_skill_context,
)
from app.agent.skills.models import (
    SkillPack,
    SkillPrecondition,
    SkillStep,
    SkillTrigger,
    SkillVerification,
)
from app.agent.skills.registry import SkillMatch, SkillRegistry
from app.agent.skills.renderer import build_render_context, render_args, render_string
from app.agent.skills.verifier import evaluate_success_expr


def get_skill_registry() -> SkillRegistry:
    """代理到 ``app.agent.skills.registry.get_skill_registry``；tests 用
    ``monkeypatch.setattr(reg_module, 'get_skill_registry', ...)`` 会通过此代理生效。
    """
    return _registry_module.get_skill_registry()


__all__ = [
    "SkillPack",
    "SkillTrigger",
    "SkillPrecondition",
    "SkillStep",
    "SkillVerification",
    "SkillMatch",
    "SkillRegistry",
    "get_skill_registry",
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
