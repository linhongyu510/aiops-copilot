import os
import uuid
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis

from app.capacity import AgentCapacityError, AgentCapacityLimiter
from app.config import config
from app.coordination import (
    CoordinationRuntime,
    MemoryCapacityBackend,
    MemoryReplayBackend,
    RedisCapacityBackend,
    RedisReplayBackend,
)
from app.sse import SSEReplayStore


@pytest.mark.asyncio
async def test_memory_coordination_adapters() -> None:
    replay = MemoryReplayBackend(SSEReplayStore())
    capacity = MemoryCapacityBackend(AgentCapacityLimiter(1, 0.01))

    producer_lease = await replay.try_acquire("operation")
    assert producer_lease
    assert await replay.try_acquire("operation") is None

    first = await replay.publish("operation", '{"type":"content"}')
    terminal = await replay.publish(
        "operation",
        '{"type":"done"}',
        terminal=True,
    )
    assert [event.event_id for event in await replay.replay("operation", first.event_id)] == [
        terminal.event_id
    ]
    assert await replay.is_terminal("operation") is True
    await replay.release("operation", producer_lease)
    assert await replay.try_acquire("operation")

    lease = await capacity.acquire()
    with pytest.raises(AgentCapacityError):
        await capacity.acquire()
    await capacity.release(lease)


@pytest.mark.asyncio
async def test_coordination_runtime_memory_and_unknown_backend(monkeypatch) -> None:
    monkeypatch.setattr(config, "coordination_backend", "memory")
    runtime = CoordinationRuntime()
    await runtime.start()
    assert runtime.backend == "memory"

    monkeypatch.setattr(config, "coordination_backend", "unknown")
    with pytest.raises(ValueError, match="unsupported"):
        await CoordinationRuntime().start()


@pytest.mark.asyncio
async def test_coordination_runtime_falls_back_when_redis_is_unavailable(
    monkeypatch,
) -> None:
    client = AsyncMock()
    client.ping.side_effect = ConnectionError("fixture unavailable")
    monkeypatch.setattr(config, "coordination_backend", "redis")
    monkeypatch.setattr(config, "coordination_required", False)
    monkeypatch.setattr("app.coordination.Redis.from_url", lambda *_args, **_kwargs: client)

    runtime = CoordinationRuntime()
    await runtime.start()

    assert runtime.backend == "memory"
    assert "ConnectionError" in (runtime.degraded_reason or "")
    client.aclose.assert_awaited_once()


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("AIOPS_RUN_REDIS_INTEGRATION") != "1",
    reason="set AIOPS_RUN_REDIS_INTEGRATION=1 and start reliability-stack.yml",
)
@pytest.mark.asyncio
async def test_redis_coordination_is_shared_across_instances() -> None:
    client_a = Redis.from_url(config.redis_url, decode_responses=True)
    client_b = Redis.from_url(config.redis_url, decode_responses=True)
    prefix = f"test:{uuid.uuid4().hex}"
    replay_a = RedisReplayBackend(client_a, prefix)
    replay_b = RedisReplayBackend(client_b, prefix)
    capacity_a = RedisCapacityBackend(
        client_a,
        prefix,
        limit=1,
        queue_timeout_seconds=0.02,
    )
    capacity_b = RedisCapacityBackend(
        client_b,
        prefix,
        limit=1,
        queue_timeout_seconds=0.02,
    )

    try:
        producer_lease = await replay_a.try_acquire("operation")
        assert producer_lease
        assert await replay_b.try_acquire("operation") is None

        first = await replay_a.publish("operation", '{"type":"content"}')
        terminal = await replay_a.publish(
            "operation",
            '{"type":"done"}',
            terminal=True,
        )
        replayed = await replay_b.replay("operation", first.event_id)
        assert [event.event_id for event in replayed] == [terminal.event_id]
        assert await replay_b.is_terminal("operation") is True

        capacity_lease = await capacity_a.acquire()
        with pytest.raises(AgentCapacityError):
            await capacity_b.acquire()
        await capacity_a.release(capacity_lease)
        assert await capacity_b.acquire()

        await replay_a.release("operation", producer_lease)
        assert await replay_b.try_acquire("operation")
    finally:
        keys = [key async for key in client_a.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client_a.delete(*keys)
        await client_a.aclose()
        await client_b.aclose()
