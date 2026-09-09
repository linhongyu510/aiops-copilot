import pytest

from mcp_servers.ops_server import (
    _allowed_diagnostic_host,
    _validate_identifier,
    _validate_read_only_sql,
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


def test_diagnostic_host_is_restricted_by_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("DIAGNOSTIC_ALLOWED_HOSTS", "localhost,redis.internal")
    assert _allowed_diagnostic_host("LOCALHOST.") == "localhost"
    assert _allowed_diagnostic_host("redis.internal") == "redis.internal"
    with pytest.raises(PermissionError, match="白名单"):
        _allowed_diagnostic_host("metadata.google.internal")


def test_read_query_enforces_schema_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("MYSQL_DSN", "mysql+pymysql://reader:secret@localhost/observability")
    monkeypatch.setenv("MYSQL_ALLOWED_SCHEMAS", "observability")
    assert _validate_read_only_sql("SELECT * FROM observability.alerts LIMIT 1")
    with pytest.raises(PermissionError, match="未授权 schema"):
        _validate_read_only_sql("SELECT * FROM private.users LIMIT 1")


# ---- Loki 只读工具（P1.4）----


@pytest.mark.asyncio
async def test_loki_query_unconfigured_returns_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("LOKI_URL", raising=False)
    from mcp_servers.ops_server import loki_query

    result = await loki_query.fn('{app="api"}', "0", "1")
    assert result["available"] is False
    assert result["reason"] == "dependency_not_configured"
    assert result["read_only"] is True


@pytest.mark.asyncio
async def test_loki_query_validates_and_clamps(monkeypatch) -> None:
    from mcp_servers.ops_server import loki_query

    with pytest.raises(ValueError):
        await loki_query.fn("   ", "0", "1")
    with pytest.raises(ValueError):
        await loki_query.fn('{app="api"}', "", "1")
    with pytest.raises(ValueError):
        await loki_query.fn("q" * 2001, "0", "1")

    captured = {}

    async def fake_loki_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return {"available": True, "ok": True}

    monkeypatch.setattr("mcp_servers.ops_server._loki_get", fake_loki_get)
    result = await loki_query.fn('{app="api"} |= "error"', "0", "1", limit=9999)
    assert result["ok"] is True
    assert captured["path"] == "/loki/api/v1/query_range"
    # limit 被钳制到 500
    assert captured["params"]["limit"] == 500
    assert captured["params"]["direction"] == "backward"


@pytest.mark.asyncio
async def test_loki_labels_routes_label_values(monkeypatch) -> None:
    captured = {}

    async def fake_loki_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return {"available": True, "ok": True}

    from mcp_servers.ops_server import loki_labels

    monkeypatch.setattr("mcp_servers.ops_server._loki_get", fake_loki_get)
    await loki_labels.fn()
    assert captured["path"] == "/loki/api/v1/labels"
    assert captured["params"] is None

    await loki_labels.fn(label_name="app", start="0", end="1")
    assert captured["path"] == "/loki/api/v1/label/app/values"
    assert captured["params"] == {"start": "0", "end": "1"}

    with pytest.raises(ValueError):
        await loki_labels.fn(label_name="bad label!")
