from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import metrics


@pytest.mark.asyncio
async def test_trace_probe_invokes_read_only_windos_tool(monkeypatch) -> None:
    tool = SimpleNamespace(
        name="windos_health",
        ainvoke=AsyncMock(return_value={"status": "ok"}),
    )
    client = SimpleNamespace(get_tools=AsyncMock(return_value=[tool]))
    monkeypatch.setattr(
        metrics,
        "get_mcp_client_with_retry",
        AsyncMock(return_value=client),
    )

    response = await metrics.trace_probe()

    assert response["ok"] is True
    assert response["path"] == ["aiops-api", "mcp-ops", "windos"]
    assert response["result"] == {"status": "ok"}
    tool.ainvoke.assert_awaited_once_with({})


@pytest.mark.asyncio
async def test_trace_probe_reports_missing_tool(monkeypatch) -> None:
    client = SimpleNamespace(get_tools=AsyncMock(return_value=[]))
    monkeypatch.setattr(
        metrics,
        "get_mcp_client_with_retry",
        AsyncMock(return_value=client),
    )

    with pytest.raises(HTTPException) as exc_info:
        await metrics.trace_probe()

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "trace_probe_failed:RuntimeError"
