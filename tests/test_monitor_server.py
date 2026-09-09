"""monitor_server 时间序列工具的参数校验：interval 下限、非法格式与数据点上限"""

import pytest

from mcp_servers.monitor_server import (
    MAX_DATA_POINTS,
    parse_interval_minutes,
    query_cpu_metrics,
    query_memory_metrics,
)


@pytest.mark.parametrize(
    "interval, expected",
    [
        ("1m", 1),
        ("5m", 5),
        ("1h", 60),
        ("2h", 120),
    ],
)
def test_parse_interval_minutes_accepts_valid_formats(interval: str, expected: int) -> None:
    assert parse_interval_minutes(interval) == expected


@pytest.mark.parametrize("interval", ["0m", "0h", "-5m", "abc", "10x", "", "1.5m"])
def test_parse_interval_minutes_rejects_invalid_formats(interval: str) -> None:
    """0/负数/无法解析的 interval 一律返回 None（0 会导致时间序列循环永不推进）"""
    assert parse_interval_minutes(interval) is None


@pytest.mark.parametrize("interval", ["0m", "abc", "10x"])
def test_query_cpu_metrics_rejects_invalid_interval(interval: str) -> None:
    """非法 interval 返回明确错误而不是死循环"""
    result = query_cpu_metrics.fn(
        service_name="svc",
        start_time="2026-02-14 10:00:00",
        end_time="2026-02-14 11:00:00",
        interval=interval,
    )
    assert result["data_points"] == []
    assert "interval" in result["error"]


@pytest.mark.parametrize("interval", ["0m", "0h"])
def test_query_memory_metrics_rejects_zero_interval(interval: str) -> None:
    result = query_memory_metrics.fn(
        service_name="svc",
        start_time="2026-02-14 10:00:00",
        end_time="2026-02-14 11:00:00",
        interval=interval,
    )
    assert result["data_points"] == []
    assert "interval" in result["error"]


@pytest.mark.parametrize("tool", [query_cpu_metrics, query_memory_metrics])
def test_query_metrics_rejects_too_many_data_points(tool) -> None:
    """时间范围过大导致数据点超过上限时返回明确错误，而不是无界输出"""
    result = tool.fn(
        service_name="svc",
        start_time="2026-01-01 00:00:00",
        end_time="2026-01-31 00:00:00",  # 30 天，1m 间隔将生成 4 万+ 个点
        interval="1m",
    )
    assert result["data_points"] == []
    assert str(MAX_DATA_POINTS) in result["error"]


@pytest.mark.parametrize("tool", [query_cpu_metrics, query_memory_metrics])
def test_query_metrics_accepts_range_at_data_point_limit(tool) -> None:
    """恰好不超限的时间范围正常返回数据"""
    result = tool.fn(
        service_name="svc",
        start_time="2026-02-14 10:00:00",
        end_time="2026-02-14 11:00:00",  # 1 小时，5m 间隔 → 13 个点
        interval="5m",
    )
    assert "error" not in result
    assert len(result["data_points"]) == 13
    assert result["statistics"]
