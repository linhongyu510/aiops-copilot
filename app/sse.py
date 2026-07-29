"""Bounded SSE replay buffers with per-operation idempotency."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from typing import Any

from app.config import config


@dataclass(frozen=True)
class StoredEvent:
    event_id: int
    event: str
    data: str
    terminal: bool

    def as_sse(self) -> dict[str, Any]:
        return {
            "id": str(self.event_id),
            "event": self.event,
            "data": self.data,
            "retry": 3000,
        }


class SSEReplayStore:
    def __init__(
        self,
        max_events: int | None = None,
        ttl_seconds: float | None = None,
        max_keys: int | None = None,
    ) -> None:
        self._max_events = max_events or config.sse_replay_events
        self._ttl_seconds = (
            ttl_seconds if ttl_seconds is not None else config.sse_replay_ttl_seconds
        )
        self._max_keys = max_keys or config.sse_replay_max_keys
        self._events: dict[str, deque[StoredEvent]] = defaultdict(
            lambda: deque(maxlen=self._max_events)
        )
        self._next_id: dict[str, int] = defaultdict(lambda: 1)
        # 每个 key 的最后发布时间；dict 保持插入序，publish 时将 key 移到末尾即 LRU 顺序
        self._last_published_at: dict[str, float] = {}
        # 正在真实执行 producer 的 key，用于并发重连去重
        self._active_producers: set[str] = set()
        self._lock = Lock()

    def publish(
        self,
        key: str,
        data: str,
        *,
        event: str = "message",
        terminal: bool = False,
    ) -> StoredEvent:
        with self._lock:
            stored = StoredEvent(
                event_id=self._next_id[key],
                event=event,
                data=data,
                terminal=terminal,
            )
            self._next_id[key] += 1
            self._events[key].append(stored)
            self._last_published_at.pop(key, None)
            self._last_published_at[key] = time.monotonic()
            self._evict_locked()
            return stored

    def replay(self, key: str, after_id: int = 0) -> list[StoredEvent]:
        with self._lock:
            self._evict_locked()
            events = self._events.get(key)
            if not events:
                return []
            return [event for event in events if event.event_id > after_id]

    def is_terminal(self, key: str) -> bool:
        with self._lock:
            self._evict_locked()
            return self._is_terminal_locked(key)

    def try_acquire(self, key: str) -> bool:
        """登记活跃 producer；同 key 已有活跃 producer 时返回 False。"""
        with self._lock:
            if key in self._active_producers:
                return False
            self._active_producers.add(key)
            return True

    def release(self, key: str) -> None:
        """释放活跃 producer 登记，允许后续重放或重新执行。"""
        with self._lock:
            self._active_producers.discard(key)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._next_id.clear()
            self._last_published_at.clear()
            self._active_producers.clear()

    def _is_terminal_locked(self, key: str) -> bool:
        events = self._events.get(key)
        return bool(events and events[-1].terminal)

    def _drop_locked(self, key: str) -> None:
        # 不清理 _active_producers：活跃 producer 仍在写入，登记由 release() 移除
        self._events.pop(key, None)
        self._next_id.pop(key, None)
        self._last_published_at.pop(key, None)

    def _evict_locked(self) -> None:
        """惰性驱逐：terminal 且超过 TTL 的 key 删除，总数超限按最久未使用驱逐。"""
        now = time.monotonic()
        for key, published_at in list(self._last_published_at.items()):
            if now - published_at > self._ttl_seconds and self._is_terminal_locked(key):
                self._drop_locked(key)
        while len(self._events) > self._max_keys:
            victim = next(
                (
                    key
                    for key in self._last_published_at
                    if key not in self._active_producers
                ),
                None,
            )
            if victim is None:
                break
            self._drop_locked(victim)


sse_replay_store = SSEReplayStore()
