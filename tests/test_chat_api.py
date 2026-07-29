import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.requests import Request

from app.api import aiops, chat
from app.capacity import AgentCapacityError
from app.config import config
from app.main import app
from app.models.aiops import AIOpsRequest
from app.models.request import ChatRequest
from app.sse import sse_replay_store


@pytest.fixture
def unauthenticated_api(monkeypatch):
    monkeypatch.setattr(config, "auth_enabled", False)
    return httpx.ASGITransport(app=app)


@pytest.mark.asyncio
async def test_quick_chat_success_and_model_failure(monkeypatch, unauthenticated_api) -> None:
    acquire = AsyncMock()
    release = Mock()
    query = AsyncMock(side_effect=["evidence-based answer", RuntimeError("secret failure")])
    monkeypatch.setattr(chat.agent_capacity, "acquire", acquire)
    monkeypatch.setattr(chat.agent_capacity, "release", release)
    monkeypatch.setattr(chat.rag_agent_service, "query", query)

    async with httpx.AsyncClient(
        transport=unauthenticated_api, base_url="http://test"
    ) as client:
        success = await client.post(
            "/api/chat", json={"Id": "session-a", "Question": "check service"}
        )
        failure = await client.post(
            "/api/chat", json={"Id": "session-b", "Question": "check service"}
        )

    assert success.status_code == 200
    assert success.json()["data"]["answer"] == "evidence-based answer"
    assert failure.status_code == 502
    assert failure.json()["message"] == "model_service_error"
    assert "secret failure" not in failure.text
    assert acquire.await_count == 2
    assert release.call_count == 2


@pytest.mark.asyncio
async def test_quick_chat_timeout_maps_to_504(monkeypatch, unauthenticated_api) -> None:
    async def slow_query(_question: str, session_id: str):
        await asyncio.sleep(5)
        return "unreachable"

    release = Mock()
    monkeypatch.setattr(chat.agent_capacity, "acquire", AsyncMock())
    monkeypatch.setattr(chat.agent_capacity, "release", release)
    monkeypatch.setattr(chat.rag_agent_service, "query", slow_query)
    monkeypatch.setattr(config, "chat_total_timeout_seconds", 0.05)

    async with httpx.AsyncClient(
        transport=unauthenticated_api, base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/chat", json={"Id": "session-t", "Question": "slow question"}
        )

    assert response.status_code == 504
    assert response.json()["message"] == "chat_timeout"
    assert response.json()["data"]["errorMessage"] == "对话处理超时，请缩小问题范围后重试"
    release.assert_called_once()


@pytest.mark.asyncio
async def test_quick_chat_capacity_rejection(monkeypatch, unauthenticated_api) -> None:
    monkeypatch.setattr(
        chat.agent_capacity,
        "acquire",
        AsyncMock(side_effect=AgentCapacityError("full")),
    )
    release = Mock()
    monkeypatch.setattr(chat.agent_capacity, "release", release)

    async with httpx.AsyncClient(
        transport=unauthenticated_api, base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/chat", json={"Id": "session", "Question": "hello"}
        )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "2"
    release.assert_not_called()


@pytest.mark.asyncio
async def test_chat_stream_serializes_events_and_releases_capacity(
    monkeypatch, unauthenticated_api
) -> None:
    async def fake_stream(_question: str, session_id: str):
        assert session_id == "stream-session"
        yield {"type": "content", "data": "partial"}
        yield {"type": "complete", "data": {"answer": "partial"}}

    monkeypatch.setattr(chat.agent_capacity, "acquire", AsyncMock())
    release = Mock()
    monkeypatch.setattr(chat.agent_capacity, "release", release)
    monkeypatch.setattr(chat.rag_agent_service, "query_stream", fake_stream)

    async with httpx.AsyncClient(
        transport=unauthenticated_api, base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/chat_stream",
            json={"Id": "stream-session", "Question": "stream it"},
        )

    assert response.status_code == 200
    assert '"type": "content"' in response.text
    assert '"type": "done"' in response.text
    release.assert_called_once()


@pytest.mark.asyncio
async def test_chat_stream_replays_terminal_operation_without_rerun(
    monkeypatch, unauthenticated_api
) -> None:
    calls = 0

    async def fake_stream(_question: str, session_id: str):
        nonlocal calls
        calls += 1
        yield {"type": "content", "data": session_id}
        yield {"type": "complete", "data": {"answer": "done"}}

    sse_replay_store.clear()
    monkeypatch.setattr(chat.agent_capacity, "acquire", AsyncMock())
    monkeypatch.setattr(chat.agent_capacity, "release", Mock())
    monkeypatch.setattr(chat.rag_agent_service, "query_stream", fake_stream)
    headers = {"X-Idempotency-Key": "stable-operation"}

    async with httpx.AsyncClient(
        transport=unauthenticated_api, base_url="http://test"
    ) as client:
        first = await client.post(
            "/api/chat_stream",
            headers=headers,
            json={"Id": "session", "Question": "stream"},
        )
        replay = await client.post(
            "/api/chat_stream",
            headers={**headers, "Last-Event-ID": "1"},
            json={"Id": "session", "Question": "stream"},
        )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert '"type": "done"' in replay.text
    assert calls == 1


@pytest.mark.asyncio
async def test_session_get_and_clear(monkeypatch, unauthenticated_api) -> None:
    monkeypatch.setattr(
        chat.rag_agent_service,
        "get_session_history",
        AsyncMock(return_value=[{"role": "user", "content": "hello"}]),
    )
    monkeypatch.setattr(
        chat.rag_agent_service, "clear_session", AsyncMock(return_value=True)
    )

    async with httpx.AsyncClient(
        transport=unauthenticated_api, base_url="http://test"
    ) as client:
        history = await client.get("/api/chat/session/session-a")
        cleared = await client.post(
            "/api/chat/clear", json={"session_id": "session-a"}
        )

    assert history.status_code == 200
    assert history.json()["message_count"] == 1
    assert cleared.status_code == 200
    assert cleared.json()["status"] == "success"


@pytest.mark.asyncio
async def test_aiops_stream_stops_after_completion(monkeypatch, unauthenticated_api) -> None:
    async def fake_diagnose(session_id: str):
        assert session_id == "ops-session"
        yield {"type": "status", "message": "started"}
        yield {"type": "complete", "message": "done"}
        yield {"type": "status", "message": "must not be emitted"}

    monkeypatch.setattr(aiops.agent_capacity, "acquire", AsyncMock())
    release = Mock()
    monkeypatch.setattr(aiops.agent_capacity, "release", release)
    monkeypatch.setattr(aiops.aiops_service, "diagnose", fake_diagnose)

    async with httpx.AsyncClient(
        transport=unauthenticated_api, base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/aiops", json={"session_id": "ops-session"}
        )

    assert response.status_code == 200
    assert '"type": "complete"' in response.text
    assert "must not be emitted" not in response.text
    release.assert_called_once()


def _stream_request(path: str, idempotency_key: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"x-idempotency-key", idempotency_key.encode())],
            "state": {},
        }
    )


@pytest.mark.asyncio
async def test_chat_stream_cancellation_propagates_and_releases_capacity(
    monkeypatch,
) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    async def blocked_stream(_question: str, session_id: str):
        del session_id
        try:
            started.set()
            await asyncio.Event().wait()
            yield {"type": "content", "data": "unreachable"}
        finally:
            closed.set()

    release = Mock()
    monkeypatch.setattr(config, "auth_enabled", False)
    monkeypatch.setattr(chat.agent_capacity, "acquire", AsyncMock())
    monkeypatch.setattr(chat.agent_capacity, "release", release)
    monkeypatch.setattr(chat.rag_agent_service, "query_stream", blocked_stream)
    response = await chat.chat_stream(
        ChatRequest(Id="cancel-chat", Question="wait"),
        _stream_request("/api/chat_stream", "cancel-chat-op"),
    )

    consumer = asyncio.create_task(anext(response.body_iterator))
    await started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    await asyncio.wait_for(closed.wait(), timeout=1)
    release.assert_called_once()


@pytest.mark.asyncio
async def test_aiops_stream_cancellation_propagates_and_releases_capacity(
    monkeypatch,
) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    async def blocked_diagnosis(session_id: str):
        del session_id
        try:
            started.set()
            await asyncio.Event().wait()
            yield {"type": "status", "message": "unreachable"}
        finally:
            closed.set()

    release = Mock()
    monkeypatch.setattr(config, "auth_enabled", False)
    monkeypatch.setattr(aiops.agent_capacity, "acquire", AsyncMock())
    monkeypatch.setattr(aiops.agent_capacity, "release", release)
    monkeypatch.setattr(aiops.aiops_service, "diagnose", blocked_diagnosis)
    response = await aiops.diagnose_stream(
        AIOpsRequest(session_id="cancel-aiops"),
        _stream_request("/api/aiops", "cancel-aiops-op"),
    )

    consumer = asyncio.create_task(anext(response.body_iterator))
    await started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    await asyncio.wait_for(closed.wait(), timeout=1)
    release.assert_called_once()
