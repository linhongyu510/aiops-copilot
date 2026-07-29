"""Deterministic admission-control benchmark with chart output."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import psutil

from app.capacity import AgentCapacityError, AgentCapacityLimiter


@dataclass
class BenchmarkRow:
    concurrency: int
    requests: int
    accepted: int
    rejected: int
    downstream_errors: int
    rejection_rate: float
    throughput_rps: float
    p50_ms: float
    p95_ms: float
    memory_delta_mb: float


async def _run_level(concurrency: int, waves: int, service_ms: float) -> BenchmarkRow:
    limiter = AgentCapacityLimiter(limit=8, queue_timeout_seconds=0.02)
    latencies: list[float] = []
    rejected = 0
    downstream_errors = 0
    process = psutil.Process()
    memory_before = process.memory_info().rss
    sequence = 0
    lock = asyncio.Lock()

    async def one_request() -> None:
        nonlocal rejected, downstream_errors, sequence
        started = time.perf_counter()
        acquired = False
        try:
            await limiter.acquire()
            acquired = True
            async with lock:
                sequence += 1
                current = sequence
            await asyncio.sleep(service_ms / 1000)
            if current % 20 == 0:
                downstream_errors += 1
        except AgentCapacityError:
            rejected += 1
        finally:
            if acquired:
                limiter.release()
            latencies.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    for _ in range(waves):
        await asyncio.gather(*(one_request() for _ in range(concurrency)))
    elapsed = time.perf_counter() - started
    total = concurrency * waves
    accepted = total - rejected
    ordered = sorted(latencies)
    p50 = statistics.median(ordered)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    memory_after = process.memory_info().rss
    return BenchmarkRow(
        concurrency=concurrency,
        requests=total,
        accepted=accepted,
        rejected=rejected,
        downstream_errors=downstream_errors,
        rejection_rate=round(rejected / total, 4),
        throughput_rps=round(total / elapsed, 2),
        p50_ms=round(p50, 2),
        p95_ms=round(p95, 2),
        memory_delta_mb=round((memory_after - memory_before) / 1024 / 1024, 3),
    )


def _write_chart(rows: list[BenchmarkRow], path: Path) -> None:
    levels = [row.concurrency for row in rows]
    fig, latency_axis = plt.subplots(figsize=(9, 5.2), dpi=160)
    rejection_axis = latency_axis.twinx()
    latency_axis.plot(levels, [row.p95_ms for row in rows], marker="o", label="P95 latency")
    rejection_axis.bar(
        [level + 0.12 for level in levels],
        [row.rejection_rate * 100 for row in rows],
        width=0.5,
        alpha=0.28,
        color="#2c8f9e",
        label="429 rejection rate",
    )
    latency_axis.set_xlabel("Concurrent requests")
    latency_axis.set_ylabel("P95 latency (ms)")
    rejection_axis.set_ylabel("Rejection rate (%)")
    latency_axis.set_xticks(levels)
    latency_axis.grid(alpha=0.2)
    fig.suptitle("AIOps admission control: bounded latency under overload")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


async def run(output_dir: Path, waves: int = 8, service_ms: float = 80) -> list[BenchmarkRow]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        await _run_level(level, waves=waves, service_ms=service_ms)
        for level in (1, 4, 8, 16)
    ]
    (output_dir / "load_benchmark.json").write_text(
        json.dumps([asdict(row) for row in rows], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (output_dir / "load_benchmark.csv").open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(asdict(rows[0])))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
    _write_chart(rows, output_dir / "load_benchmark.png")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/reliability"))
    parser.add_argument("--waves", type=int, default=8)
    parser.add_argument("--service-ms", type=float, default=80)
    args = parser.parse_args()
    rows = asyncio.run(run(args.output_dir, args.waves, args.service_ms))
    print(json.dumps([asdict(row) for row in rows], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
