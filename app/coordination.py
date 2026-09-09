"""Process-local and Redis-backed coordination primitives.

需要 ``[state]`` extra（``redis``）。缺失时抛
:class:`aiops_core._optional.OptionalDependencyMissing`。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
import uuid
from typing import Any

from loguru import logger

try:
    from redis.asyncio import Redis
except ImportError as _exc:  # pragma: no cover - depends on install profile
    from aiops_core._optional import OptionalDependencyMissing

    raise OptionalDependencyMissing(
        "app.coordination 需要 `[state]` extra（redis）。"
        "\n    pip install 'aiops-copilot[state]'"
    ) from _exc

from app.capacity import AgentCapacityError, AgentCapacityLimiter, agent_capacity
from app.config import config
from app.sse import SSEReplayStore, StoredEvent, sse_replay_store


class MemoryReplayBackend:
    def __init__(self, store: SSEReplayStore) -> None:
        self.store = store

    async def publish(
        self, key: str, data: str, *, event: str = "message", terminal: bool = False
    ) -> StoredEvent:
        return self.store.publish(key, data, event=event, terminal=terminal)

    async def replay(self, key: str, after_id: int = 0) -> list[StoredEvent]:
        return self.store.replay(key, after_id)

    async def is_terminal(self, key: str) -> bool:
        return self.store.is_terminal(key)

    async def try_acquire(self, key: str) -> str | None:
        return key if self.store.try_acquire(key) else None

    async def release(self, key: str, lease: str) -> None:
        del lease
        self.store.release(key)


class RedisReplayBackend:
    _RELEASE_LEASE_SCRIPT = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """

    def __init__(self, client: Redis, prefix: str) -> None:
        self.client = client
        self.prefix = prefix.strip(":") or "aiops"
        self.max_events = config.sse_replay_events
        self.terminal_ttl = max(1, math.ceil(config.sse_replay_ttl_seconds))
        self.active_ttl = max(
            self.terminal_ttl,
            math.ceil(config.aiops_total_timeout_seconds + 60),
            math.ceil(config.chat_total_timeout_seconds + 60),
        )

    def _keys(self, key: str) -> tuple[str, str, str, str]:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        base = f"{self.prefix}:sse:{digest}"
        return f"{base}:seq", f"{base}:events", f"{base}:terminal", f"{base}:lease"

    async def publish(
        self, key: str, data: str, *, event: str = "message", terminal: bool = False
    ) -> StoredEvent:
        seq_key, events_key, terminal_key, _ = self._keys(key)
        event_id = int(await self.client.incr(seq_key))
        stored = StoredEvent(event_id, event, data, terminal)
        encoded = json.dumps(
            {
                "event_id": event_id,
                "event": event,
                "data": data,
                "terminal": terminal,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        ttl = self.terminal_ttl if terminal else self.active_ttl
        pipe = self.client.pipeline(transaction=True)
        pipe.rpush(events_key, encoded)
        pipe.ltrim(events_key, -self.max_events, -1)
        pipe.expire(seq_key, ttl)
        pipe.expire(events_key, ttl)
        if terminal:
            pipe.set(terminal_key, "1", ex=self.terminal_ttl)
        await pipe.execute()
        return stored

    async def replay(self, key: str, after_id: int = 0) -> list[StoredEvent]:
        _, events_key, _, _ = self._keys(key)
        payloads = await self.client.lrange(events_key, 0, -1)
        events = []
        for payload in payloads:
            item = json.loads(payload)
            if int(item["event_id"]) > after_id:
                events.append(
                    StoredEvent(
                        event_id=int(item["event_id"]),
                        event=str(item["event"]),
                        data=str(item["data"]),
                        terminal=bool(item["terminal"]),
                    )
                )
        return sorted(events, key=lambda item: item.event_id)

    async def is_terminal(self, key: str) -> bool:
        _, _, terminal_key, _ = self._keys(key)
        return bool(await self.client.exists(terminal_key))

    async def try_acquire(self, key: str) -> str | None:
        _, _, _, lease_key = self._keys(key)
        lease = uuid.uuid4().hex
        acquired = await self.client.set(
            lease_key,
            lease,
            nx=True,
            ex=self.active_ttl,
        )
        return lease if acquired else None

    async def release(self, key: str, lease: str) -> None:
        _, _, _, lease_key = self._keys(key)
        await self.client.eval(self._RELEASE_LEASE_SCRIPT, 1, lease_key, lease)


class MemoryCapacityBackend:
    def __init__(self, limiter: AgentCapacityLimiter) -> None:
        self.limiter = limiter

    async def acquire(self) -> str:
        await self.limiter.acquire()
        return uuid.uuid4().hex

    async def release(self, lease: str) -> None:
        del lease
        self.limiter.release()


class RedisCapacityBackend:
    _ACQUIRE_SCRIPT = """
    redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
    if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3]) then
      redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
      redis.call('PEXPIRE', KEYS[1], ARGV[5])
      return 1
    end
    return 0
    """

    def __init__(
        self,
        client: Redis,
        prefix: str,
        *,
        limit: int | None = None,
        queue_timeout_seconds: float | None = None,
        lease_seconds: float | None = None,
    ) -> None:
        self.client = client
        self.key = f"{prefix.strip(':') or 'aiops'}:capacity:agent"
        self.limit = max(
            1,
            limit if limit is not None else config.max_concurrent_agent_requests,
        )
        self.queue_timeout = max(
            0.01,
            queue_timeout_seconds
            if queue_timeout_seconds is not None
            else config.request_queue_timeout_seconds,
        )
        self.lease_ms = max(
            60_000,
            int(
                (
                    lease_seconds
                    if lease_seconds is not None
                    else config.aiops_total_timeout_seconds + 60
                )
                * 1000
            ),
        )

    async def acquire(self) -> str:
        lease = uuid.uuid4().hex
        deadline = time.monotonic() + self.queue_timeout
        while True:
            now_ms = int(time.time() * 1000)
            acquired = await self.client.eval(
                self._ACQUIRE_SCRIPT,
                1,
                self.key,
                now_ms,
                now_ms + self.lease_ms,
                self.limit,
                lease,
                self.lease_ms,
            )
            if acquired:
                return lease
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentCapacityError("agent_capacity_exhausted")
            await asyncio.sleep(min(0.05, remaining))

    async def release(self, lease: str) -> None:
        await self.client.zrem(self.key, lease)


class CoordinationRuntime:
    def __init__(self) -> None:
        self.backend = "memory"
        self.degraded_reason: str | None = None
        self.client: Redis | None = None
        self.replay: Any = MemoryReplayBackend(sse_replay_store)
        self.capacity: Any = MemoryCapacityBackend(agent_capacity)

    async def start(self) -> None:
        selected = config.coordination_backend.strip().lower()
        if selected in {"", "memory"}:
            return
        if selected != "redis":
            raise ValueError(f"unsupported coordination backend: {selected}")
        client = Redis.from_url(config.redis_url, decode_responses=True)
        try:
            await client.ping()
            self.client = client
            self.replay = RedisReplayBackend(client, config.coordination_key_prefix)
            self.capacity = RedisCapacityBackend(client, config.coordination_key_prefix)
            self.backend = "redis"
            self.degraded_reason = None
            logger.info("Redis 分布式协调已就绪")
        except Exception as exc:
            await client.aclose()
            self.degraded_reason = f"{type(exc).__name__}: {exc}"
            if config.coordination_required:
                raise
            logger.warning("Redis 协调不可用，降级为单进程内存状态: {}", type(exc).__name__)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None


coordination_runtime = CoordinationRuntime()
