"""Thread-safe LLM call metrics: latency, token usage and estimated cost."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from math import ceil
from threading import Lock
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from app.config import config


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 2)


def _extract_usage(response: LLMResult) -> tuple[int, int]:
    """Extract (input_tokens, output_tokens), tolerating missing usage fields."""
    generations = response.generations or []
    if generations and generations[0]:
        message = getattr(generations[0][0], "message", None)
        usage = getattr(message, "usage_metadata", None)
        if isinstance(usage, dict):
            return (
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
            )
    llm_output = response.llm_output or {}
    token_usage = llm_output.get("token_usage") or {}
    return (
        int(token_usage.get("prompt_tokens") or 0),
        int(token_usage.get("completion_tokens") or 0),
    )


class LLMMetricsRegistry:
    def __init__(self, history_size: int = 1000) -> None:
        self._history_size = history_size
        self._lock = Lock()
        self._calls: dict[str, int] = defaultdict(int)
        self._successes: dict[str, int] = defaultdict(int)
        self._latencies: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self._history_size)
        )
        self._input_tokens: dict[str, int] = defaultdict(int)
        self._output_tokens: dict[str, int] = defaultdict(int)
        self._cost_usd: dict[str, float] = defaultdict(float)

    def record(
        self,
        model: str,
        success: bool,
        latency_ms: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        with self._lock:
            self._calls[model] += 1
            self._successes[model] += int(success)
            self._latencies[model].append(float(latency_ms))
            self._input_tokens[model] += int(input_tokens)
            self._output_tokens[model] += int(output_tokens)
            self._cost_usd[model] += float(cost_usd)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            names = sorted(self._calls)
            models: dict[str, Any] = {}
            total_calls = 0
            total_successes = 0
            total_input_tokens = 0
            total_output_tokens = 0
            total_cost_usd = 0.0
            all_latencies: list[float] = []
            for name in names:
                calls = self._calls[name]
                successes = self._successes[name]
                latencies = list(self._latencies[name])
                total_calls += calls
                total_successes += successes
                total_input_tokens += self._input_tokens[name]
                total_output_tokens += self._output_tokens[name]
                total_cost_usd += self._cost_usd[name]
                all_latencies.extend(latencies)
                models[name] = {
                    "calls": calls,
                    "successes": successes,
                    "failures": calls - successes,
                    "success_rate": round(successes / calls, 4) if calls else 0.0,
                    "average_latency_ms": round(sum(latencies) / len(latencies), 2)
                    if latencies
                    else 0.0,
                    "p50_latency_ms": _percentile(latencies, 0.50),
                    "p95_latency_ms": _percentile(latencies, 0.95),
                    "input_tokens": self._input_tokens[name],
                    "output_tokens": self._output_tokens[name],
                    "cost_usd": round(self._cost_usd[name], 6),
                }
            return {
                "total_calls": total_calls,
                "total_successes": total_successes,
                "total_failures": total_calls - total_successes,
                "success_rate": round(total_successes / total_calls, 4) if total_calls else 0.0,
                "average_latency_ms": round(sum(all_latencies) / len(all_latencies), 2)
                if all_latencies
                else 0.0,
                "p50_latency_ms": _percentile(all_latencies, 0.50),
                "p95_latency_ms": _percentile(all_latencies, 0.95),
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "total_cost_usd": round(total_cost_usd, 6),
                "models": models,
            }

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()
            self._successes.clear()
            self._latencies.clear()
            self._input_tokens.clear()
            self._output_tokens.clear()
            self._cost_usd.clear()

    @staticmethod
    def _escape_label(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def render_prometheus(self) -> str:
        snapshot = self.snapshot()
        lines = [
            "# HELP aiops_llm_calls_total LLM calls by model and outcome.",
            "# TYPE aiops_llm_calls_total counter",
        ]
        for name, item in snapshot["models"].items():
            safe_name = self._escape_label(name)
            lines.append(
                f'aiops_llm_calls_total{{model="{safe_name}",outcome="success"}} '
                f"{item['successes']}"
            )
            lines.append(
                f'aiops_llm_calls_total{{model="{safe_name}",outcome="failure"}} '
                f"{item['failures']}"
            )
        lines.extend(
            [
                "# HELP aiops_llm_latency_milliseconds LLM latency by quantile.",
                "# TYPE aiops_llm_latency_milliseconds gauge",
            ]
        )
        for name, item in snapshot["models"].items():
            safe_name = self._escape_label(name)
            lines.append(
                f'aiops_llm_latency_milliseconds{{model="{safe_name}",quantile="0.5"}} '
                f"{item['p50_latency_ms']}"
            )
            lines.append(
                f'aiops_llm_latency_milliseconds{{model="{safe_name}",quantile="0.95"}} '
                f"{item['p95_latency_ms']}"
            )
        lines.extend(
            [
                "# HELP aiops_llm_tokens_total LLM token usage by model and type.",
                "# TYPE aiops_llm_tokens_total counter",
                "# HELP aiops_llm_cost_usd_total Estimated LLM cost in USD by model.",
                "# TYPE aiops_llm_cost_usd_total counter",
            ]
        )
        for name, item in snapshot["models"].items():
            safe_name = self._escape_label(name)
            lines.append(
                f'aiops_llm_tokens_total{{model="{safe_name}",type="input"}} '
                f"{item['input_tokens']}"
            )
            lines.append(
                f'aiops_llm_tokens_total{{model="{safe_name}",type="output"}} '
                f"{item['output_tokens']}"
            )
            lines.append(
                f'aiops_llm_cost_usd_total{{model="{safe_name}"}} {item["cost_usd"]}'
            )
        return "\n".join(lines) + "\n"


def diff_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Diff two snapshot() results into per-interval totals (e.g. one diagnosis run)."""
    return {
        "calls": after["total_calls"] - before["total_calls"],
        "failures": after["total_failures"] - before["total_failures"],
        "input_tokens": after["total_input_tokens"] - before["total_input_tokens"],
        "output_tokens": after["total_output_tokens"] - before["total_output_tokens"],
        "cost_usd": round(after["total_cost_usd"] - before["total_cost_usd"], 6),
    }


class LLMMetricsCallback(BaseCallbackHandler):
    """Record per-model LLM metrics without touching prompt or response content."""

    def __init__(
        self,
        model_name: str,
        registry: LLMMetricsRegistry | None = None,
    ) -> None:
        self._model_name = model_name
        self._registry = registry or llm_metrics
        self._lock = Lock()
        self._in_flight: dict[UUID, float] = {}

    def _mark_start(self, run_id: UUID) -> None:
        with self._lock:
            self._in_flight[run_id] = time.perf_counter()

    def _elapsed_ms(self, run_id: UUID) -> float:
        with self._lock:
            started = self._in_flight.pop(run_id, None)
        if started is None:
            return 0.0
        return (time.perf_counter() - started) * 1000

    @staticmethod
    def _estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
        """Zero-cost default: prices stay 0 until configured, so cost is not counted."""
        input_price = config.llm_price_input_per_million
        output_price = config.llm_price_output_per_million
        return (input_tokens * input_price + output_tokens * output_price) / 1_000_000

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self._mark_start(run_id)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self._mark_start(run_id)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        latency_ms = self._elapsed_ms(run_id)
        input_tokens, output_tokens = _extract_usage(response)
        cost_usd = self._estimate_cost_usd(input_tokens, output_tokens)
        self._registry.record(
            self._model_name,
            True,
            latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        latency_ms = self._elapsed_ms(run_id)
        self._registry.record(self._model_name, False, latency_ms)


llm_metrics = LLMMetricsRegistry()
