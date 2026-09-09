from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.agent.tool_registry import ToolSpec, tool_registry
from app.api import metrics


def _client_with(*tools) -> SimpleNamespace:
    return SimpleNamespace(get_tools=AsyncMock(return_value=list(tools)))


def _patch_client(monkeypatch, client) -> None:
    monkeypatch.setattr(
        metrics,
        "get_mcp_client_with_retry",
        AsyncMock(return_value=client),
    )


@pytest.mark.asyncio
async def test_trace_probe_picks_any_registered_read_only_tool(monkeypatch) -> None:
    """The probe verifies trace propagation, so it must not depend on one vendor tool."""
    tool = SimpleNamespace(
        name="prom_active_alerts",
        ainvoke=AsyncMock(return_value={"status": "ok"}),
    )
    _patch_client(monkeypatch, _client_with(tool))
    monkeypatch.setattr(metrics.config, "trace_probe_tool", "", raising=False)

    response = await metrics.trace_probe()

    assert response["ok"] is True
    assert response["probe_tool"] == "prom_active_alerts"
    assert response["path"] == ["aiops-api", "mcp", "prom_active_alerts"]
    assert response["result"] == {"status": "ok"}
    tool.ainvoke.assert_awaited_once_with({})


@pytest.mark.asyncio
async def test_trace_probe_skips_tools_that_are_not_read_only(monkeypatch) -> None:
    """A write-capable tool must never be invoked by an observability probe."""
    tool_registry.register(
        ToolSpec(
            name="restart_service_probe_fixture",
            category="test",
            read_only=False,
            risk_level=1,
            required_role="operator",
        )
    )
    risky = SimpleNamespace(
        name="restart_service_probe_fixture",
        ainvoke=AsyncMock(return_value={"restarted": True}),
    )
    safe = SimpleNamespace(
        name="prom_active_alerts",
        ainvoke=AsyncMock(return_value={"status": "ok"}),
    )
    _patch_client(monkeypatch, _client_with(risky, safe))
    monkeypatch.setattr(metrics.config, "trace_probe_tool", "", raising=False)

    response = await metrics.trace_probe()

    assert response["probe_tool"] == "prom_active_alerts"
    risky.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_trace_probe_honours_configured_tool(monkeypatch) -> None:
    first = SimpleNamespace(
        name="prom_active_alerts",
        ainvoke=AsyncMock(return_value={"status": "ok"}),
    )
    pinned = SimpleNamespace(
        name="get_current_time",
        ainvoke=AsyncMock(return_value={"now": "2026-01-01T00:00:00Z"}),
    )
    _patch_client(monkeypatch, _client_with(first, pinned))
    monkeypatch.setattr(metrics.config, "trace_probe_tool", "get_current_time", raising=False)

    response = await metrics.trace_probe()

    assert response["probe_tool"] == "get_current_time"
    pinned.ainvoke.assert_awaited_once_with({})
    first.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_trace_probe_reports_missing_configured_tool(monkeypatch) -> None:
    _patch_client(monkeypatch, _client_with())
    monkeypatch.setattr(metrics.config, "trace_probe_tool", "absent_tool", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        await metrics.trace_probe()

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "trace_probe_failed:RuntimeError"


@pytest.mark.asyncio
async def test_trace_probe_reports_when_no_tool_is_available(monkeypatch) -> None:
    _patch_client(monkeypatch, _client_with())
    monkeypatch.setattr(metrics.config, "trace_probe_tool", "", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        await metrics.trace_probe()

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "trace_probe_failed:RuntimeError"
