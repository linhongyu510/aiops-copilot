"""Real HTTP/SSE admission-control benchmark against /api/chat_stream."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MethodType
from typing import Any

import httpx
import matplotlib.pyplot as plt
import psutil
import uvicorn

from app.api import chat
from app.capacity import AgentCapacityLimiter
from app.config import config
from app.coordination import (
    MemoryCapacityBackend,
    MemoryReplayBackend,
    coordination_runtime,
)
from app.main import app
from app.sse import SSEReplayStore


@dataclass
class BenchmarkRow:
    concurrency: int
    requests: int
    accepted: int
    rejected: int
    downstream_errors: int
    rejection_rate: float
    downstream_error_rate: float
    throughput_rps: float
    p50_ms: float
    p95_ms: float
    accepted_p50_ms: float
    accepted_p95_ms: float
    memory_delta_mb: float
    sse_events: int


@dataclass
class RequestResult:
    status_code: int
    latency_ms: float
    downstream_error: bool
    sse_events: int


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile) - 1))
    return ordered[index]


def _parse_sse(body: str) -> tuple[int, bool]:
    events = 0
    downstream_error = False
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        events += 1
        try:
            payload = json.loads(line.removeprefix("data:").strip())
        except json.JSONDecodeError:
            continue
        downstream_error = downstream_error or payload.get("type") == "error"
    return events, downstream_error


async def _run_level(
    client: httpx.AsyncClient,
    concurrency: int,
    waves: int,
) -> BenchmarkRow:
    process = psutil.Process()
    memory_before = process.memory_info().rss
    results: list[RequestResult] = []
    started = time.perf_counter()

    async def one_request(request_number: int) -> RequestResult:
        request_started = time.perf_counter()
        operation_id = uuid.uuid4().hex
        async with client.stream(
            "POST",
            "/api/chat_stream",
            headers={"X-Idempotency-Key": operation_id},
            json={
                "id": f"benchmark-{operation_id}",
                "question": f"benchmark:{request_number}",
            },
        ) as response:
            body = (await response.aread()).decode("utf-8")
        sse_events, downstream_error = _parse_sse(body)
        return RequestResult(
            status_code=response.status_code,
            latency_ms=(time.perf_counter() - request_started) * 1000,
            downstream_error=downstream_error,
            sse_events=sse_events,
        )

    for wave in range(waves):
        offset = wave * concurrency
        results.extend(
            await asyncio.gather(*(one_request(offset + index + 1) for index in range(concurrency)))
        )

    elapsed = time.perf_counter() - started
    accepted_results = [result for result in results if result.status_code == 200]
    rejected = sum(result.status_code == 429 for result in results)
    unexpected = [result.status_code for result in results if result.status_code not in {200, 429}]
    if unexpected:
        raise RuntimeError(f"unexpected HTTP statuses: {unexpected}")
    downstream_errors = sum(result.downstream_error for result in accepted_results)
    latencies = [result.latency_ms for result in results]
    accepted_latencies = [result.latency_ms for result in accepted_results]
    memory_after = process.memory_info().rss
    total = len(results)
    accepted = len(accepted_results)
    return BenchmarkRow(
        concurrency=concurrency,
        requests=total,
        accepted=accepted,
        rejected=rejected,
        downstream_errors=downstream_errors,
        rejection_rate=round(rejected / total, 4),
        downstream_error_rate=round(
            downstream_errors / accepted if accepted else 0.0,
            4,
        ),
        throughput_rps=round(total / elapsed, 2),
        p50_ms=round(statistics.median(latencies), 2),
        p95_ms=round(_percentile(latencies, 0.95), 2),
        accepted_p50_ms=round(
            statistics.median(accepted_latencies) if accepted_latencies else 0.0,
            2,
        ),
        accepted_p95_ms=round(
            _percentile(accepted_latencies, 0.95),
            2,
        ),
        memory_delta_mb=round(
            (memory_after - memory_before) / 1024 / 1024,
            3,
        ),
        sse_events=sum(result.sse_events for result in accepted_results),
    )


def _write_chart(rows: list[BenchmarkRow], path: Path) -> None:
    levels = [row.concurrency for row in rows]
    fig, latency_axis = plt.subplots(figsize=(9.2, 5.4), dpi=160)
    rate_axis = latency_axis.twinx()
    latency_axis.plot(
        levels,
        [row.accepted_p95_ms for row in rows],
        marker="o",
        linewidth=2.2,
        color="#d8a24a",
        label="Accepted P95 latency",
    )
    rate_axis.bar(
        [level - 0.22 for level in levels],
        [row.rejection_rate * 100 for row in rows],
        width=0.44,
        alpha=0.42,
        color="#44c2d6",
        label="HTTP 429 ratio",
    )
    rate_axis.bar(
        [level + 0.22 for level in levels],
        [row.downstream_error_rate * 100 for row in rows],
        width=0.44,
        alpha=0.42,
        color="#ef6a78",
        label="Downstream error ratio",
    )
    latency_axis.set_xlabel("Concurrent HTTP/SSE requests")
    latency_axis.set_ylabel("Accepted request P95 (ms)")
    rate_axis.set_ylabel("Request ratio (%)")
    latency_axis.set_xticks(levels)
    latency_axis.grid(alpha=0.2)
    handles_a, labels_a = latency_axis.get_legend_handles_labels()
    handles_b, labels_b = rate_axis.get_legend_handles_labels()
    latency_axis.legend(
        handles_a + handles_b,
        labels_a + labels_b,
        loc="upper left",
    )
    fig.suptitle("AIOps /api/chat_stream overload behavior")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


async def run(
    output_dir: Path,
    *,
    waves: int = 8,
    service_ms: float = 80,
    host: str = "127.0.0.1",
    port: int = 9918,
) -> list[BenchmarkRow]:
    output_dir.mkdir(parents=True, exist_ok=True)
    original_query_stream = chat.rag_agent_service.query_stream

    async def deterministic_query_stream(
        _service: Any,
        question: str,
        *,
        session_id: str,
    ):
        del session_id
        request_number = int(question.rsplit(":", 1)[-1])
        await asyncio.sleep(service_ms / 1000)
        if request_number % 20 == 0:
            yield {"type": "error", "data": "synthetic_downstream_error"}
        else:
            yield {
                "type": "complete",
                "data": {"answer": "benchmark-complete"},
            }

    chat.rag_agent_service.query_stream = MethodType(
        deterministic_query_stream,
        chat.rag_agent_service,
    )
    config.checkpoint_backend = "memory"
    config.coordination_backend = "memory"
    config.otel_enabled = False
    coordination_runtime.replay = MemoryReplayBackend(SSEReplayStore())
    coordination_runtime.capacity = MemoryCapacityBackend(
        AgentCapacityLimiter(limit=8, queue_timeout_seconds=0.02)
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    server_task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            if server_task.done():
                await server_task
            await asyncio.sleep(0.025)
        if not server.started:
            raise RuntimeError("benchmark server did not start")

        async with httpx.AsyncClient(
            base_url=f"http://{host}:{port}",
            timeout=30.0,
        ) as client:
            rows = [await _run_level(client, level, waves) for level in (1, 4, 8, 16)]
    finally:
        server.should_exit = True
        await server_task
        chat.rag_agent_service.query_stream = original_query_stream

    payload = {
        "method": "real_http_sse",
        "endpoint": "/api/chat_stream",
        "capacity": 8,
        "queue_timeout_ms": 20,
        "service_time_ms": service_ms,
        "waves": waves,
        "rows": [asdict(row) for row in rows],
    }
    (output_dir / "load_benchmark.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (output_dir / "load_benchmark.csv").open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.DictWriter(file, fieldnames=list(asdict(rows[0])))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
    _write_chart(rows, output_dir / "load_benchmark.png")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/reliability"),
    )
    parser.add_argument("--waves", type=int, default=8)
    parser.add_argument("--service-ms", type=float, default=80)
    parser.add_argument("--port", type=int, default=9918)
    args = parser.parse_args()
    loop_factory = asyncio.SelectorEventLoop if hasattr(asyncio, "SelectorEventLoop") else None
    rows = asyncio.run(
        run(
            args.output_dir,
            waves=args.waves,
            service_ms=args.service_ms,
            port=args.port,
        ),
        loop_factory=loop_factory,
    )
    print(json.dumps([asdict(row) for row in rows], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
