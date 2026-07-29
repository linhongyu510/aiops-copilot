"""Dependency circuit breakers and isolation bulkheads."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from app.config import config

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    pass


class BulkheadFullError(RuntimeError):
    pass


@dataclass(frozen=True)
class GuardSnapshot:
    name: str
    state: str
    failures: int
    in_flight: int
    rejected: int


class DependencyGuard:
    def __init__(
        self,
        name: str,
        *,
        max_concurrency: int,
        queue_timeout_seconds: float,
        failure_threshold: int,
        recovery_timeout_seconds: float,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout_seconds = max(0.01, recovery_timeout_seconds)
        self.queue_timeout_seconds = max(0.01, queue_timeout_seconds)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_probe = False
        self._in_flight = 0
        self._rejected = 0
        self._lock = asyncio.Lock()

    async def _before_call(self) -> None:
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._opened_at < self.recovery_timeout_seconds:
                    self._rejected += 1
                    raise CircuitOpenError(f"circuit_open:{self.name}")
                self._state = CircuitState.HALF_OPEN
                self._half_open_probe = False
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_probe:
                    self._rejected += 1
                    raise CircuitOpenError(f"half_open_probe_in_progress:{self.name}")
                self._half_open_probe = True

    async def _record(self, failed: bool) -> None:
        async with self._lock:
            if failed:
                self._failures += 1
                if (
                    self._state == CircuitState.HALF_OPEN
                    or self._failures >= self.failure_threshold
                ):
                    self._state = CircuitState.OPEN
                    self._opened_at = time.monotonic()
                    self._half_open_probe = False
            else:
                self._failures = 0
                self._state = CircuitState.CLOSED
                self._half_open_probe = False

    async def call(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        failure_predicate: Callable[[T], bool] | None = None,
        exception_failure_predicate: Callable[[Exception], bool] | None = None,
    ) -> T:
        await self._before_call()
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self.queue_timeout_seconds
            )
        except TimeoutError as exc:
            async with self._lock:
                self._rejected += 1
                if self._state == CircuitState.HALF_OPEN:
                    self._half_open_probe = False
            raise BulkheadFullError(f"bulkhead_full:{self.name}") from exc

        self._in_flight += 1
        try:
            result = await operation()
            await self._record(bool(failure_predicate and failure_predicate(result)))
            return result
        except Exception as exc:
            failed = (
                exception_failure_predicate(exc)
                if exception_failure_predicate is not None
                else True
            )
            await self._record(failed)
            raise
        finally:
            self._in_flight = max(0, self._in_flight - 1)
            self._semaphore.release()

    def snapshot(self) -> GuardSnapshot:
        return GuardSnapshot(
            name=self.name,
            state=self._state.value,
            failures=self._failures,
            in_flight=self._in_flight,
            rejected=self._rejected,
        )


class DependencyGuardRegistry:
    def __init__(self) -> None:
        self._guards: dict[str, DependencyGuard] = {}

    def get(self, name: str) -> DependencyGuard:
        if name not in self._guards:
            self._guards[name] = DependencyGuard(
                name,
                max_concurrency=config.dependency_max_concurrency,
                queue_timeout_seconds=config.dependency_queue_timeout_seconds,
                failure_threshold=config.circuit_failure_threshold,
                recovery_timeout_seconds=config.circuit_recovery_timeout_seconds,
            )
        return self._guards[name]

    def snapshots(self) -> list[dict[str, Any]]:
        return [guard.snapshot().__dict__ for guard in self._guards.values()]

    def reset(self) -> None:
        self._guards.clear()


dependency_guards = DependencyGuardRegistry()
