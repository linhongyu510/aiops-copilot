"""Verify cross-instance SSE replay, idempotency, and admission control."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

from redis.asyncio import Redis

from app.capacity import AgentCapacityError
from app.coordination import RedisCapacityBackend, RedisReplayBackend


async def verify(redis_url: str, output: Path) -> dict:
    prefix = f"verification:{uuid.uuid4().hex}"
    client_a = Redis.from_url(redis_url, decode_responses=True)
    client_b = Redis.from_url(redis_url, decode_responses=True)
    replay_a = RedisReplayBackend(client_a, prefix)
    replay_b = RedisReplayBackend(client_b, prefix)
    capacity_a = RedisCapacityBackend(
        client_a,
        prefix,
        limit=1,
        queue_timeout_seconds=0.05,
    )
    capacity_b = RedisCapacityBackend(
        client_b,
        prefix,
        limit=1,
        queue_timeout_seconds=0.05,
    )
    operation = "shared-operation"
    producer_lease: str | None = None
    capacity_lease: str | None = None
    second_capacity_lease: str | None = None
    started_at = time.time()

    try:
        producer_lease = await replay_a.try_acquire(operation)
        duplicate_producer_rejected = await replay_b.try_acquire(operation) is None
        first = await replay_a.publish(operation, '{"type":"content","data":"A"}')
        terminal = await replay_a.publish(
            operation,
            '{"type":"done","data":"complete"}',
            terminal=True,
        )
        replica_b_events = await replay_b.replay(operation, first.event_id)
        replica_b_terminal = await replay_b.is_terminal(operation)

        capacity_lease = await capacity_a.acquire()
        overload_rejected = False
        try:
            await capacity_b.acquire()
        except AgentCapacityError:
            overload_rejected = True
        await capacity_a.release(capacity_lease)
        capacity_lease = None
        second_capacity_lease = await capacity_b.acquire()

        await replay_a.release(operation, producer_lease or "")
        producer_lease = None
        producer_reacquired = bool(await replay_b.try_acquire(operation))

        result = {
            "backend": "redis",
            "redis_url": redis_url,
            "replicas": 2,
            "duplicate_producer_rejected": duplicate_producer_rejected,
            "replica_b_event_ids_after_cursor": [event.event_id for event in replica_b_events],
            "expected_event_ids_after_cursor": [terminal.event_id],
            "replica_b_observed_terminal": replica_b_terminal,
            "overload_rejected_across_replicas": overload_rejected,
            "capacity_recovered_after_release": second_capacity_lease is not None,
            "producer_recovered_after_release": producer_reacquired,
            "duration_ms": round((time.time() - started_at) * 1000, 2),
        }
        result["passed"] = all(
            (
                producer_lease is None,
                duplicate_producer_rejected,
                result["replica_b_event_ids_after_cursor"]
                == result["expected_event_ids_after_cursor"],
                replica_b_terminal,
                overload_rejected,
                second_capacity_lease is not None,
                producer_reacquired,
            )
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if not result["passed"]:
            raise RuntimeError("Redis coordination verification failed")
        return result
    finally:
        if producer_lease is not None:
            await replay_a.release(operation, producer_lease)
        if capacity_lease is not None:
            await capacity_a.release(capacity_lease)
        if second_capacity_lease is not None:
            await capacity_b.release(second_capacity_lease)
        keys = [key async for key in client_a.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client_a.delete(*keys)
        await client_a.aclose()
        await client_b.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6389/0")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/reliability/redis_coordination.json"),
    )
    args = parser.parse_args()
    loop_factory = asyncio.SelectorEventLoop if hasattr(asyncio, "SelectorEventLoop") else None
    print(
        json.dumps(
            asyncio.run(
                verify(args.redis_url, args.output),
                loop_factory=loop_factory,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
