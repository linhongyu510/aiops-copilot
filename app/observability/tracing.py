"""Optional OpenTelemetry wiring and manual dependency spans."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI
from loguru import logger
from opentelemetry import trace

from app.config import config

_configured = False


def configure_telemetry(app: FastAPI) -> bool:
    """Configure OTLP tracing once; remain a no-op when disabled."""
    global _configured
    if _configured or not config.otel_enabled:
        return _configured

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create({"service.name": config.otel_service_name})
    )
    if config.otel_exporter_otlp_endpoint:
        endpoint = config.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="/live,/ready,/health",
    )
    HTTPXClientInstrumentor().instrument()
    _configured = True
    logger.info("OpenTelemetry tracing 已启用")
    return True


@contextmanager
def dependency_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    tracer = trace.get_tracer("aiops-copilot.dependencies")
    with tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
            raise
