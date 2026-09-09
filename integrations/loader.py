"""Opt-in loading of optional integrations.

The core agent never imports an integration directly. A deployment lists the
ones it wants in ``AIOPS_ENABLED_INTEGRATIONS`` (comma-separated); everything
else stays unloaded, so a default clone has no dependency on any external
platform.

Unknown names are reported rather than silently ignored — a typo in the env var
should be visible, not turn into a missing tool later.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from loguru import logger

ENV_VAR = "AIOPS_ENABLED_INTEGRATIONS"

# name -> importable module path. Add an entry when contributing an integration.
AVAILABLE_INTEGRATIONS: dict[str, str] = {
    "windos": "integrations.windos",
}


def enabled_integration_names() -> tuple[str, ...]:
    """Return the integration names requested via the environment."""
    raw = os.getenv(ENV_VAR, "")
    names: list[str] = []
    for chunk in raw.split(","):
        name = chunk.strip().lower()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _load_module(name: str) -> Any | None:
    module_path = AVAILABLE_INTEGRATIONS.get(name)
    if module_path is None:
        logger.warning(
            f"未知集成 {name!r}（{ENV_VAR}），已跳过；"
            f"可用集成：{sorted(AVAILABLE_INTEGRATIONS) or '无'}"
        )
        return None
    try:
        from importlib import import_module

        return import_module(module_path)
    except Exception as exc:  # noqa: BLE001 - a broken integration must not stop startup
        logger.warning(f"集成 {name!r} 加载失败，已跳过：{type(exc).__name__}: {exc}")
        return None


def load_enabled_integrations() -> tuple[Any, ...]:
    """Import every enabled integration and register its tool metadata."""
    modules: list[Any] = []
    for name in enabled_integration_names():
        module = _load_module(name)
        if module is None:
            continue
        register: Callable[[], None] | None = getattr(module, "register_tool_specs", None)
        if callable(register):
            register()
        modules.append(module)
    if modules:
        logger.info(f"已启用集成：{[getattr(m, 'INTEGRATION_NAME', '?') for m in modules]}")
    return tuple(modules)


def register_enabled_mcp_tools(mcp: Any) -> tuple[str, ...]:
    """Attach the MCP tools of every enabled integration to ``mcp``."""
    registered: list[str] = []
    for module in load_enabled_integrations():
        attach: Callable[[Any], tuple[str, ...]] | None = getattr(
            module, "register_mcp_tools", None
        )
        if not callable(attach):
            continue
        try:
            registered.extend(attach(mcp))
        except Exception as exc:  # noqa: BLE001 - keep the server usable
            name = getattr(module, "INTEGRATION_NAME", "?")
            logger.warning(f"集成 {name!r} 注册 MCP 工具失败：{type(exc).__name__}: {exc}")
    return tuple(registered)


def extra_tool_groups() -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Collect routing hints contributed by enabled integrations."""
    groups: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for module in load_enabled_integrations():
        groups.extend(getattr(module, "TOOL_GROUPS", ()))
    return tuple(groups)
