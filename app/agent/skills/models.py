"""Skill Pack 数据模型（re-export from ``aiops_core.skills.models``）。

内核已抽到 :mod:`aiops_core.skills.models`；本模块只做 re-export，保持既有
``from app.agent.skills.models import ...`` 语义不断链。**不要在此新增字段**，
直接修改 ``aiops_core.skills.models``。
"""

from aiops_core.skills.models import (  # noqa: F401
    SkillPack,
    SkillPrecondition,
    SkillStep,
    SkillTrigger,
    SkillVerification,
)

__all__ = [
    "SkillPack",
    "SkillPrecondition",
    "SkillStep",
    "SkillTrigger",
    "SkillVerification",
]
