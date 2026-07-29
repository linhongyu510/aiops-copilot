"""Small process-local HTTP metrics registry with Prometheus rendering."""

from __future__ import annotations

from collections import defaultdict, deque
from math import ceil
from threading import Lock


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, ceil(percentile * len(ordered)) - 1)]


class RequestMetricsRegistry:
    def __init__(self, history_size: int = 2000) -> None:
        self._lock = Lock()
        self._history_size = history_size
        self._calls: dict[tuple[str, str, str], int] = defaultdict(int)
        self._latencies: dict[tuple[str, str], deque[float]] = defaultdict(
            lambda: deque(maxlen=self._history_size)
        )
        self._in_flight = 0

    def begin(self) -> None:
        with self._lock:
            self._in_flight += 1

    def end(self, method: str, path: str, status_code: int, latency_ms: float) -> None:
        outcome = "success" if status_code < 500 else "error"
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            self._calls[(method.upper(), path, outcome)] += 1
            self._latencies[(method.upper(), path)].append(float(latency_ms))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "in_flight": self._in_flight,
                "calls": dict(self._calls),
                "latencies": {key: list(values) for key, values in self._latencies.items()},
            }

    def render_prometheus(self) -> str:
        snapshot = self.snapshot()
        lines = [
            "# HELP aiops_http_requests_in_flight Current in-flight HTTP requests.",
            "# TYPE aiops_http_requests_in_flight gauge",
            f"aiops_http_requests_in_flight {snapshot['in_flight']}",
            "# HELP aiops_http_requests_total HTTP requests by route and outcome.",
            "# TYPE aiops_http_requests_total counter",
        ]
        for (method, path, outcome), count in sorted(snapshot["calls"].items()):
            safe_path = path.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(
                f'aiops_http_requests_total{{method="{method}",path="{safe_path}",'
                f'outcome="{outcome}"}} {count}'
            )
        lines.extend(
            [
                "# HELP aiops_http_request_latency_milliseconds HTTP request latency.",
                "# TYPE aiops_http_request_latency_milliseconds gauge",
            ]
        )
        for (method, path), values in sorted(snapshot["latencies"].items()):
            safe_path = path.replace("\\", "\\\\").replace('"', '\\"')
            for quantile in (0.5, 0.95):
                value = round(_percentile(values, quantile), 2)
                lines.append(
                    f'aiops_http_request_latency_milliseconds{{method="{method}",'
                    f'path="{safe_path}",quantile="{quantile}"}} {value}'
                )
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()
            self._latencies.clear()
            self._in_flight = 0


request_metrics = RequestMetricsRegistry()
