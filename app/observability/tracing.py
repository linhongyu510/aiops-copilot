"""Optional OpenTelemetry wiring and manual dependency spans.

需要 ``[obs]`` extra（``opentelemetry-*``）与 ``[server]`` extra（``fastapi``）。
缺失时抛 :class:`aiops_core._optional.OptionalDependencyMissing`。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

try:
    from fastapi import FastAPI
except ImportError as _exc:  # pragma: no cover - depends on install profile
    from aiops_core._optional import OptionalDependencyMissing

    raise OptionalDependencyMissing(
        "app.observability.tracing 需要 `[server]` extra（fastapi）。"
        "\n    pip install 'aiops-copilot[server]'"
    ) from _exc

from loguru import logger

try:
    from opentelemetry import trace
except ImportError as _exc:  # pragma: no cover - depends on install profile
    from aiops_core._optional import OptionalDependencyMissing

    raise OptionalDependencyMissing(
        "app.observability.tracing 需要 `[obs]` extra（opentelemetry-api/sdk）。"
        "\n    pip install 'aiops-copilot[obs]'"
    ) from _exc

from app.config import config

_configured = False
_provider: Any | None = None


def _configure_provider() -> bool:
    global _configured, _provider
    if _configured or not config.otel_enabled:
        return _configured

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": config.otel_service_name}))
    if config.otel_exporter_otlp_endpoint:
        endpoint = config.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _provider = provider
    _configured = True
    return True


def configure_telemetry(app: FastAPI) -> bool:
    """Configure OTLP tracing once; remain a no-op when disabled."""
    if not _configure_provider():
        return False

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="/live,/ready,/health",
    )
    HTTPXClientInstrumentor().instrument()
    logger.info("OpenTelemetry tracing 已启用")
    return True


def instrument_asgi_app(app: Any) -> Any:
    """Wrap an MCP/Starlette ASGI app and propagate HTTP client context."""
    if not _configure_provider():
        return app

    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument()
    logger.info("OpenTelemetry ASGI/MCP tracing 已启用")
    return OpenTelemetryMiddleware(app)


def telemetry_status() -> dict[str, Any]:
    """Return whether an OTLP exporter is configured for this process."""
    return {
        "enabled": _configured,
        "exporter_configured": bool(_configured and config.otel_exporter_otlp_endpoint),
        "endpoint": config.otel_exporter_otlp_endpoint or None,
    }


def shutdown_telemetry() -> None:
    """Flush pending spans before process shutdown."""
    global _configured, _provider
    if _provider is not None:
        _provider.shutdown()
    _provider = None
    _configured = False


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
