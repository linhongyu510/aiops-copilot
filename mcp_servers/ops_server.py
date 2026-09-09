"""Ops MCP server: safe read-only MySQL diagnostics and web search."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import ssl
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlparse

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
_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,252}$")
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
        "prometheus_url": os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/"),
        "kubernetes_api_url": os.getenv("KUBERNETES_API_URL", "").rstrip("/"),
        "kubernetes_token": os.getenv("KUBERNETES_TOKEN", "").strip(),
        "kubernetes_verify_tls": os.getenv("KUBERNETES_VERIFY_TLS", "true").lower() == "true",
        "docker_api_url": os.getenv("DOCKER_API_URL", "").rstrip("/"),
        "redis_diagnostic_url": os.getenv(
            "REDIS_DIAGNOSTIC_URL", os.getenv("AIOPS_REDIS_URL", "")
        ).strip(),
        "diagnostic_allowed_hosts": {
            value.strip().lower()
            for value in os.getenv(
                "DIAGNOSTIC_ALLOWED_HOSTS", "localhost,127.0.0.1"
            ).split(",")
            if value.strip()
        },
        "diagnostic_timeout": max(1, int(os.getenv("DIAGNOSTIC_TIMEOUT_SECONDS", "8"))),
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


def _resource_name(value: str, label: str) -> str:
    if not _RESOURCE_NAME.fullmatch(value):
        raise ValueError(f"非法{label}: {value!r}")
    return value


def _allowed_diagnostic_host(host: str) -> str:
    normalized = host.strip().lower().rstrip(".")
    if not normalized:
        raise ValueError("主机名不能为空")
    allowed = _settings()["diagnostic_allowed_hosts"]
    if normalized not in allowed:
        raise PermissionError(
            f"主机 {normalized!r} 不在 DIAGNOSTIC_ALLOWED_HOSTS 白名单中"
        )
    try:
        address = ipaddress.ip_address(normalized)
        if not (address.is_loopback or address.is_private):
            raise PermissionError("默认禁止探测公网 IP")
    except ValueError:
        pass
    return normalized


async def _json_get(
    base_url: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    if not base_url:
        return {
            "available": False,
            "read_only": True,
            "reason": "dependency_not_configured",
        }
    started = time.perf_counter()
    timeout = httpx.Timeout(_settings()["diagnostic_timeout"])
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout, verify=verify) as client:
        response = await client.get(path, params=params, headers=headers)
    try:
        payload: Any = response.json()
    except ValueError:
        payload = {"text": response.text[:4000]}
    return {
        "available": True,
        "read_only": True,
        "http_status": response.status_code,
        "ok": response.is_success,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "data": payload,
    }


async def _prometheus_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _json_get(_settings()["prometheus_url"], path, params=params)


def _kubernetes_headers() -> dict[str, str]:
    token = _settings()["kubernetes_token"]
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _kubernetes_get(
    path: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    settings = _settings()
    return await _json_get(
        settings["kubernetes_api_url"],
        path,
        params=params,
        headers=_kubernetes_headers(),
        verify=settings["kubernetes_verify_tls"],
    )


@mcp.tool()
async def prom_query(query: str, evaluation_time: str | None = None) -> dict[str, Any]:
    """执行 Prometheus 即时 PromQL 查询；只读取时序数据。"""
    query = query.strip()
    if not query or len(query) > 2000:
        raise ValueError("PromQL 不能为空且长度不得超过 2000")
    params: dict[str, Any] = {"query": query}
    if evaluation_time:
        params["time"] = evaluation_time
    return await _prometheus_get("/api/v1/query", params)


@mcp.tool()
async def prom_query_range(
    query: str,
    start: str,
    end: str,
    step: str = "60s",
) -> dict[str, Any]:
    """执行 Prometheus 区间 PromQL 查询，用于故障时间窗趋势分析。"""
    query = query.strip()
    if not query or len(query) > 2000:
        raise ValueError("PromQL 不能为空且长度不得超过 2000")
    if not re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", step):
        raise ValueError("step 必须是正数或 Prometheus 时长，例如 60s")
    return await _prometheus_get(
        "/api/v1/query_range",
        {"query": query, "start": start, "end": end, "step": step},
    )


@mcp.tool()
async def prom_active_alerts() -> dict[str, Any]:
    """读取 Prometheus 当前活跃告警及其标签、注解和状态。"""
    return await _prometheus_get("/api/v1/alerts")


@mcp.tool()
async def prom_target_health(state: Literal["active", "dropped", "any"] = "active") -> dict[str, Any]:
    """读取 Prometheus 抓取 Target 健康状态，不修改配置。"""
    return await _prometheus_get("/api/v1/targets", {"state": state})


async def _loki_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Loki 只读查询；LOKI_URL 未配置时明确返回不可用，不伪造数据。"""
    return await _json_get(
        os.getenv("LOKI_URL", "").rstrip("/"), path, params=params
    )


@mcp.tool()
async def loki_query(
    query: str,
    start: str,
    end: str,
    limit: int = 100,
    direction: Literal["forward", "backward"] = "backward",
) -> dict[str, Any]:
    """执行 Loki LogQL 日志查询（时间窗内），用于真实日志检索；只读。

    Args:
        query: LogQL 表达式，如 `{app="api"} |= "error"`
        start/end: RFC3339 或 Unix 秒时间戳
        limit: 返回条数上限（1-500）
        direction: backward 从最新往回查
    """
    query = query.strip()
    if not query or len(query) > 2000:
        raise ValueError("LogQL 不能为空且长度不得超过 2000")
    if not start or not end:
        raise ValueError("start/end 不能为空")
    return await _loki_get(
        "/loki/api/v1/query_range",
        {
            "query": query,
            "start": start,
            "end": end,
            "limit": min(max(1, limit), 500),
            "direction": direction,
        },
    )


@mcp.tool()
async def loki_labels(
    label_name: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """列出 Loki 日志标签名（或指定标签的取值），用于构造 LogQL；只读。"""
    if label_name:
        label_name = _resource_name(label_name, "标签名")
        path = f"/loki/api/v1/label/{label_name}/values"
    else:
        path = "/loki/api/v1/labels"
    params = {"start": start, "end": end} if start and end else None
    return await _loki_get(path, params)


@mcp.tool()
async def k8s_list_pods(
    namespace: str = "default",
    label_selector: str | None = None,
) -> dict[str, Any]:
    """列出 Kubernetes Namespace 中的 Pod 和状态；只读。"""
    namespace = _resource_name(namespace, "namespace")
    params = {"labelSelector": label_selector} if label_selector else None
    return await _kubernetes_get(f"/api/v1/namespaces/{namespace}/pods", params)


@mcp.tool()
async def k8s_describe_workload(
    name: str,
    namespace: str = "default",
    kind: Literal["deployment", "statefulset", "daemonset"] = "deployment",
) -> dict[str, Any]:
    """读取 Kubernetes 工作负载 spec/status，不返回 Secret。"""
    name = _resource_name(name, "workload")
    namespace = _resource_name(namespace, "namespace")
    plural = {
        "deployment": "deployments",
        "statefulset": "statefulsets",
        "daemonset": "daemonsets",
    }[kind]
    return await _kubernetes_get(
        f"/apis/apps/v1/namespaces/{namespace}/{plural}/{name}"
    )


@mcp.tool()
async def k8s_get_events(
    namespace: str = "default",
    involved_object: str | None = None,
) -> dict[str, Any]:
    """读取 Kubernetes Event，用于定位调度、拉镜像和探针失败。"""
    namespace = _resource_name(namespace, "namespace")
    params = None
    if involved_object:
        involved_object = _resource_name(involved_object, "资源名")
        params = {"fieldSelector": f"involvedObject.name={involved_object}"}
    return await _kubernetes_get(f"/api/v1/namespaces/{namespace}/events", params)


@mcp.tool()
async def k8s_get_logs(
    pod: str,
    namespace: str = "default",
    container: str | None = None,
    tail_lines: int = 200,
) -> dict[str, Any]:
    """读取 Kubernetes Pod 最近日志；限制返回行数且不执行命令。"""
    pod = _resource_name(pod, "pod")
    namespace = _resource_name(namespace, "namespace")
    params: dict[str, Any] = {"tailLines": min(max(1, tail_lines), 1000)}
    if container:
        params["container"] = _resource_name(container, "container")
    return await _kubernetes_get(
        f"/api/v1/namespaces/{namespace}/pods/{pod}/log",
        params,
    )


@mcp.tool()
async def k8s_rollout_status(
    deployment: str,
    namespace: str = "default",
) -> dict[str, Any]:
    """读取 Deployment rollout 的期望/可用/更新副本状态。"""
    result = await k8s_describe_workload(deployment, namespace, "deployment")
    data = result.get("data")
    if not isinstance(data, dict):
        return result
    spec = data.get("spec", {})
    status = data.get("status", {})
    result["rollout"] = {
        "generation": data.get("metadata", {}).get("generation"),
        "observed_generation": status.get("observedGeneration"),
        "desired_replicas": spec.get("replicas"),
        "updated_replicas": status.get("updatedReplicas", 0),
        "available_replicas": status.get("availableReplicas", 0),
        "unavailable_replicas": status.get("unavailableReplicas", 0),
    }
    result["data"] = {"metadata": data.get("metadata", {}), "rollout": result["rollout"]}
    return result


@mcp.tool()
async def docker_list_containers(all_containers: bool = True) -> dict[str, Any]:
    """通过 Docker Engine Read API 列出容器，不执行创建、停止或删除。"""
    return await _json_get(
        _settings()["docker_api_url"],
        "/containers/json",
        params={"all": "1" if all_containers else "0"},
    )


@mcp.tool()
async def docker_inspect_container(container: str) -> dict[str, Any]:
    """读取 Docker 容器配置与状态；不返回环境变量以避免泄露 Secret。"""
    container = _resource_name(container, "container")
    result = await _json_get(
        _settings()["docker_api_url"], f"/containers/{container}/json"
    )
    data = result.get("data")
    if isinstance(data, dict):
        config_data = dict(data.get("Config") or {})
        config_data.pop("Env", None)
        result["data"] = {
            "Id": data.get("Id"),
            "Name": data.get("Name"),
            "State": data.get("State"),
            "Config": config_data,
            "NetworkSettings": data.get("NetworkSettings"),
        }
    return result


@mcp.tool()
async def docker_container_stats(container: str) -> dict[str, Any]:
    """读取 Docker 容器单次 CPU、内存和网络统计快照。"""
    container = _resource_name(container, "container")
    return await _json_get(
        _settings()["docker_api_url"],
        f"/containers/{container}/stats",
        params={"stream": "false", "one-shot": "true"},
    )


@mcp.tool()
async def probe_http(url: str, method: Literal["GET", "HEAD"] = "HEAD") -> dict[str, Any]:
    """对白名单主机执行只读 HTTP/HTTPS 探测，默认禁止公网与重定向。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("仅支持带主机名的 http/https URL")
    _allowed_diagnostic_host(parsed.hostname)
    started = time.perf_counter()
    async with httpx.AsyncClient(
        timeout=_settings()["diagnostic_timeout"],
        follow_redirects=False,
    ) as client:
        response = await client.request(method, url)
    return {
        "read_only": True,
        "url": url,
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type"),
        "content_length": response.headers.get("content-length"),
        "location": response.headers.get("location"),
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


@mcp.tool()
async def resolve_dns(host: str) -> dict[str, Any]:
    """解析白名单主机的 A/AAAA 地址，用于只读 DNS 诊断。"""
    host = _allowed_diagnostic_host(host)
    started = time.perf_counter()
    loop = asyncio.get_running_loop()
    values = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    addresses = sorted({value[4][0] for value in values})
    return {
        "read_only": True,
        "host": host,
        "addresses": addresses,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _tls_certificate_sync(host: str, port: int) -> dict[str, Any]:
    context = ssl.create_default_context()
    with socket.create_connection(
        (host, port), timeout=_settings()["diagnostic_timeout"]
    ) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=host) as tls_socket:
            certificate = tls_socket.getpeercert()
            cipher = tls_socket.cipher()
    return {
        "subject": certificate.get("subject"),
        "issuer": certificate.get("issuer"),
        "serial_number": certificate.get("serialNumber"),
        "not_before": certificate.get("notBefore"),
        "not_after": certificate.get("notAfter"),
        "subject_alt_names": certificate.get("subjectAltName"),
        "cipher": cipher,
    }


@mcp.tool()
async def inspect_tls_certificate(host: str, port: int = 443) -> dict[str, Any]:
    """读取白名单主机 TLS 证书有效期、颁发者和 SAN，不发送业务请求。"""
    host = _allowed_diagnostic_host(host)
    if not 1 <= port <= 65535:
        raise ValueError("port 必须在 1..65535")
    started = time.perf_counter()
    certificate = await asyncio.to_thread(_tls_certificate_sync, host, port)
    return {
        "read_only": True,
        "host": host,
        "port": port,
        **certificate,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


@mcp.tool()
async def redis_info(
    section: Literal["server", "clients", "memory", "stats", "replication", "keyspace"] = "memory",
) -> dict[str, Any]:
    """读取 Redis INFO 指定安全分区，不读取 Key 名称或 Value。"""
    redis_url = _settings()["redis_diagnostic_url"]
    if not redis_url:
        return {"available": False, "read_only": True, "reason": "redis_not_configured"}
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        data = await client.info(section=section)
    finally:
        await client.aclose()
    return {"available": True, "read_only": True, "section": section, "data": data}


@mcp.tool()
async def redis_slowlog(limit: int = 20) -> dict[str, Any]:
    """读取 Redis Slowlog 的耗时与命令名，参数统一脱敏。"""
    redis_url = _settings()["redis_diagnostic_url"]
    if not redis_url:
        return {"available": False, "read_only": True, "reason": "redis_not_configured"}
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        rows = await client.slowlog_get(min(max(1, limit), 100))
    finally:
        await client.aclose()
    sanitized = []
    for row in rows:
        command = row.get("command", "")
        command_name = str(command).split(maxsplit=1)[0] if command else ""
        sanitized.append(
            {
                "id": row.get("id"),
                "start_time": row.get("start_time"),
                "duration_microseconds": row.get("duration"),
                "command": command_name,
            }
        )
    return {"available": True, "read_only": True, "entries": sanitized}


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
    import uvicorn

    from app.observability.tracing import instrument_asgi_app

    host = os.getenv("MCP_OPS_HOST", "127.0.0.1")
    port = int(os.getenv("MCP_OPS_PORT", "8005"))
    logger.info(f"启动 Ops MCP Server: http://{host}:{port}/mcp")
    app = mcp.http_app(path="/mcp", transport="streamable-http")
    uvicorn.run(instrument_asgi_app(app), host=host, port=port)
