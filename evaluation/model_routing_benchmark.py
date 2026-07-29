"""Compare local, Flash, and Pro routes with an explicit routing rubric."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from app.config import config

CASES = [
    {
        "id": "http-503",
        "tier": "short_triage",
        "prompt": "服务持续返回 503。80字以内给出排查步骤，必须包含健康检查、下游依赖、最近变更。",
        "required": ["健康", "依赖", "变更"],
        "forbidden": ["直接重启所有"],
    },
    {
        "id": "db-pool",
        "tier": "short_triage",
        "prompt": "数据库连接池耗尽。80字以内回答，必须包含活跃连接、慢查询、限流。",
        "required": ["活跃连接", "慢查询", "限流"],
        "forbidden": ["删除数据"],
    },
    {
        "id": "queue-backlog",
        "tier": "short_triage",
        "prompt": "消息队列持续积压。80字以内回答，必须包含消费速率、重试、扩容。",
        "required": ["消费速率", "重试", "扩容"],
        "forbidden": ["清空队列"],
    },
    {
        "id": "redis-hotkey",
        "tier": "short_triage",
        "prompt": "Redis 单分片 CPU 100%。80字以内回答，必须包含热 Key、分片、降级。",
        "required": ["热", "分片", "降级"],
        "forbidden": ["FLUSHALL"],
    },
    {
        "id": "disk-full",
        "tier": "short_triage",
        "prompt": "节点磁盘使用率 99%。80字以内回答，必须包含 inode、日志、扩容。",
        "required": ["inode", "日志", "扩容"],
        "forbidden": ["rm -rf"],
    },
    {
        "id": "latency-spike",
        "tier": "short_triage",
        "prompt": "接口 P95 突增。80字以内回答，必须包含 Trace、依赖、回滚。",
        "required": ["Trace", "依赖", "回滚"],
        "forbidden": ["关闭监控"],
    },
    {
        "id": "multi-signal",
        "tier": "complex_report",
        "prompt": (
            "同一时段出现接口 5xx 上升、数据库慢查询和发布事件。请按证据优先级给出"
            "诊断与止损方案，必须包含时间线、因果假设、回滚条件、验证指标。"
        ),
        "required": ["时间线", "假设", "回滚", "验证"],
        "forbidden": ["一定是"],
    },
    {
        "id": "partial-failure",
        "tier": "complex_report",
        "prompt": (
            "日志服务不可达，但监控显示 CPU 正常、队列积压。请给出可审计的诊断报告，"
            "必须包含证据缺失、替代证据、止损、恢复验证。"
        ),
        "required": ["证据缺失", "替代", "止损", "验证"],
        "forbidden": ["确认根因"],
    },
    {
        "id": "overload-recovery",
        "tier": "complex_report",
        "prompt": (
            "服务因突发流量开始 429，依赖 P95 同时升高。请设计恢复步骤，必须包含准入控制、"
            "熔断半开、错误预算、退出条件。"
        ),
        "required": ["准入", "半开", "错误预算", "退出"],
        "forbidden": ["无限重试"],
    },
]

PRICES_USD_PER_MILLION = {
    "deepseek-v4-flash": {"input": 0.14, "output": 0.28},
    "deepseek-v4-pro": {"input": 0.435, "output": 0.87},
}


@dataclass
class CaseResult:
    case_id: str
    tier: str
    success: bool
    matched_keywords: list[str]
    missing_keywords: list[str]
    forbidden_hits: list[str]
    keyword_recall: float
    latency_ms: float
    input_tokens: int
    output_tokens: int
    error: str | None


@dataclass
class ModelResult:
    route: str
    model: str
    thinking: str
    requests: int
    successful_requests: int
    case_pass_rate: float
    keyword_recall: float
    safety_pass_rate: float
    short_triage_pass_rate: float
    complex_report_pass_rate: float
    average_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    cases: list[CaseResult]


def _rate(values: list[bool]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


async def _benchmark(
    route: str,
    model: str,
    client: AsyncOpenAI,
    *,
    thinking: str,
    max_tokens: int,
    extra_body: dict[str, Any],
    cases: list[dict[str, Any]] | None = None,
) -> ModelResult:
    selected_cases = cases or CASES
    case_results: list[CaseResult] = []
    for case in selected_cases:
        started = time.perf_counter()
        error: str | None = None
        text = ""
        prompt_tokens = 0
        completion_tokens = 0
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": case["prompt"]}],
                temperature=0,
                max_tokens=max_tokens,
                extra_body=extra_body,
            )
            text = response.choices[0].message.content or ""
            if response.usage:
                prompt_tokens = response.usage.prompt_tokens
                completion_tokens = response.usage.completion_tokens
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        matched = [keyword for keyword in case["required"] if keyword.lower() in text.lower()]
        missing = [keyword for keyword in case["required"] if keyword.lower() not in text.lower()]
        forbidden_hits = [phrase for phrase in case["forbidden"] if phrase.lower() in text.lower()]
        case_results.append(
            CaseResult(
                case_id=case["id"],
                tier=case["tier"],
                success=error is None,
                matched_keywords=matched,
                missing_keywords=missing,
                forbidden_hits=forbidden_hits,
                keyword_recall=round(len(matched) / len(case["required"]), 4),
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                error=error,
            )
        )

    latencies = [item.latency_ms for item in case_results]
    ordered = sorted(latencies)
    input_tokens = sum(item.input_tokens for item in case_results)
    output_tokens = sum(item.output_tokens for item in case_results)
    price = PRICES_USD_PER_MILLION.get(
        model,
        {"input": 0.0, "output": 0.0},
    )
    cost = (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000
    passed = [
        item.success and not item.missing_keywords and not item.forbidden_hits
        for item in case_results
    ]
    short_passed = [
        passed[index] for index, item in enumerate(case_results) if item.tier == "short_triage"
    ]
    complex_passed = [
        passed[index] for index, item in enumerate(case_results) if item.tier == "complex_report"
    ]
    return ModelResult(
        route=route,
        model=model,
        thinking=thinking,
        requests=len(selected_cases),
        successful_requests=sum(item.success for item in case_results),
        case_pass_rate=_rate(passed),
        keyword_recall=round(
            sum(item.keyword_recall for item in case_results) / len(case_results),
            4,
        ),
        safety_pass_rate=_rate([not item.forbidden_hits for item in case_results]),
        short_triage_pass_rate=_rate(short_passed),
        complex_report_pass_rate=_rate(complex_passed),
        average_latency_ms=round(statistics.mean(latencies), 2),
        p50_latency_ms=round(statistics.median(latencies), 2),
        p95_latency_ms=round(ordered[-1], 2),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=round(cost, 8),
        cases=case_results,
    )


def _routing_decision(results: list[ModelResult]) -> dict[str, Any]:
    by_route = {result.route: result for result in results}
    flash = by_route["flash"]
    pro = by_route["pro"]
    local = by_route["local"]
    short_route = (
        "flash"
        if flash.short_triage_pass_rate >= local.short_triage_pass_rate
        and flash.p95_latency_ms < local.p95_latency_ms
        else "local"
    )
    complex_route = (
        "pro"
        if pro.complex_report_pass_rate > flash.complex_report_pass_rate
        else "not_demonstrated"
    )
    return {
        "short_triage_route": short_route,
        "short_triage_reason": (
            "choose the route with no lower short-case pass rate and lower measured P95"
        ),
        "complex_report_route": complex_route,
        "complex_report_reason": (
            "Pro is selected only when its complex-case pass rate exceeds Flash; "
            "otherwise this small automated rubric does not justify the premium route"
        ),
        "manual_review_required": (
            "Report coherence and root-cause quality require blinded human review; "
            "keyword recall is only a contract check."
        ),
    }


async def run(output: Path) -> list[ModelResult]:
    local_cases = [CASES[0], CASES[3], CASES[6]]
    pro_cases = [case for case in CASES if case["tier"] == "complex_report"]
    routes = [
        _benchmark(
            "local",
            "qwen3:8b",
            AsyncOpenAI(
                base_url="http://127.0.0.1:11434/v1",
                api_key="ollama",
                timeout=180,
            ),
            thinking="disabled",
            max_tokens=120,
            extra_body={
                "think": False,
                "reasoning": {"effort": "none"},
            },
            cases=local_cases,
        ),
        _benchmark(
            "flash",
            "deepseek-v4-flash",
            AsyncOpenAI(
                base_url=config.llm_api_base,
                api_key=config.llm_api_key,
                timeout=180,
            ),
            thinking="disabled",
            max_tokens=320,
            extra_body={"thinking": {"type": "disabled"}},
        ),
        _benchmark(
            "pro",
            "deepseek-v4-pro",
            AsyncOpenAI(
                base_url=config.llm_api_base,
                api_key=config.llm_api_key,
                timeout=180,
            ),
            thinking="enabled",
            max_tokens=2400,
            extra_body={"thinking": {"type": "enabled"}},
            cases=pro_cases,
        ),
    ]
    results = list(await asyncio.gather(*routes))
    payload = {
        "pricing_source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "pricing_basis": "cache-miss input and output USD per 1M tokens",
        "rubric": {
            "cases": len(CASES),
            "short_triage": 6,
            "complex_report": 3,
            "route_samples": {
                "local": len(local_cases),
                "flash": len(CASES),
                "pro": len(pro_cases),
            },
            "case_pass": "all required keywords present, no forbidden unsafe phrase",
            "boundary": (
                "automated contract rubric; not human-rated correctness or production accuracy"
            ),
        },
        "routing_decision": _routing_decision(results),
        "results": [asdict(item) for item in results],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/reliability/model_routing_benchmark.json"),
    )
    args = parser.parse_args()
    loop_factory = asyncio.SelectorEventLoop if hasattr(asyncio, "SelectorEventLoop") else None
    results = asyncio.run(run(args.output), loop_factory=loop_factory)
    print(
        json.dumps(
            [
                {key: value for key, value in asdict(item).items() if key != "cases"}
                for item in results
            ],
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
