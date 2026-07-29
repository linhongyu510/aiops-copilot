"""Compare local, Flash and Pro routes on a small deterministic rubric."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from openai import AsyncOpenAI

from app.config import config

CASES = [
    {
        "prompt": "服务持续返回 503，回答中列出先查健康检查、下游依赖和最近变更，80字以内。",
        "required": ["健康", "依赖", "变更"],
    },
    {
        "prompt": "数据库连接池耗尽，回答中必须包含活跃连接、慢查询和限流，80字以内。",
        "required": ["连接", "慢查询", "限流"],
    },
    {
        "prompt": "消息队列积压，回答中必须包含消费速率、重试和扩容，80字以内。",
        "required": ["消费", "重试", "扩容"],
    },
]

PRICES_USD_PER_MILLION = {
    "deepseek-v4-flash": {"input": 0.14, "output": 0.28},
    "deepseek-v4-pro": {"input": 0.435, "output": 0.87},
}


@dataclass
class ModelResult:
    route: str
    model: str
    requests: int
    successful_requests: int
    rubric_accuracy: float
    average_latency_ms: float
    p95_latency_ms: float
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    error: str | None = None


async def _benchmark(
    route: str,
    model: str,
    client: AsyncOpenAI,
) -> ModelResult:
    latencies: list[float] = []
    passed = 0
    succeeded = 0
    input_tokens = 0
    output_tokens = 0
    last_error = None
    for case in CASES:
        started = time.perf_counter()
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": case["prompt"]}],
                temperature=0,
                max_tokens=200,
            )
            text = response.choices[0].message.content or ""
            succeeded += 1
            passed += int(all(keyword in text for keyword in case["required"]))
            if response.usage:
                input_tokens += response.usage.prompt_tokens
                output_tokens += response.usage.completion_tokens
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        latencies.append((time.perf_counter() - started) * 1000)
    ordered = sorted(latencies)
    price = PRICES_USD_PER_MILLION.get(model, {"input": 0.0, "output": 0.0})
    cost = (
        input_tokens * price["input"] + output_tokens * price["output"]
    ) / 1_000_000
    return ModelResult(
        route=route,
        model=model,
        requests=len(CASES),
        successful_requests=succeeded,
        rubric_accuracy=round(passed / len(CASES), 4),
        average_latency_ms=round(sum(latencies) / len(latencies), 2),
        p95_latency_ms=round(ordered[-1], 2),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=round(cost, 8),
        error=last_error,
    )


async def run(output: Path) -> list[ModelResult]:
    routes = [
        (
            "local",
            "qwen3:8b",
            AsyncOpenAI(base_url="http://127.0.0.1:11434/v1", api_key="ollama"),
        ),
        (
            "flash",
            "deepseek-v4-flash",
            AsyncOpenAI(base_url=config.llm_api_base, api_key=config.llm_api_key),
        ),
        (
            "pro",
            "deepseek-v4-pro",
            AsyncOpenAI(base_url=config.llm_api_base, api_key=config.llm_api_key),
        ),
    ]
    results = [await _benchmark(*route) for route in routes]
    payload = {
        "pricing_source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "pricing_basis": "cache-miss input and output USD per 1M tokens",
        "rubric": "3 deterministic keyword-completeness cases; not a production accuracy claim",
        "results": [asdict(item) for item in results],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/reliability/model_routing_benchmark.json"),
    )
    args = parser.parse_args()
    print(json.dumps([asdict(item) for item in asyncio.run(run(args.output))], indent=2))


if __name__ == "__main__":
    main()
