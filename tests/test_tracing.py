from fastapi import FastAPI

from app.config import config
from app.observability import tracing


def test_configure_telemetry_is_noop_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(tracing, "_configured", False)
    monkeypatch.setattr(tracing, "_provider", None)
    monkeypatch.setattr(config, "otel_enabled", False)

    app = FastAPI()
    assert tracing.configure_telemetry(app) is False
    assert tracing.instrument_asgi_app(app) is app


def test_configure_telemetry_instruments_fastapi_and_httpx(monkeypatch) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    app_calls = []
    httpx_calls = []
    monkeypatch.setattr(tracing, "_configured", False)
    monkeypatch.setattr(tracing, "_provider", None)
    monkeypatch.setattr(config, "otel_enabled", True)
    monkeypatch.setattr(config, "otel_exporter_otlp_endpoint", "")
    monkeypatch.setattr(
        FastAPIInstrumentor,
        "instrument_app",
        lambda *args, **kwargs: app_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        HTTPXClientInstrumentor,
        "instrument",
        lambda *args, **kwargs: httpx_calls.append((args, kwargs)),
    )

    assert tracing.configure_telemetry(FastAPI()) is True
    assert app_calls
    assert httpx_calls
    assert tracing.telemetry_status()["enabled"] is True
    tracing.shutdown_telemetry()
    assert tracing.telemetry_status()["enabled"] is False
