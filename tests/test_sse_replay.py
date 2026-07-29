"""SSE 重放缓存治理：TTL 驱逐、LRU 上限驱逐、活跃 producer 并发去重"""

import time
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import chat
from app.config import config
from app.models.request import ChatRequest
from app.sse import SSEReplayStore, sse_replay_store


def test_terminal_keys_expire_after_ttl() -> None:
    store = SSEReplayStore(max_events=10, ttl_seconds=0.05, max_keys=10)
    store.publish("done-key", '{"type":"done"}', terminal=True)
    store.publish("live-key", '{"type":"content"}')

    assert store.is_terminal("done-key") is True

    time.sleep(0.06)

    # terminal 且超过 TTL 的 key 被惰性驱逐；未终结的 key 不受影响
    assert store.replay("done-key") == []
    assert store.is_terminal("done-key") is False
    assert len(store.replay("live-key")) == 1


def test_lru_eviction_when_exceeding_max_keys() -> None:
    store = SSEReplayStore(max_events=10, ttl_seconds=600.0, max_keys=2)
    store.publish("k1", "a")
    store.publish("k2", "b")
    store.publish("k3", "c")

    # 最久未使用的 k1 被驱逐，k2/k3 保留
    assert store.replay("k1") == []
    assert [event.data for event in store.replay("k2")] == ["b"]
    assert [event.data for event in store.replay("k3")] == ["c"]


def test_lru_eviction_skips_active_producer() -> None:
    store = SSEReplayStore(max_events=10, ttl_seconds=600.0, max_keys=2)
    assert store.try_acquire("k1") is True
    store.publish("k1", "a")
    store.publish("k2", "b")
    store.publish("k3", "c")

    # k1 有活跃 producer，即使最久未使用也不驱逐，转而驱逐 k2
    assert [event.data for event in store.replay("k1")] == ["a"]
    assert store.replay("k2") == []


def test_try_acquire_mutex_and_release_keeps_replay() -> None:
    store = SSEReplayStore(max_events=10, ttl_seconds=600.0, max_keys=10)
    assert store.try_acquire("k") is True
    assert store.try_acquire("k") is False

    store.release("k")
    assert store.try_acquire("k") is True
    store.release("k")

    # release 后 terminal 事件仍可正常重放
    store.publish("k", '{"type":"done"}', terminal=True)
    assert store.is_terminal("k") is True
    assert len(store.replay("k")) == 1


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
async def test_chat_stream_duplicate_active_key_rejected_with_409(monkeypatch) -> None:
    """同 stream_key 的并发重连在 producer 活跃时返回 409，release 后可再次执行"""
    sse_replay_store.clear()
    monkeypatch.setattr(config, "auth_enabled", False)
    monkeypatch.setattr(chat.agent_capacity, "acquire", AsyncMock())
    monkeypatch.setattr(chat.agent_capacity, "release", Mock())

    async def fake_stream(_question: str, session_id: str):
        yield {"type": "content", "data": "partial"}
        yield {"type": "complete", "data": {"answer": "partial"}}

    monkeypatch.setattr(chat.rag_agent_service, "query_stream", fake_stream)

    first = await chat.chat_stream(
        ChatRequest(Id="dup", Question="q"),
        _stream_request("/api/chat_stream", "dup-op"),
    )
    with pytest.raises(HTTPException) as exc_info:
        await chat.chat_stream(
            ChatRequest(Id="dup", Question="q"),
            _stream_request("/api/chat_stream", "dup-op"),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "stream_already_active"

    # 消费并关闭第一个流后 producer 登记释放，同 key 可再次执行
    await anext(first.body_iterator)
    await first.body_iterator.aclose()

    third = await chat.chat_stream(
        ChatRequest(Id="dup", Question="q"),
        _stream_request("/api/chat_stream", "dup-op"),
    )
    await third.body_iterator.aclose()
    sse_replay_store.clear()
