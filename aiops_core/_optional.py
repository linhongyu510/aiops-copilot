"""内核 lazy-import 守卫：给可选 extras 提供统一的缺失提示。

CLI / MCP / re-export 层如果需要 langchain / fastmcp / pymilvus 等能力，先经过
:func:`require_module` 拿到已装载的模块引用；缺失时抛 :class:`OptionalDependencyMissing`，
错误消息里带上对应 extras 名（如 ``pip install "aiops-copilot[llm]"``），避免用户看
到裸的 ModuleNotFoundError 摸不到重装路径。

不做的事：
- 不做 import 拦截 / meta path 魔法；只是"检查 -> import -> 返回"的直白函数；
- 不写 shim，也不给缺失依赖构造 stub 对象 —— 让上层代码要么正常用，要么直接
  没能力时给出 405/Feature Disabled，而不是引入沉默失败。
"""

from __future__ import annotations

import importlib
from types import ModuleType


class OptionalDependencyMissing(ImportError):
    """引导用户按 extras 装依赖的错误。"""


_HINT: dict[str, str] = {
    "langchain": "llm",
    "langchain_core": "llm",
    "langchain_openai": "llm",
    "langchain_community": "llm",
    "langchain_mcp_adapters": "llm",
    "langchain_text_splitters": "llm",
    "langchain_milvus": "rag",
    "langgraph": "llm",
    "openai": "llm",
    "dashscope": "llm",
    "fastapi": "server",
    "uvicorn": "server",
    "sse_starlette": "server",
    "pydantic_settings": "server",
    "aiofiles": "server",
    "pymilvus": "rag",
    "sentence_transformers": "local-embeddings",
    "transformers": "local-embeddings",
    "huggingface_hub": "local-embeddings",
    "modelscope": "local-embeddings",
    "sqlalchemy": "state",
    "pymysql": "state",
    "redis": "state",
    "psycopg": "state",
    "langgraph_checkpoint_postgres": "state",
    "opentelemetry": "obs",
    "fastmcp": "mcp",
    "tavily": "mcp-full",
}


def _extras_for(module_name: str) -> str:
    root = module_name.split(".", 1)[0]
    return _HINT.get(root, "full")


def require_module(module_name: str, *, extras: str | None = None) -> ModuleType:
    """按名称 lazy import 一个可选模块；缺失时抛可读错误。

    Args:
        module_name: 目标模块名（顶级或子模块）。
        extras: 覆盖默认 extras 提示；不传时按内置映射猜。
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        hint = extras or _extras_for(module_name)
        raise OptionalDependencyMissing(
            f"需要可选依赖 `{module_name}`。请安装 extras 后重试："
            f"\n    pip install 'aiops-copilot[{hint}]'"
        ) from exc


def has_module(module_name: str) -> bool:
    """轻量存在性探测；用于 CLI 打印能力清单等场景。"""
    try:
        importlib.import_module(module_name)
    except ImportError:
        return False
    return True
