import pytest

from app.api.health import health_check, liveness_check, production_readiness, readiness_check
from app.config import config


@pytest.mark.asyncio
async def test_liveness_does_not_depend_on_milvus() -> None:
    response = await liveness_check()
    assert response["status"] == "alive"


@pytest.mark.asyncio
async def test_readiness_reports_milvus_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.health.milvus_manager.health_check",
        lambda: False,
    )
    response = await readiness_check()
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_production_readiness_is_honest_about_local_state(monkeypatch) -> None:
    monkeypatch.setattr(config, "auth_enabled", False)
    report = await production_readiness()
    assert report["ready_for_production"] is False
    assert "authentication" in report["blockers"]
    assert "persistent_sessions" in report["blockers"]


@pytest.mark.asyncio
async def test_legacy_health_endpoint_remains_compatible(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.health.milvus_manager.health_check",
        lambda: True,
    )
    response = await health_check()
    assert response.status_code == 200
