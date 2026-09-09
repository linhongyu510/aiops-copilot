"""Tests for the optional integration mechanism and the WINDOS reference integration.

Two invariants matter here:
1. A default deployment loads no integration at all — the core must not depend
   on any external platform.
2. When an integration *is* enabled, its read-only tools and routing keywords
   become available without the core hardcoding its name.
"""

import pytest

from app.agent.tool_registry import ToolRegistry, tool_registry
from integrations import loader, windos

# --------------------------------------------------------------------------- #
# opt-in gate
# --------------------------------------------------------------------------- #


def test_no_integration_is_enabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv(loader.ENV_VAR, raising=False)
    assert loader.enabled_integration_names() == ()
    assert loader.load_enabled_integrations() == ()
    assert loader.extra_tool_groups() == ()


def test_enabled_names_are_parsed_and_deduplicated(monkeypatch) -> None:
    monkeypatch.setenv(loader.ENV_VAR, " windos , WINDOS ,, windos")
    assert loader.enabled_integration_names() == ("windos",)


def test_unknown_integration_is_skipped_without_raising(monkeypatch) -> None:
    monkeypatch.setenv(loader.ENV_VAR, "does-not-exist")
    assert loader.load_enabled_integrations() == ()


def test_enabling_windos_contributes_routing_keywords(monkeypatch) -> None:
    monkeypatch.setenv(loader.ENV_VAR, "windos")
    groups = loader.extra_tool_groups()
    assert (("windos", "生产就绪", "排程", "治理"), ("windos_",)) in groups


def test_core_catalog_has_no_vendor_specific_tools() -> None:
    """The shipped catalog must stay vendor-neutral."""
    fresh = ToolRegistry()
    assert not [name for name in fresh.catalog if name.startswith("windos_")]


def test_core_default_prefixes_exclude_optional_integrations() -> None:
    from app.agent.tool_registry import DEFAULT_TOOL_PREFIXES, TOOL_GROUPS

    assert not any(prefix.startswith("windos_") for prefix in DEFAULT_TOOL_PREFIXES)
    flattened = [prefix for _, prefixes in TOOL_GROUPS for prefix in prefixes]
    assert not any(prefix.startswith("windos_") for prefix in flattened)


def test_registering_specs_publishes_read_only_metadata() -> None:
    windos.register_tool_specs()
    spec = tool_registry.spec_for("windos_diagnose_overview")
    assert spec.read_only is True
    assert spec.risk_level == 0
    assert spec.category == "windos"


def test_register_mcp_tools_binds_stable_tool_names() -> None:
    """Names must stay ``windos_*`` so the routing prefix keeps working."""
    bound: list[str] = []

    class FakeMCP:
        def tool(self, name=None, **_kwargs):
            def decorator(function):
                bound.append(name)
                return function

            return decorator

    registered = windos.register_mcp_tools(FakeMCP())
    assert registered == (
        "windos_health",
        "windos_queue_status",
        "windos_agent_metrics",
        "windos_governance_status",
        "windos_recent_audit",
        "windos_diagnose_overview",
    )
    assert bound == list(registered)


# --------------------------------------------------------------------------- #
# tool behaviour (moved from tests/test_ops_server.py)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_health_only_uses_read_only_endpoints(monkeypatch) -> None:
    calls = []

    async def fake_get(path, parameters=None):
        calls.append((path, parameters))
        return {"endpoint": path, "ok": path.endswith("health"), "read_only": True}

    monkeypatch.setattr(windos, "windos_get", fake_get)
    result = await windos.health()
    assert result["read_only"] is True
    assert [path for path, _ in calls] == [
        "/api/v1/health",
        "/api/v1/ready",
        "/api/v1/production/readiness",
    ]


@pytest.mark.asyncio
async def test_overview_degrades_one_failed_check(monkeypatch) -> None:
    async def fake_get(path, parameters=None):
        if path.endswith("queue/status"):
            raise TimeoutError("fixture timeout")
        return {"endpoint": path, "ok": True, "read_only": True}

    monkeypatch.setattr(windos, "windos_get", fake_get)
    result = await windos.diagnose_overview()
    assert result["automatic_changes_permitted"] is False
    assert result["evidence"]["queue"]["ok"] is False
    assert result["evidence"]["queue"]["error_type"] == "TimeoutError"
    assert result["evidence"]["health"]["ok"] is True


@pytest.mark.asyncio
async def test_audit_limit_is_bounded(monkeypatch) -> None:
    async def fake_get(path, parameters=None):
        return {"path": path, "parameters": parameters}

    monkeypatch.setattr(windos, "windos_get", fake_get)
    result = await windos.recent_audit(500)
    assert result["parameters"] == {"limit": 100}


@pytest.mark.asyncio
async def test_provenance_is_retained_for_auditability(monkeypatch) -> None:
    """Answers must be traceable back to the endpoint that produced them."""

    class FakeResponse:
        status_code = 200
        is_success = True
        text = "{}"

        def json(self):
            return {"status": "ok"}

    class FakeClient:
        async def get(self, path, params=None, headers=None):
            return FakeResponse()

    class FakeGuard:
        async def call(self, thunk, failure_predicate=None):
            return await thunk()

    monkeypatch.setattr(windos, "_get_client", lambda: _awaitable(FakeClient()))
    monkeypatch.setattr(windos.dependency_guards, "get", lambda _name: FakeGuard())

    result = await windos.windos_get("/api/v1/health")
    assert result["source"] == "windos_live_api"
    assert result["endpoint"] == "/api/v1/health"
    assert result["read_only"] is True
    assert result["http_status"] == 200
    assert "latency_ms" in result


async def _awaitable(value):
    return value


def test_settings_read_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("WINDOS_BASE_URL", "http://example.invalid:9000/")
    monkeypatch.setenv("WINDOS_TIMEOUT_SECONDS", "3")
    current = windos.settings()
    assert current["base_url"] == "http://example.invalid:9000"
    assert current["timeout"] == 3


def test_every_declared_tool_is_read_only() -> None:
    """This integration is diagnosis-only; a write tool here would bypass approval."""
    assert windos.TOOL_SPECS
    for spec in windos.TOOL_SPECS:
        assert spec.read_only is True, spec.name
        assert spec.risk_level == 0, spec.name
        assert spec.name.startswith("windos_"), spec.name


@pytest.mark.asyncio
async def test_unreachable_dependency_propagates_instead_of_faking_success(
    monkeypatch,
) -> None:
    """A dead endpoint must raise, never return a fabricated healthy payload."""

    class FailingGuard:
        async def call(self, thunk, failure_predicate=None):
            raise ConnectionError("windos unreachable")

    class FakeClient:
        async def get(self, path, params=None, headers=None):  # pragma: no cover
            raise AssertionError("guard should short-circuit before the request")

    monkeypatch.setattr(windos, "_get_client", lambda: _awaitable(FakeClient()))
    monkeypatch.setattr(windos.dependency_guards, "get", lambda _name: FailingGuard())

    with pytest.raises(ConnectionError):
        await windos.windos_get("/api/v1/health")

    # The aggregate tool converts the failure into explicit per-check evidence
    # rather than dropping it.
    overview = await windos.diagnose_overview()
    assert overview["evidence"]["health"]["ok"] is False
    assert overview["evidence"]["health"]["error_type"] == "ConnectionError"
