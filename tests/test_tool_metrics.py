import asyncio

from app.api.metrics import get_reliability_summary
from app.observability import tool_metrics
from app.observability.tool_metrics import ToolMetricsRegistry


def test_tool_metrics_aggregates_success_and_latency() -> None:
    registry = ToolMetricsRegistry()
    registry.record("search_log", True, 10)
    registry.record("search_log", False, 30, error_class="timeout")
    registry.record("query_cpu_metrics", True, 20)

    snapshot = registry.snapshot()

    assert snapshot["total_calls"] == 3
    assert snapshot["total_successes"] == 2
    assert snapshot["success_rate"] == 0.6667
    assert snapshot["p50_latency_ms"] == 20
    assert snapshot["tools"]["search_log"]["success_rate"] == 0.5
    assert snapshot["tools"]["search_log"]["error_classes"] == {"timeout": 1}
    prometheus = registry.render_prometheus()
    assert 'aiops_tool_calls_total{tool="search_log",outcome="success"} 1' in prometheus
    assert 'aiops_tool_calls_total{tool="search_log",outcome="failure"} 1' in prometheus
    assert 'aiops_tool_errors_total{tool="search_log",error_class="timeout"} 1' in prometheus


def test_reliability_summary_calculates_error_budget() -> None:
    tool_metrics.reset()
    for _ in range(99):
        tool_metrics.record("health", True, 10)
    tool_metrics.record("health", False, 20, error_class="timeout")
    report = asyncio.run(get_reliability_summary())
    assert report["slo"]["sample_size"] == 100
    assert report["slo"]["error_budget_remaining"] == 0.0
    assert report["aggregate"]["total_calls"] == 100
    tool_metrics.reset()
