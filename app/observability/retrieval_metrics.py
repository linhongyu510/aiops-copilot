"""Bounded in-process metrics for RAG retrieval stages and degradations."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from math import ceil
from threading import Lock


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, ceil(percentile * len(ordered)) - 1)]


class RetrievalMetricsRegistry:
    def __init__(self, history_size: int = 2000) -> None:
        self._lock = Lock()
        self._latencies: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self._counts: Counter[str] = Counter()
        self._candidate_counts: deque[int] = deque(maxlen=history_size)

    def record_stage(self, stage: str, latency_ms: float) -> None:
        with self._lock:
            self._latencies[stage].append(float(latency_ms))

    def record_request(self, candidates: int, degradations: list[str]) -> None:
        with self._lock:
            self._counts["requests"] += 1
            if not candidates:
                self._counts["no_answer"] += 1
            self._candidate_counts.append(candidates)
            for degradation in degradations:
                self._counts[f"degradation:{degradation}"] += 1

    def record_cache(self, hit: bool) -> None:
        with self._lock:
            self._counts["cache_hit" if hit else "cache_miss"] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counts": dict(self._counts),
                "candidate_counts": list(self._candidate_counts),
                "latencies": {key: list(value) for key, value in self._latencies.items()},
            }

    def render_prometheus(self) -> str:
        snapshot = self.snapshot()
        lines = [
            "# HELP aiops_rag_requests_total RAG retrieval requests.",
            "# TYPE aiops_rag_requests_total counter",
            f"aiops_rag_requests_total {snapshot['counts'].get('requests', 0)}",
            "# HELP aiops_rag_no_answer_total RAG requests without evidence.",
            "# TYPE aiops_rag_no_answer_total counter",
            f"aiops_rag_no_answer_total {snapshot['counts'].get('no_answer', 0)}",
            "# HELP aiops_rag_stage_latency_milliseconds RAG stage latency.",
            "# TYPE aiops_rag_stage_latency_milliseconds gauge",
        ]
        for stage, values in sorted(snapshot["latencies"].items()):
            for quantile in (0.5, 0.95):
                lines.append(
                    f'aiops_rag_stage_latency_milliseconds{{stage="{stage}",'
                    f'quantile="{quantile}"}} '
                    f"{_percentile(values, quantile):.2f}"
                )
        lines.extend(
            [
                "# HELP aiops_rag_candidate_count RAG final candidate count.",
                "# TYPE aiops_rag_candidate_count gauge",
                'aiops_rag_candidate_count{quantile="0.5"} '
                f"{_percentile(snapshot['candidate_counts'], 0.5):.2f}",
                'aiops_rag_candidate_count{quantile="0.95"} '
                f"{_percentile(snapshot['candidate_counts'], 0.95):.2f}",
                "# HELP aiops_rag_cache_requests_total RAG query-expansion cache outcomes.",
                "# TYPE aiops_rag_cache_requests_total counter",
                f'aiops_rag_cache_requests_total{{result="hit"}} '
                f"{snapshot['counts'].get('cache_hit', 0)}",
                f'aiops_rag_cache_requests_total{{result="miss"}} '
                f"{snapshot['counts'].get('cache_miss', 0)}",
                "# HELP aiops_rag_cache_hit_ratio RAG query-expansion cache hit ratio.",
                "# TYPE aiops_rag_cache_hit_ratio gauge",
                "aiops_rag_cache_hit_ratio "
                f"{self._cache_hit_ratio(snapshot['counts']):.6f}",
                "# HELP aiops_rag_degradations_total RAG degradations by reason.",
                "# TYPE aiops_rag_degradations_total counter",
            ]
        )
        for key, value in sorted(snapshot["counts"].items()):
            if key.startswith("degradation:"):
                reason = key.split(":", 1)[1]
                lines.append(f'aiops_rag_degradations_total{{reason="{reason}"}} {value}')
        return "\n".join(lines) + "\n"

    @staticmethod
    def _cache_hit_ratio(counts: dict[str, int]) -> float:
        hits = counts.get("cache_hit", 0)
        total = hits + counts.get("cache_miss", 0)
        return hits / total if total else 0.0

    def reset(self) -> None:
        with self._lock:
            self._latencies.clear()
            self._counts.clear()
            self._candidate_counts.clear()


retrieval_metrics = RetrievalMetricsRegistry()
