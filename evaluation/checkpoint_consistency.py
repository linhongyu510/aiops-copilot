"""Verify that two graph replicas observe the same PostgreSQL checkpoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph


class ReplicaState(TypedDict):
    value: str
    writer: str


def _graph(checkpointer):
    builder = StateGraph(ReplicaState)
    builder.add_node("persist", lambda state: state)
    builder.add_edge(START, "persist")
    builder.add_edge("persist", END)
    return builder.compile(checkpointer=checkpointer)


async def verify(dsn: str, output: Path) -> dict:
    thread_id = f"replica-consistency-{uuid.uuid4().hex}"
    config = {"configurable": {"thread_id": thread_id}}
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver_a:
        await saver_a.setup()
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver_b:
            graph_a = _graph(saver_a)
            graph_b = _graph(saver_b)
            await graph_a.ainvoke(
                {"value": "durable-checkpoint", "writer": "replica-a"}, config
            )
            observed = await graph_b.aget_state(config)
            consistent = observed.values == {
                "value": "durable-checkpoint",
                "writer": "replica-a",
            }
            await saver_a.adelete_thread(thread_id)
    result = {
        "backend": "postgres",
        "thread_id": thread_id,
        "replica_a_write": "durable-checkpoint",
        "replica_b_observed": observed.values,
        "consistent": consistent,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not consistent:
        raise RuntimeError("replica checkpoint consistency failed")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dsn",
        default="postgresql://aiops:aiops@127.0.0.1:55432/aiops",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/reliability/checkpoint_consistency.json"),
    )
    args = parser.parse_args()
    loop_factory = asyncio.SelectorEventLoop if hasattr(asyncio, "SelectorEventLoop") else None
    print(
        json.dumps(
            asyncio.run(verify(args.dsn, args.output), loop_factory=loop_factory),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
