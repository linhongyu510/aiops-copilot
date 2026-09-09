"""WINDOS integration — an example of plugging a private platform into AIOps Copilot.

WINDOS is a separate in-house system, not part of this project. It is kept here
as a **reference integration**: it shows how to expose an external platform's
read-only diagnostics to the agent without the core ever depending on it.

Enable it by setting ``AIOPS_ENABLED_INTEGRATIONS=windos`` (plus
``WINDOS_BASE_URL``). When disabled, none of these tools are registered and the
agent behaves exactly as if this directory did not exist.

Use this module as a template for your own platform:

1. Describe each tool with :class:`ToolSpec` — ``read_only`` and ``risk_level``
   are what the router, tool-level RBAC and the change-approval path key off.
2. Return provenance (``source`` / ``endpoint`` / ``http_status`` / ``latency_ms``)
   so answers stay auditable.
3. Surface a failed dependency as an explicit error instead of a fabricated
   value.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

import httpx
from opentelemetry.propagate import inject

from app.agent.tool_registry import ToolSpec, tool_registry
from app.observability.tracing import dependency_span
from app.reliability import dependency_guards

INTEGRATION_NAME = "windos"

# Tool metadata registered into the shared registry when this integration loads.
TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="windos_health",
        category="windos",
        description="查询 WINDOS 生产就绪健康状态与就绪检查结果",
        keywords=("windos", "生产就绪", "健康"),
    ),
    ToolSpec(
        name="windos_queue_status",
        category="windos",
        description="查询 WINDOS 调度队列状态与积压情况",
        keywords=("windos", "排程", "队列"),
    ),
    ToolSpec(
        name="windos_agent_metrics",
        category="windos",
        description="查询 WINDOS agent 工具调用指标",
        keywords=("windos", "agent", "指标"),
    ),
    ToolSpec(
        name="windos_governance_status",
        category="windos",
        description="查询 WINDOS 治理状态",
        keywords=("windos", "治理"),
    ),
    ToolSpec(
        name="windos_recent_audit",
        category="windos",
        description="查询 WINDOS 近期审计记录",
        keywords=("windos", "审计"),
    ),
    ToolSpec(
        name="windos_diagnose_overview",
        category="windos",
        description="WINDOS 综合诊断总览，聚合健康、队列、治理等多项只读检查",
        keywords=("windos", "生产就绪", "总览", "诊断"),
    ),
)

# Routing hints contributed by this integration (keywords -> tool prefixes).
TOOL_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("windos", "生产就绪", "排程", "治理"), ("windos_",)),
)

_client: httpx.AsyncClient | None = None
_client_signature: tuple[str, int] | None = None
_client_lock = asyncio.Lock()


def settings() -> dict[str, Any]:
    return {
        "base_url": os.getenv("WINDOS_BASE_URL", "http://127.0.0.1:8002").rstrip("/"),
        "api_key": os.getenv("WINDOS_API_KEY", "").strip(),
        "timeout": max(1, int(os.getenv("WINDOS_TIMEOUT_SECONDS", "8"))),
    }


def register_tool_specs() -> None:
    """Publish this integration's governance metadata to the shared registry."""
    for spec in TOOL_SPECS:
        tool_registry.register(spec)


async def _get_client() -> httpx.AsyncClient:
    """Reuse connections while rebuilding the pool when endpoint settings change."""
    global _client, _client_signature
    current = settings()
    signature = (current["base_url"], current["timeout"])
    if _client is not None and _client_signature == signature:
        return _client
    async with _client_lock:
        if _client is not None and _client_signature != signature:
            await _client.aclose()
            _client = None
        if _client is None:
            _client = httpx.AsyncClient(
                base_url=current["base_url"],
                timeout=httpx.Timeout(current["timeout"]),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
            _client_signature = signature
        return _client


async def windos_get(path: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a WINDOS read-only endpoint and retain provenance for the Agent report."""
    current = settings()
    headers = {
        "Accept": "application/json",
        "X-Request-ID": f"aiops-{uuid.uuid4().hex}",
    }
    if current["api_key"]:
        headers["X-API-Key"] = current["api_key"]
    inject(headers)
    started = time.perf_counter()
    client = await _get_client()
    guard = dependency_guards.get("windos-api")
    with dependency_span(
        "windos.http",
        {"http.request.method": "GET", "url.path": path, "peer.service": "windos"},
    ) as span:
        response = await guard.call(
            lambda: client.get(path, params=parameters, headers=headers),
            failure_predicate=lambda item: item.status_code in {500, 502, 504},
        )
        span.set_attribute("http.response.status_code", response.status_code)
    try:
        payload: Any = response.json()
    except ValueError:
        payload = {"text": response.text[:2000]}
    return {
        "source": "windos_live_api",
        "endpoint": path,
        "read_only": True,
        "http_status": response.status_code,
        "ok": response.is_success,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "data": payload,
    }


async def health() -> dict[str, Any]:
    """检查 WINDOS API 存活、综合就绪和生产配置闸门；全部为只读检查。"""
    paths = ("/api/v1/health", "/api/v1/ready", "/api/v1/production/readiness")
    values = await asyncio.gather(*(windos_get(path) for path in paths), return_exceptions=True)
    results = [
        {"ok": False, "error_type": type(value).__name__, "error": str(value)}
        if isinstance(value, Exception)
        else value
        for value in values
    ]
    return {
        "system": "WINDOS",
        "read_only": True,
        "checks": dict(zip(paths, results, strict=True)),
    }


async def queue_status() -> dict[str, Any]:
    """只读查询 WINDOS 异步排程队列、任务状态分布、Worker 心跳和积压时间。"""
    return await windos_get("/api/v1/schedule/queue/status")


async def agent_metrics() -> dict[str, Any]:
    """只读查询 WINDOS Agent 各工具成功率、错误/超时及 P50/P95 延迟。"""
    return await windos_get("/api/v1/agent/metrics/tools")


async def governance_status() -> dict[str, Any]:
    """只读查询 WINDOS 运行模式、审计、身份、持久化与集成治理状态。"""
    return await windos_get("/api/v1/governance/status")


async def recent_audit(limit: int = 20) -> dict[str, Any]:
    """只读查询 WINDOS 最近审计事件，用于关联故障前的配置或操作变更。"""
    return await windos_get("/api/v1/audit/recent", {"limit": min(max(1, limit), 100)})


async def diagnose_overview() -> dict[str, Any]:
    """并行收集 WINDOS 健康、队列、Agent 指标与治理状态，生成自动运维诊断证据快照。"""
    checks = {
        "health": windos_get("/api/v1/health"),
        "ready": windos_get("/api/v1/ready"),
        "production_readiness": windos_get("/api/v1/production/readiness"),
        "queue": windos_get("/api/v1/schedule/queue/status"),
        "agent_metrics": windos_get("/api/v1/agent/metrics/tools"),
        "governance": windos_get("/api/v1/governance/status"),
    }
    values = await asyncio.gather(*checks.values(), return_exceptions=True)
    evidence: dict[str, Any] = {}
    for name, value in zip(checks, values, strict=True):
        evidence[name] = (
            {"ok": False, "error_type": type(value).__name__, "error": str(value)}
            if isinstance(value, Exception)
            else value
        )
    return {
        "system": "WINDOS",
        "mode": "read_only_diagnosis",
        "automatic_changes_permitted": False,
        "evidence": evidence,
    }


def register_mcp_tools(mcp: Any) -> tuple[str, ...]:
    """Attach this integration's read-only tools to an MCP server instance.

    Called by ``mcp_servers/ops_server.py`` only when the integration is
    enabled, so a default deployment never exposes WINDOS tools. Tool names are
    bound explicitly to keep the ``windos_`` routing prefix stable regardless of
    the local function names.
    """
    register_tool_specs()
    bindings = (
        ("windos_health", health),
        ("windos_queue_status", queue_status),
        ("windos_agent_metrics", agent_metrics),
        ("windos_governance_status", governance_status),
        ("windos_recent_audit", recent_audit),
        ("windos_diagnose_overview", diagnose_overview),
    )
    for tool_name, function in bindings:
        mcp.tool(name=tool_name)(function)
    return tuple(name for name, _ in bindings)
