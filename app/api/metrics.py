"""Runtime metrics endpoints used for diagnostics and reproducible evaluation."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from opentelemetry import trace

from app.agent.mcp_client import get_mcp_client_with_retry
from app.agent.tool_registry import tool_registry
from app.config import config
from app.observability import llm_metrics, request_metrics, retrieval_metrics, tool_metrics
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
    return (
        request_metrics.render_prometheus()
        + tool_metrics.render_prometheus()
        + retrieval_metrics.render_prometheus()
        + llm_metrics.render_prometheus()
    )


@router.get("/metrics/llm")
async def get_llm_metrics() -> dict:
    """Return per-model LLM call, latency, token and cost metrics."""
    return llm_metrics.snapshot()


@router.get("/metrics/retrieval")
async def get_retrieval_metrics() -> dict:
    """Return bounded RAG stage latency and degradation samples."""
    return retrieval_metrics.snapshot()


@router.get("/observability/trace-probe")
async def trace_probe() -> dict:
    """Verify trace propagation through one read-only MCP call.

    The probe only needs *some* read-only tool to cross the API → MCP boundary;
    it is not tied to any particular integration. ``AIOPS_TRACE_PROBE_TOOL``
    pins a specific tool when a deployment wants a stable probe target,
    otherwise the first registered read-only tool is used.
    """
    try:
        client = await get_mcp_client_with_retry()
        tools = await client.get_tools()

        preferred = config.trace_probe_tool.strip()
        probe_tool = None
        if preferred:
            probe_tool = next((tool for tool in tools if tool.name == preferred), None)
            if probe_tool is None:
                raise RuntimeError(f"configured trace probe tool is unavailable: {preferred}")
        else:
            probe_tool = next(
                (
                    tool
                    for tool in tools
                    if tool.name
                    and tool_registry.spec_for(tool.name).read_only
                    and tool_registry.spec_for(tool.name).risk_level == 0
                ),
                None,
            )
        if probe_tool is None:
            raise RuntimeError("no read-only MCP tool is available for the trace probe")

        with dependency_span(
            "observability.trace_probe",
            {"probe.target": f"mcp/{probe_tool.name}"},
        ):
            result = await probe_tool.ainvoke({})
        span_context = trace.get_current_span().get_span_context()
        return {
            "ok": True,
            "trace_id": (f"{span_context.trace_id:032x}" if span_context.is_valid else None),
            "path": ["aiops-api", "mcp", probe_tool.name],
            "probe_tool": probe_tool.name,
            "result": result,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"trace_probe_failed:{type(exc).__name__}",
        ) from exc
