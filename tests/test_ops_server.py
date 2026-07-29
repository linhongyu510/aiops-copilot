import pytest

from mcp_servers.ops_server import (
    _validate_identifier,
    _validate_read_only_sql,
    windos_diagnose_overview,
    windos_health,
    windos_recent_audit,
)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM alerts LIMIT 10",
        "show tables",
        "DESCRIBE alert_events",
        "EXPLAIN SELECT * FROM alerts",
    ],
)
def test_read_only_sql_accepts_safe_statements(statement: str) -> None:
    assert _validate_read_only_sql(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM alerts",
        "SELECT * FROM alerts; DROP TABLE alerts",
        "SELECT * FROM alerts -- bypass",
        "WITH removed AS (DELETE FROM alerts RETURNING *) SELECT * FROM removed",
        "SELECT * FROM alerts INTO OUTFILE '/tmp/a'",
        "SELECT SLEEP(10)",
        "SELECT BENCHMARK(1000000, SHA2('x', 256))",
        "SELECT LOAD_FILE('/etc/passwd')",
        "SHOW DATABASES",
    ],
)
def test_read_only_sql_rejects_unsafe_statements(statement: str) -> None:
    with pytest.raises((PermissionError, ValueError)):
        _validate_read_only_sql(statement)


def test_identifier_validation() -> None:
    assert _validate_identifier("alert_events_2026", "table") == "alert_events_2026"
    with pytest.raises(ValueError):
        _validate_identifier("alerts;drop", "table")


def test_read_query_enforces_schema_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("MYSQL_DSN", "mysql+pymysql://reader:secret@localhost/observability")
    monkeypatch.setenv("MYSQL_ALLOWED_SCHEMAS", "observability")
    assert _validate_read_only_sql("SELECT * FROM observability.alerts LIMIT 1")
    with pytest.raises(PermissionError, match="未授权 schema"):
        _validate_read_only_sql("SELECT * FROM private.users LIMIT 1")


@pytest.mark.asyncio
async def test_windos_health_only_uses_read_only_endpoints(monkeypatch) -> None:
    calls = []

    async def fake_get(path, parameters=None):
        calls.append((path, parameters))
        return {"endpoint": path, "ok": path.endswith("health"), "read_only": True}

    monkeypatch.setattr("mcp_servers.ops_server._windos_get", fake_get)
    result = await windos_health.fn()
    assert result["read_only"] is True
    assert [path for path, _ in calls] == [
        "/api/v1/health",
        "/api/v1/ready",
        "/api/v1/production/readiness",
    ]


@pytest.mark.asyncio
async def test_windos_overview_degrades_one_failed_check(monkeypatch) -> None:
    async def fake_get(path, parameters=None):
        if path.endswith("queue/status"):
            raise TimeoutError("fixture timeout")
        return {"endpoint": path, "ok": True, "read_only": True}

    monkeypatch.setattr("mcp_servers.ops_server._windos_get", fake_get)
    result = await windos_diagnose_overview.fn()
    assert result["automatic_changes_permitted"] is False
    assert result["evidence"]["queue"]["ok"] is False
    assert result["evidence"]["queue"]["error_type"] == "TimeoutError"
    assert result["evidence"]["health"]["ok"] is True


@pytest.mark.asyncio
async def test_windos_audit_limit_is_bounded(monkeypatch) -> None:
    async def fake_get(path, parameters=None):
        return {"path": path, "parameters": parameters}

    monkeypatch.setattr("mcp_servers.ops_server._windos_get", fake_get)
    result = await windos_recent_audit.fn(500)
    assert result["parameters"] == {"limit": 100}
