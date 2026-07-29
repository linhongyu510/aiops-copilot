"""Ops MCP server: safe read-only MySQL diagnostics and web search."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from loguru import logger
from opentelemetry.propagate import inject
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from tavily import AsyncTavilyClient

from app.observability.tracing import dependency_span
from app.reliability import dependency_guards

load_dotenv()

mcp = FastMCP("Ops")

_engine: Engine | None = None
_windos_client: httpx.AsyncClient | None = None
_windos_client_signature: tuple[str, int] | None = None
_windos_client_lock = asyncio.Lock()
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_READ_ONLY_PREFIXES = ("select", "show", "describe", "desc", "explain")
_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|replace|merge|alter|drop|truncate|create|rename|grant|"
    r"revoke|call|execute|prepare|handler|load|outfile|dumpfile|lock|unlock|set|use)\b",
    re.IGNORECASE,
)
_DANGEROUS_SQL_FUNCTION = re.compile(r"\b(sleep|benchmark|load_file)\s*\(", re.IGNORECASE)
_SCHEMA_REFERENCE = re.compile(
    r"\b(?:from|join)\s+`?([A-Za-z_][A-Za-z0-9_$]*)`?\s*\.",
    re.IGNORECASE,
)


def _settings() -> dict[str, Any]:
    return {
        "mysql_dsn": os.getenv("MYSQL_DSN", "").strip(),
        "allowed_schemas": {
            value.strip()
            for value in os.getenv("MYSQL_ALLOWED_SCHEMAS", "").split(",")
            if value.strip()
        },
        "query_timeout": max(1, int(os.getenv("MYSQL_QUERY_TIMEOUT_SECONDS", "10"))),
        "max_rows": max(1, int(os.getenv("MYSQL_MAX_ROWS", "200"))),
        "tavily_api_key": os.getenv("TAVILY_API_KEY", "").strip(),
        "web_max_results": max(1, int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))),
        "windos_base_url": os.getenv("WINDOS_BASE_URL", "http://127.0.0.1:8002").rstrip("/"),
        "windos_api_key": os.getenv("WINDOS_API_KEY", "").strip(),
        "windos_timeout": max(1, int(os.getenv("WINDOS_TIMEOUT_SECONDS", "8"))),
    }


async def _windos_get(path: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a WINDOS read-only endpoint and retain provenance for the Agent report."""
    settings = _settings()
    headers = {
        "Accept": "application/json",
        "X-Request-ID": f"aiops-{uuid.uuid4().hex}",
    }
    if settings["windos_api_key"]:
        headers["X-API-Key"] = settings["windos_api_key"]
    inject(headers)
    started = time.perf_counter()
    client = await _get_windos_client()
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


async def _get_windos_client() -> httpx.AsyncClient:
    """Reuse connections while rebuilding the pool when endpoint settings change."""
    global _windos_client, _windos_client_signature
    settings = _settings()
    signature = (settings["windos_base_url"], settings["windos_timeout"])
    if _windos_client is not None and _windos_client_signature == signature:
        return _windos_client
    async with _windos_client_lock:
        if _windos_client is not None and _windos_client_signature != signature:
            await _windos_client.aclose()
            _windos_client = None
        if _windos_client is None:
            timeout = httpx.Timeout(settings["windos_timeout"])
            limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
            _windos_client = httpx.AsyncClient(
                base_url=settings["windos_base_url"],
                timeout=timeout,
                limits=limits,
            )
            _windos_client_signature = signature
        return _windos_client


@mcp.tool()
async def windos_health() -> dict[str, Any]:
    """检查 WINDOS API 存活、综合就绪和生产配置闸门；全部为只读检查。"""
    paths = ("/api/v1/health", "/api/v1/ready", "/api/v1/production/readiness")
    values = await asyncio.gather(*(_windos_get(path) for path in paths), return_exceptions=True)
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


@mcp.tool()
async def windos_queue_status() -> dict[str, Any]:
    """只读查询 WINDOS 异步排程队列、任务状态分布、Worker 心跳和积压时间。"""
    return await _windos_get("/api/v1/schedule/queue/status")


@mcp.tool()
async def windos_agent_metrics() -> dict[str, Any]:
    """只读查询 WINDOS Agent 各工具成功率、错误/超时及 P50/P95 延迟。"""
    return await _windos_get("/api/v1/agent/metrics/tools")


@mcp.tool()
async def windos_governance_status() -> dict[str, Any]:
    """只读查询 WINDOS 运行模式、审计、身份、持久化与集成治理状态。"""
    return await _windos_get("/api/v1/governance/status")


@mcp.tool()
async def windos_recent_audit(limit: int = 20) -> dict[str, Any]:
    """只读查询 WINDOS 最近审计事件，用于关联故障前的配置或操作变更。"""
    return await _windos_get("/api/v1/audit/recent", {"limit": min(max(1, limit), 100)})


@mcp.tool()
async def windos_diagnose_overview() -> dict[str, Any]:
    """并行收集 WINDOS 健康、队列、Agent 指标与治理状态，生成自动运维诊断证据快照。"""
    checks = {
        "health": _windos_get("/api/v1/health"),
        "ready": _windos_get("/api/v1/ready"),
        "production_readiness": _windos_get("/api/v1/production/readiness"),
        "queue": _windos_get("/api/v1/schedule/queue/status"),
        "agent_metrics": _windos_get("/api/v1/agent/metrics/tools"),
        "governance": _windos_get("/api/v1/governance/status"),
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


def _get_engine() -> Engine:
    global _engine
    settings = _settings()
    if not settings["mysql_dsn"]:
        raise RuntimeError("MYSQL_DSN 未配置；请使用只读 MySQL 账号配置连接串")
    if _engine is None:
        timeout = settings["query_timeout"]
        _engine = create_engine(
            settings["mysql_dsn"],
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={
                "connect_timeout": timeout,
                "read_timeout": timeout,
                "write_timeout": timeout,
            },
        )
    return _engine


def _validate_identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"非法{label}: {value!r}")
    return value


def _validate_schema(schema: str | None) -> str | None:
    allowed = _settings()["allowed_schemas"]
    if schema is None:
        dsn = _settings()["mysql_dsn"]
        default_schema = make_url(dsn).database if dsn else None
        if allowed and default_schema not in allowed:
            raise PermissionError("MySQL 默认 schema 不在 MYSQL_ALLOWED_SCHEMAS 白名单中")
        return None
    schema = _validate_identifier(schema, "schema")
    if allowed and schema not in allowed:
        raise PermissionError(f"schema {schema!r} 不在 MYSQL_ALLOWED_SCHEMAS 白名单中")
    return schema


def _validate_read_only_sql(statement: str) -> str:
    sql = statement.strip()
    normalized = re.sub(r"\s+", " ", sql).lower()
    if not sql:
        raise ValueError("SQL 不能为空")
    if ";" in sql.rstrip(";") or "--" in sql or "/*" in sql or "#" in sql:
        raise ValueError("不允许多语句或 SQL 注释")
    if not normalized.startswith(_READ_ONLY_PREFIXES):
        raise PermissionError("仅允许 SELECT / SHOW / DESCRIBE / EXPLAIN 查询")
    if _FORBIDDEN_SQL.search(normalized):
        raise PermissionError("查询包含被禁止的写入或管理关键字")
    if _DANGEROUS_SQL_FUNCTION.search(normalized):
        raise PermissionError("查询包含被禁止的高风险函数")
    if normalized.startswith("show databases"):
        raise PermissionError("不允许枚举数据库")
    allowed = _settings()["allowed_schemas"]
    referenced_schemas = set(_SCHEMA_REFERENCE.findall(sql))
    denied = sorted(schema for schema in referenced_schemas if allowed and schema not in allowed)
    if denied:
        raise PermissionError(f"查询引用了未授权 schema: {', '.join(denied)}")
    _validate_schema(None)
    return sql.rstrip(";")


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


@mcp.tool()
def mysql_list_tables(schema: str | None = None) -> dict[str, Any]:
    """列出允许访问的 MySQL schema 中的表；不返回任何业务数据。"""
    started = time.perf_counter()
    schema = _validate_schema(schema)
    tables = inspect(_get_engine()).get_table_names(schema=schema)
    return {
        "schema": schema,
        "tables": sorted(tables),
        "count": len(tables),
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


@mcp.tool()
def mysql_describe_table(table: str, schema: str | None = None) -> dict[str, Any]:
    """返回 MySQL 表结构，用于生成和校验只读诊断查询。"""
    started = time.perf_counter()
    table = _validate_identifier(table, "表名")
    schema = _validate_schema(schema)
    columns = inspect(_get_engine()).get_columns(table, schema=schema)
    return {
        "schema": schema,
        "table": table,
        "columns": [
            {
                "name": item["name"],
                "type": str(item["type"]),
                "nullable": bool(item.get("nullable", True)),
                "default": _json_value(item.get("default")),
            }
            for item in columns
        ],
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


@mcp.tool()
def mysql_read_query(
    sql: str,
    parameters: dict[str, Any] | None = None,
    max_rows: int = 100,
) -> dict[str, Any]:
    """执行参数化的只读 MySQL 查询。禁止写入、多语句、注释和危险关键字。"""
    started = time.perf_counter()
    statement = _validate_read_only_sql(sql)
    settings = _settings()
    row_limit = min(max(1, max_rows), settings["max_rows"])
    if statement.lstrip().lower().startswith("select") and not re.search(
        r"\blimit\s+\d+", statement, re.IGNORECASE
    ):
        statement = f"{statement} LIMIT {row_limit}"

    with _get_engine().connect() as connection:
        result = connection.execute(text(statement), parameters or {})
        rows = [
            {key: _json_value(value) for key, value in row.items()}
            for row in result.mappings().fetchmany(row_limit)
        ]

    return {
        "rows": rows,
        "row_count": len(rows),
        "truncated": len(rows) >= row_limit,
        "max_rows": row_limit,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


@mcp.tool()
async def web_search(
    query: str,
    max_results: int = 5,
    search_depth: Literal["basic", "advanced"] = "basic",
    topic: Literal["general", "news", "finance"] = "general",
    time_range: Literal["day", "week", "month", "year"] | None = None,
) -> dict[str, Any]:
    """联网检索最新公开信息，返回标题、URL、摘要和相关性分数。"""
    settings = _settings()
    if not settings["tavily_api_key"]:
        raise RuntimeError("TAVILY_API_KEY 未配置，联网检索不可用")
    query = query.strip()
    if not query:
        raise ValueError("检索词不能为空")
    result_limit = min(max(1, max_results), settings["web_max_results"])
    started = time.perf_counter()
    client = AsyncTavilyClient(api_key=settings["tavily_api_key"])
    try:
        response = await client.search(
            query=query,
            max_results=result_limit,
            search_depth=search_depth,
            topic=topic,
            time_range=time_range,
            include_answer="basic",
            include_raw_content=False,
            timeout=20,
        )
    finally:
        close = getattr(client, "close", None)
        if close:
            await close()

    results = response.get("results", [])
    return {
        "query": query,
        "answer": response.get("answer"),
        "results": [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content"),
                "score": item.get("score"),
            }
            for item in results[:result_limit]
        ],
        "result_count": min(len(results), result_limit),
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


if __name__ == "__main__":
    host = os.getenv("MCP_OPS_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_OPS_PORT", "8005"))
    logger.info(f"启动 Ops MCP Server: http://{host}:{port}/mcp")
    mcp.run(transport="streamable-http", host=host, port=port)
