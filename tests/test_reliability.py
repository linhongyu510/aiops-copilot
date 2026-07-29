import asyncio

import pytest

from app.reliability import (
    BulkheadFullError,
    CircuitOpenError,
    DependencyGuard,
)
from app.sse import SSEReplayStore


def _guard(**overrides) -> DependencyGuard:
    settings = {
        "max_concurrency": 1,
        "queue_timeout_seconds": 0.01,
        "failure_threshold": 2,
        "recovery_timeout_seconds": 0.02,
    }
    settings.update(overrides)
    return DependencyGuard("fixture", **settings)


@pytest.mark.asyncio
async def test_circuit_opens_then_recovers_through_half_open_probe() -> None:
    guard = _guard()

    async def fail():
        raise TimeoutError("dependency timeout")

    with pytest.raises(TimeoutError):
        await guard.call(fail)
    with pytest.raises(TimeoutError):
        await guard.call(fail)
    assert guard.snapshot().state == "open"
    with pytest.raises(CircuitOpenError):
        await guard.call(fail)

    await asyncio.sleep(0.03)
    assert await guard.call(lambda: asyncio.sleep(0, result="ok")) == "ok"
    assert guard.snapshot().state == "closed"


@pytest.mark.asyncio
async def test_bulkhead_rejects_waiter_without_leaking_permit() -> None:
    guard = _guard()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        entered.set()
        await release.wait()
        return "done"

    task = asyncio.create_task(guard.call(slow))
    await entered.wait()
    with pytest.raises(BulkheadFullError):
        await guard.call(lambda: asyncio.sleep(0, result="late"))
    release.set()
    assert await task == "done"
    assert guard.snapshot().in_flight == 0
    assert guard.snapshot().rejected == 1


@pytest.mark.asyncio
async def test_validation_error_does_not_open_dependency_circuit() -> None:
    guard = _guard(failure_threshold=1)

    async def invalid():
        raise ValueError("caller bug")

    with pytest.raises(ValueError):
        await guard.call(
            invalid,
            exception_failure_predicate=lambda exc: not isinstance(exc, ValueError),
        )
    assert guard.snapshot().state == "closed"


def test_sse_replay_cursor_and_terminal_idempotency() -> None:
    store = SSEReplayStore(max_events=3)
    first = store.publish("session:key", '{"type":"content","data":"a"}')
    second = store.publish("session:key", '{"type":"content","data":"b"}')
    final = store.publish(
        "session:key", '{"type":"done"}', terminal=True
    )

    assert [event.event_id for event in store.replay("session:key", first.event_id)] == [
        second.event_id,
        final.event_id,
    ]
    assert store.is_terminal("session:key") is True
    assert final.as_sse()["retry"] == 3000


def test_sse_buffer_is_bounded() -> None:
    store = SSEReplayStore(max_events=2)
    for index in range(4):
        store.publish("stream", str(index))
    assert [item.data for item in store.replay("stream")] == ["2", "3"]
