"""Measure the same embedding workload on CPU and CUDA."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import torch
from sentence_transformers import SentenceTransformer

SAMPLES = [
    "CPU 使用率持续过高，检查进程、负载和限流策略。",
    "数据库连接池耗尽，检查慢查询和连接泄漏。",
    "消息队列积压，检查生产消费速率和重试队列。",
    "服务返回 503，检查健康状态、依赖和最近变更。",
] * 64


@dataclass
class DeviceResult:
    device: str
    samples: int
    batch_size: int
    rounds: int
    average_ms: float
    p95_ms: float
    samples_per_second: float


def _measure(device: str, model_name: str, batch_size: int, rounds: int) -> DeviceResult:
    model = SentenceTransformer(model_name, device=device)
    model.encode(SAMPLES[:batch_size], batch_size=batch_size, show_progress_bar=False)
    latencies: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        model.encode(SAMPLES, batch_size=batch_size, show_progress_bar=False)
        if device == "cuda":
            torch.cuda.synchronize()
        latencies.append((time.perf_counter() - started) * 1000)
    ordered = sorted(latencies)
    average = statistics.mean(latencies)
    return DeviceResult(
        device=device,
        samples=len(SAMPLES),
        batch_size=batch_size,
        rounds=rounds,
        average_ms=round(average, 2),
        p95_ms=round(ordered[-1], 2),
        samples_per_second=round(len(SAMPLES) / (average / 1000), 2),
    )


def run(model_name: str, output: Path, rounds: int) -> dict:
    devices = ["cpu"] + (["cuda"] if torch.cuda.is_available() else [])
    results = [_measure(device, model_name, 32, rounds) for device in devices]
    cpu = next(item for item in results if item.device == "cpu")
    cuda = next((item for item in results if item.device == "cuda"), None)
    payload = {
        "model": model_name,
        "gpu": torch.cuda.get_device_name(0) if cuda else None,
        "results": [asdict(item) for item in results],
        "cuda_speedup": round(
            cuda.samples_per_second / cpu.samples_per_second, 2
        ) if cuda else None,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/reliability/embedding_benchmark.json"),
    )
    args = parser.parse_args()
    print(json.dumps(run(args.model, args.output, args.rounds), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
