"""Skill Registry（re-export from ``aiops_core.skills.registry``）。

历史 import 路径 ``from app.agent.skills.registry import ...`` 保持可用；单例
``skill_registry`` 与 ``get_skill_registry`` 均代理到内核，避免同一进程内出现
两份不同步的 registry 状态。

在首次实例化前，此模块会把 legacy playbook 与 tool_registry.catalog 探针注入
到内核，保持旧行为：
- ``BUILTIN_DEMO_PLAYBOOKS`` 作为兜底 Skill；
- ``required_tools`` 用 ``app.agent.tool_registry.tool_registry.catalog`` 校验。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiops_core.skills import registry as _core_registry
from aiops_core.skills.registry import (
    SkillMatch,
    SkillMetric,
    SkillRegistry as _CoreSkillRegistry,
)


def _legacy_playbook_provider() -> list[dict[str, Any]]:
    try:
        from app.services.playbook_service import BUILTIN_DEMO_PLAYBOOKS

        return list(BUILTIN_DEMO_PLAYBOOKS)
    except Exception:
        return []


def _tools_catalog_provider() -> set[str]:
    from app.agent.tool_registry import tool_registry as _tool_registry

    return set(_tool_registry.catalog.keys())


class SkillRegistry(_CoreSkillRegistry):
    """兼容 app 层旧构造方式，并把工具目录探针注入纯内核实现。"""

    # 旧测试/扩展会从 ``SkillRegistry.__dict__`` 读取该方法的 globals。
    _load_internal = _CoreSkillRegistry._load_internal

    def __init__(
        self,
        skills_dir: str | Path | None = None,
        include_legacy_playbooks: bool = True,
        *,
        tools_catalog_provider=None,
        legacy_playbooks_provider=None,
        default_top_k: int | None = None,
    ) -> None:
        super().__init__(
            skills_dir=skills_dir,
            include_legacy_playbooks=include_legacy_playbooks,
            tools_catalog_provider=tools_catalog_provider or _tools_catalog_provider,
            legacy_playbooks_provider=legacy_playbooks_provider
            or _legacy_playbook_provider,
            default_top_k=default_top_k,
        )


_core_registry.register_legacy_playbook_provider(_legacy_playbook_provider)
_core_registry.register_tools_catalog_provider(_tools_catalog_provider)

# 兼容旧测试和旧扩展点；正常运行时始终与内核单例指向同一对象。
_default_registry: _CoreSkillRegistry | None = None


def get_skill_registry() -> _CoreSkillRegistry:
    """获取 Skill Registry 单例；首次访问按 app 层配置装配 skills_dir。"""
    global _default_registry
    if _default_registry is not None:
        return _default_registry
    if _core_registry.skill_registry is None:
        from app.config import config

        candidate: str | None = getattr(config, "aiops_skills_dir", "") or None
        if not candidate:
            default_dir = Path(__file__).resolve().parents[3] / "skills" / "aiops"
            if default_dir.exists():
                candidate = str(default_dir)
        _core_registry.skill_registry = SkillRegistry(
            skills_dir=candidate,
            tools_catalog_provider=_tools_catalog_provider,
            legacy_playbooks_provider=_legacy_playbook_provider,
        )
    _default_registry = _core_registry.skill_registry
    return _default_registry


# 兼容旧代码：`from app.agent.skills.registry import skill_registry` 与
# `reg_module.skill_registry = X` 都需要指向同一个内核变量。用 module-level
# __getattr__ / __setattr__ 反射到内核变量以保持"单一真值源"。


def __getattr__(name: str) -> Any:
    if name == "skill_registry":
        return _core_registry.skill_registry
    raise AttributeError(name)


__all__ = [
    "SkillMatch",
    "SkillMetric",
    "SkillRegistry",
    "get_skill_registry",
]
