"""Deterministic capability routing that keeps large MCP catalogs usable.

P0.1 起路由的单一实现在 app/agent/tool_registry.py（工具注册中心）：
- 领域关键词命中 → 确定性路由（与历史行为一致）；
- 未命中 → 字符 n-gram TF-IDF 语义泛化，让「服务响应变慢」这类
  不含领域关键词的问法也能命中相关工具。

本模块保留旧符号（select_relevant_tools / DEFAULT_TOOL_PREFIXES /
dynamic_tool_router / MAX_EXPOSED_TOOLS）作为兼容入口，调用方无需改动。

需要 ``[llm]`` extra（``langchain`` / ``langchain-core``）。缺失时抛
:class:`aiops_core._optional.OptionalDependencyMissing`。
"""

from __future__ import annotations

from typing import Any

try:
    from langchain.agents.middleware import ModelRequest, wrap_model_call
    from langchain_core.messages import HumanMessage
except ImportError as _exc:  # pragma: no cover - depends on install profile
    from aiops_core._optional import OptionalDependencyMissing

    raise OptionalDependencyMissing(
        "app.agent.tool_router 需要 `[llm]` extra（langchain/langchain-core）。"
        "\n    pip install 'aiops-copilot[llm]'"
    ) from _exc

from app.agent.tool_registry import (
    DEFAULT_TOOL_PREFIXES,
    TOOL_CATALOG,
    ToolSpec,
    tool_registry,
)

MAX_EXPOSED_TOOLS = 8

# 兼容导出：旧代码/测试通过 dict 视图访问工具元数据（category/keywords/read_only）。
TOOL_METADATA: dict[str, dict[str, Any]] = {
    name: {
        "category": spec.category,
        "keywords": spec.keywords,
        "read_only": spec.read_only,
    }
    for name, spec in TOOL_CATALOG.items()
}


def select_relevant_tools(
    question: str,
    tools: list[Any],
    limit: int = MAX_EXPOSED_TOOLS,
) -> list[Any]:
    """Select tools by capability while preserving discovery order and hard limit."""
    return tool_registry.select(question, tools, limit)


@wrap_model_call
async def dynamic_tool_router(request: ModelRequest, handler):
    """Restrict each model call to the tools relevant to the original user request."""
    question = ""
    for message in reversed(request.messages):
        if isinstance(message, HumanMessage):
            question = message.text
            break
    selected = select_relevant_tools(question, request.tools)
    return await handler(request.override(tools=selected))


__all__ = [
    "MAX_EXPOSED_TOOLS",
    "DEFAULT_TOOL_PREFIXES",
    "TOOL_METADATA",
    "TOOL_CATALOG",
    "ToolSpec",
    "tool_registry",
    "select_relevant_tools",
    "dynamic_tool_router",
]
