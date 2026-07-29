import asyncio
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent

import app.agent.mcp_client as mcp_client_module
from app.agent.mcp_client import get_mcp_client, get_mcp_client_with_retry, retry_interceptor
from app.config import config
from app.observability import tool_metrics
from app.reliability import CircuitOpenError, dependency_guards


def _result(text: str, *, is_error: bool) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=is_error)


@pytest.fixture(autouse=True)
def reset_tool_metrics():
    tool_metrics.reset()
    dependency_guards.reset()
    yield
    tool_metrics.reset()
    dependency_guards.reset()


@pytest.mark.asyncio
async def test_retry_interceptor_retries_error_results() -> None:
    attempts = 0

    async def handler(_request):
        nonlocal attempts
        attempts += 1
        return _result("temporary failure", is_error=True) if attempts == 1 else _result(
            "ok", is_error=False
        )

    request = SimpleNamespace(name="demo_tool", server_name="demo", args={})
    result = await retry_interceptor(request, handler, max_retries=3, delay=0)  # type: ignore[arg-type]

    assert attempts == 2
    assert result.isError is False
    assert tool_metrics.snapshot()["tools"]["demo_tool"]["calls"] == 1
    assert tool_metrics.snapshot()["tools"]["demo_tool"]["success_rate"] == 1.0


@pytest.mark.asyncio
async def test_retry_interceptor_returns_last_error_result() -> None:
    attempts = 0

    async def handler(_request):
        nonlocal attempts
        attempts += 1
        return _result(f"failure-{attempts}", is_error=True)

    request = SimpleNamespace(name="demo_tool", server_name="demo", args={})
    result = await retry_interceptor(request, handler, max_retries=3, delay=0)  # type: ignore[arg-type]

    assert attempts == 3
    assert result.isError is True
    assert result.content[0].text == "failure-3"  # type: ignore[union-attr]
    assert tool_metrics.snapshot()["tools"]["demo_tool"]["success_rate"] == 0.0


@pytest.mark.asyncio
async def test_retry_interceptor_does_not_retry_validation_errors() -> None:
    attempts = 0

    async def handler(_request):
        nonlocal attempts
        attempts += 1
        raise ValueError("invalid arguments")

    request = SimpleNamespace(name="demo_tool", server_name="demo", args={})
    result = await retry_interceptor(request, handler, max_retries=3, delay=0)  # type: ignore[arg-type]

    assert result.isError is True
    assert attempts == 1


@pytest.mark.asyncio
async def test_retry_interceptor_retries_timeout_then_succeeds(monkeypatch) -> None:
    """单次执行超过 mcp_tool_timeout_seconds 视为失败，退避后重试并成功"""
    monkeypatch.setattr(config, "mcp_tool_timeout_seconds", 0.05)
    attempts = 0

    async def handler(_request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await asyncio.sleep(5)
        return _result("ok", is_error=False)

    request = SimpleNamespace(name="demo_tool", server_name="demo", args={})
    result = await retry_interceptor(request, handler, max_retries=3, delay=0)  # type: ignore[arg-type]

    assert attempts == 2
    assert result.isError is False
    assert tool_metrics.snapshot()["tools"]["demo_tool"]["success_rate"] == 1.0


@pytest.mark.asyncio
async def test_retry_interceptor_does_not_retry_circuit_open(monkeypatch) -> None:
    """熔断器拒绝（CircuitOpenError）没有重试意义，handler 只应被调用 0 次且直接结束"""
    attempts = 0

    async def handler(_request):
        nonlocal attempts
        attempts += 1
        return _result("ok", is_error=False)

    class _OpenGuard:
        calls = 0

        async def call(self, operation, **_kwargs):
            type(self).calls += 1
            raise CircuitOpenError("circuit_open:demo")

    monkeypatch.setattr(mcp_client_module.dependency_guards, "get", lambda _name: _OpenGuard())

    request = SimpleNamespace(name="demo_tool", server_name="demo", args={})
    result = await retry_interceptor(request, handler, max_retries=3, delay=0)  # type: ignore[arg-type]

    assert result.isError is True
    # 只尝试一次：guard.call 未重试，handler 也未被执行
    assert _OpenGuard.calls == 1
    assert attempts == 0
    assert "circuit_open" in result.content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_plain_and_retry_clients_use_separate_cache_entries(monkeypatch) -> None:
    created = []

    def fake_create(_servers, interceptors=None):
        instance = object()
        created.append((instance, tuple(interceptors or [])))
        return instance

    monkeypatch.setattr(mcp_client_module, "_create_mcp_client", fake_create)
    mcp_client_module._mcp_clients.clear()
    plain = await get_mcp_client(servers={"demo": {"transport": "stdio"}})
    retried = await get_mcp_client_with_retry(servers={"demo": {"transport": "stdio"}})

    assert plain is not retried
    assert len(created) == 2
