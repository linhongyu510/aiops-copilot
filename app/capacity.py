"""Bounded admission control for expensive Agent requests."""

from __future__ import annotations

import asyncio

from app.config import config


class AgentCapacityError(RuntimeError):
    pass


class AgentCapacityLimiter:
    def __init__(self, limit: int, queue_timeout_seconds: float) -> None:
        self.limit = max(1, int(limit))
        self.queue_timeout_seconds = max(0.01, float(queue_timeout_seconds))
        self._semaphore = asyncio.Semaphore(self.limit)

    async def acquire(self) -> None:
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self.queue_timeout_seconds,
            )
        except TimeoutError as exc:
            raise AgentCapacityError("agent_capacity_exhausted") from exc

    def release(self) -> None:
        self._semaphore.release()


agent_capacity = AgentCapacityLimiter(
    config.max_concurrent_agent_requests,
    config.request_queue_timeout_seconds,
)
