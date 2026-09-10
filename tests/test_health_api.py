import json

import pytest

from app.api.health import health_check, liveness_check, production_readiness, readiness_check
from app.config import config
from app.services import retrieval_backend


@pytest.mark.asyncio
async def test_liveness_does_not_depend_on_milvus() -> None:
    response = await liveness_check()
    assert response["status"] == "alive"


@pytest.mark.asyncio
async def test_readiness_reports_milvus_failure(monkeypatch) -> None:
    monkeypatch.setattr(config, "retrieval_backend", "milvus")
    retrieval_backend.reset_retrieval_backend()
    backend = retrieval_backend.get_retrieval_backend()
    monkeypatch.setattr(
        backend,
        "health",
        lambda: {"status": "disconnected", "message": "Milvus 连接异常"},
    )
    try:
        response = await readiness_check()
        assert response.status_code == 503
    finally:
        retrieval_backend.reset_retrieval_backend()


@pytest.mark.asyncio
async def test_production_readiness_is_honest_about_local_state(monkeypatch) -> None:
    monkeypatch.setattr(config, "auth_enabled", False)
    report = await production_readiness()
    assert report["ready_for_production"] is False
    assert "authentication" in report["blockers"]
    assert "persistent_sessions" in report["blockers"]


@pytest.mark.asyncio
async def test_legacy_health_endpoint_remains_compatible(monkeypatch) -> None:
    monkeypatch.setattr(config, "retrieval_backend", "milvus")
    retrieval_backend.reset_retrieval_backend()
    backend = retrieval_backend.get_retrieval_backend()
    monkeypatch.setattr(
        backend,
        "health",
        lambda: {"status": "connected", "message": "Milvus 连接正常"},
    )
    try:
        response = await health_check()
        assert response.status_code == 200
    finally:
        retrieval_backend.reset_retrieval_backend()


@pytest.mark.asyncio
async def test_local_wiki_health_returns_200_without_milvus(monkeypatch) -> None:
    monkeypatch.setattr(config, "retrieval_backend", "local_wiki")
    retrieval_backend.reset_retrieval_backend()
    backend = retrieval_backend.get_retrieval_backend()
    monkeypatch.setattr(
        backend,
        "health",
        lambda: {"status": "ready", "pages": 42, "message": "本地 Wiki 就绪"},
    )
    try:
        response = await health_check()
        assert response.status_code == 200
        payload = json.loads(response.body)
        data = payload["data"]
        assert data["retrieval_backend"] == "local_wiki"
        assert data["local_wiki"]["status"] == "ready"
    finally:
        retrieval_backend.reset_retrieval_backend()
