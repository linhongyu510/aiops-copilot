"""``aiops_core._optional`` 的最小回归：hint 映射 + 缺失依赖错误。

单测目标：
- ``require_module`` 命中已装模块时正常返回；
- 缺失模块抛 :class:`OptionalDependencyMissing`，错误消息里带 extras 名；
- ``report_environment`` 输出布尔字典且键固定。
"""

from __future__ import annotations

import pytest

from aiops_core import report_environment
from aiops_core._optional import (
    OptionalDependencyMissing,
    _extras_for,
    has_module,
    require_module,
)


def test_extras_for_known_modules() -> None:
    assert _extras_for("langchain_openai") == "llm"
    assert _extras_for("fastapi") == "server"
    assert _extras_for("pymilvus") == "rag"
    assert _extras_for("psycopg") == "state"
    assert _extras_for("opentelemetry.trace") == "obs"
    assert _extras_for("fastmcp") == "mcp"
    assert _extras_for("tavily") == "mcp-full"


def test_extras_for_unknown_module_falls_back_to_full() -> None:
    assert _extras_for("something_totally_new") == "full"


def test_require_module_returns_when_present() -> None:
    module = require_module("json")
    assert module.__name__ == "json"


def test_require_module_raises_with_hint() -> None:
    with pytest.raises(OptionalDependencyMissing) as exc_info:
        require_module("aiops__does_not_exist__pkg")

    msg = str(exc_info.value)
    assert "aiops__does_not_exist__pkg" in msg
    assert "pip install 'aiops-copilot[" in msg


def test_require_module_accepts_override_extras() -> None:
    with pytest.raises(OptionalDependencyMissing) as exc_info:
        require_module("aiops__missing__", extras="custom-extra")

    assert "aiops-copilot[custom-extra]" in str(exc_info.value)


def test_has_module_matches_reality() -> None:
    assert has_module("json") is True
    assert has_module("aiops__really_missing__") is False


def test_report_environment_returns_bool_map() -> None:
    env = report_environment()
    assert set(env.keys()) == {"llm", "server", "rag", "state", "obs", "mcp"}
    for value in env.values():
        assert isinstance(value, bool)
