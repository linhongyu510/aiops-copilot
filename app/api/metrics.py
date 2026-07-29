"""Runtime metrics endpoints used for diagnostics and reproducible evaluation."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from opentelemetry import trace

from app.agent.mcp_client import get_mcp_client_with_retry
from app.config import config
from app.observability import request_metrics, tool_metrics
from app.observability.tracing import dependency_span
from app.reliability import dependency_guards

router = APIRouter()


@router.get("/metrics/tools")
async def get_tool_metrics() -> dict:
    """Return aggregate MCP call success and latency metrics."""
    return tool_metrics.snapshot()


@router.get("/metrics/reliability")
async def get_reliability_summary() -> dict:
    """Return error-budget and dependency-isolation status for the UI."""
    snapshot = tool_metrics.snapshot()
    target = min(1.0, max(0.0, config.tool_slo_success_rate))
    allowed_failure_rate = max(0.000001, 1.0 - target)
    observed_failure_rate = 1.0 - float(snapshot["success_rate"])
    consumed = observed_failure_rate / allowed_failure_rate if snapshot["total_calls"] else 0.0
    remaining = max(0.0, 1.0 - consumed)
    return {
        "slo": {
            "target_success_rate": target,
            "target_p95_ms": config.tool_slo_p95_ms,
            "observed_success_rate": snapshot["success_rate"],
            "observed_p95_ms": snapshot["p95_latency_ms"],
            "error_budget_remaining": round(remaining, 4),
            "latency_objective_met": (
                snapshot["total_calls"] == 0 or snapshot["p95_latency_ms"] <= config.tool_slo_p95_ms
            ),
            "sample_size": snapshot["total_calls"],
        },
        "aggregate": {key: value for key, value in snapshot.items() if key != "tools"},
        "tools": snapshot["tools"],
        "dependency_guards": dependency_guards.snapshots(),
    }


@router.get("/metrics", response_class=PlainTextResponse)
async def get_prometheus_metrics() -> str:
    """Return Prometheus-compatible request and tool metrics."""
    return request_metrics.render_prometheus() + tool_metrics.render_prometheus()


@router.get("/observability/trace-probe")
async def trace_probe() -> dict:
    """Run a read-only MCP → WINDOS call to verify trace propagation."""
    try:
        client = await get_mcp_client_with_retry()
        tools = await client.get_tools()
        windos_health = next(
            (tool for tool in tools if tool.name == "windos_health"),
            None,
        )
        if windos_health is None:
            raise RuntimeError("windos_health tool is unavailable")
        with dependency_span(
            "observability.trace_probe",
            {"probe.target": "mcp-ops/windos_health"},
        ):
            result = await windos_health.ainvoke({})
        span_context = trace.get_current_span().get_span_context()
        return {
            "ok": True,
            "trace_id": (f"{span_context.trace_id:032x}" if span_context.is_valid else None),
            "path": ["aiops-api", "mcp-ops", "windos"],
            "result": result,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"trace_probe_failed:{type(exc).__name__}",
        ) from exc
