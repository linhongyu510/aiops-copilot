import httpx
import pytest

from app.config import config
from app.main import app


@pytest.mark.asyncio
async def test_public_liveness_and_protected_api_roles(monkeypatch) -> None:
    monkeypatch.setattr(config, "auth_enabled", True)
    monkeypatch.setattr(
        config,
        "api_keys",
        "viewer-secret:viewer,operator-secret:operator",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        live = await client.get("/live")
        unauthenticated = await client.get("/api/metrics/tools")
        viewer = await client.get(
            "/api/metrics/tools",
            headers={"X-API-Key": "viewer-secret", "X-Request-ID": "test-request"},
        )
        forbidden = await client.post(
            "/api/chat",
            headers={"X-API-Key": "viewer-secret"},
            json={"Id": "session", "Question": "hello"},
        )

    assert live.status_code == 200
    assert unauthenticated.status_code == 401
    assert viewer.status_code == 200
    assert viewer.headers["X-Request-ID"] == "test-request"
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_missing_key_configuration_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(config, "auth_enabled", True)
    monkeypatch.setattr(config, "api_keys", "")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/metrics/tools")
    assert response.status_code == 503
    assert response.json()["detail"] == "auth_not_configured"
