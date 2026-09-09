"""Thread-safe MCP tool metrics used by health checks and agent evaluation."""

from __future__ import annotations

from collections import defaultdict, deque
from math import ceil
from threading import Lock
from typing import Any


class ToolMetricsRegistry:
    def __init__(self, history_size: int = 1000) -> None:
        self._history_size = history_size
        self._lock = Lock()
        self._calls: dict[str, int] = defaultdict(int)
        self._successes: dict[str, int] = defaultdict(int)
        self._errors: dict[tuple[str, str], int] = defaultdict(int)
        self._latencies: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self._history_size)
        )
        self._sequence = 0
        self._recent_calls: deque[dict[str, Any]] = deque(maxlen=self._history_size)

    def record(
        self,
        tool_name: str,
        success: bool,
        latency_ms: float,
        error_class: str = "none",
        arguments: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._sequence += 1
            self._calls[tool_name] += 1
            self._successes[tool_name] += int(success)
            self._latencies[tool_name].append(float(latency_ms))
            if not success:
                normalized = error_class.strip().lower() or "unknown"
                self._errors[(tool_name, normalized)] += 1
            self._recent_calls.append(
                {
                    "sequence": self._sequence,
                    "tool": tool_name,
                    "argument_schema": {
                        str(key): self._type_name(value)
                        for key, value in sorted((arguments or {}).items())
                    },
                    "success": bool(success),
                }
            )

    @staticmethod
    def _type_name(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, str):
            return "str"
        if isinstance(value, list):
            return "list"
        if isinstance(value, dict):
            return "dict"
        return type(value).__name__

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(0, ceil(percentile * len(ordered)) - 1)
        return round(ordered[index], 2)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            names = sorted(self._calls)
            tools: dict[str, Any] = {}
            total_calls = 0
            total_successes = 0
            all_latencies: list[float] = []
            for name in names:
                calls = self._calls[name]
                successes = self._successes[name]
                latencies = list(self._latencies[name])
                total_calls += calls
                total_successes += successes
                all_latencies.extend(latencies)
                tools[name] = {
                    "calls": calls,
                    "successes": successes,
                    "failures": calls - successes,
                    "success_rate": round(successes / calls, 4) if calls else 0.0,
                    "average_latency_ms": round(sum(latencies) / len(latencies), 2)
                    if latencies
                    else 0.0,
                    "p50_latency_ms": self._percentile(latencies, 0.50),
                    "p95_latency_ms": self._percentile(latencies, 0.95),
                    "error_classes": {
                        error_class: count
                        for (tool_name, error_class), count in sorted(self._errors.items())
                        if tool_name == name
                    },
                }
            return {
                "total_calls": total_calls,
                "total_successes": total_successes,
                "total_failures": total_calls - total_successes,
                "success_rate": round(total_successes / total_calls, 4) if total_calls else 0.0,
                "average_latency_ms": round(sum(all_latencies) / len(all_latencies), 2)
                if all_latencies
                else 0.0,
                "p50_latency_ms": self._percentile(all_latencies, 0.50),
                "p95_latency_ms": self._percentile(all_latencies, 0.95),
                "tools": tools,
                # Only names and value types are retained; argument values and secrets
                # are intentionally excluded from the observability API.
                "recent_calls": list(self._recent_calls),
            }

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()
            self._successes.clear()
            self._latencies.clear()
            self._errors.clear()
            self._recent_calls.clear()
            self._sequence = 0

    def render_prometheus(self) -> str:
        snapshot = self.snapshot()
        lines = [
            "# HELP aiops_tool_calls_total Tool calls by tool and outcome.",
            "# TYPE aiops_tool_calls_total counter",
            "# HELP aiops_tool_errors_total Tool failures by error class.",
            "# TYPE aiops_tool_errors_total counter",
        ]
        for name, item in snapshot["tools"].items():
            safe_name = name.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(
                f'aiops_tool_calls_total{{tool="{safe_name}",outcome="success"}} '
                f"{item['successes']}"
            )
            for error_class, count in item["error_classes"].items():
                safe_error = error_class.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(
                    f'aiops_tool_errors_total{{tool="{safe_name}",'
                    f'error_class="{safe_error}"}} {count}'
                )
            lines.append(
                f'aiops_tool_calls_total{{tool="{safe_name}",outcome="failure"}} '
                f"{item['failures']}"
            )
        lines.extend(
            [
                "# HELP aiops_tool_latency_milliseconds Tool latency by quantile.",
                "# TYPE aiops_tool_latency_milliseconds gauge",
            ]
        )
        for name, item in snapshot["tools"].items():
            safe_name = name.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(
                f'aiops_tool_latency_milliseconds{{tool="{safe_name}",quantile="0.5"}} '
                f"{item['p50_latency_ms']}"
            )
            lines.append(
                f'aiops_tool_latency_milliseconds{{tool="{safe_name}",quantile="0.95"}} '
                f"{item['p95_latency_ms']}"
            )
        return "\n".join(lines) + "\n"


tool_metrics = ToolMetricsRegistry()
