from app.observability.request_metrics import RequestMetricsRegistry


def test_request_metrics_track_inflight_outcomes_and_prometheus() -> None:
    registry = RequestMetricsRegistry()
    registry.begin()
    registry.end("GET", "/ready", 200, 12.5)
    registry.begin()
    registry.end("GET", "/ready", 503, 40.0)

    rendered = registry.render_prometheus()
    assert "aiops_http_requests_in_flight 0" in rendered
    assert 'outcome="success"} 1' in rendered
    assert 'outcome="error"} 1' in rendered
    assert 'quantile="0.95"} 40.0' in rendered
