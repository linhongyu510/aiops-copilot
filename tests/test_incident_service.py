"""P1.1 + P2.1：告警接入 + incident 状态机 + 自治诊断（state_store 后端）"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import config
from app.main import app
from app.services.incident_service import (
    IncidentService,
    build_diagnosis_prompt,
)
from app.state_store import MemoryStateStore

client = TestClient(app)


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(config, "incident_dedup_window_seconds", 300.0)
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", False)
    monkeypatch.setattr(config, "incident_store_max", 50)
    return IncidentService(store=MemoryStateStore())


def _alert(name="HighCPU", instance="host-1", severity="warning", fingerprint=None):
    payload = {
        "status": "firing",
        "labels": {"alertname": name, "severity": severity, "instance": instance},
        "annotations": {"description": f"{name} on {instance}"},
        "startsAt": "2026-08-17T00:00:00Z",
    }
    if fingerprint:
        payload["fingerprint"] = fingerprint
    return payload


async def test_same_fingerprint_alerts_are_aggregated(service):
    first = await service.ingest_alert(_alert())
    assert first["aggregated"] is False
    second = await service.ingest_alert(_alert())
    third = await service.ingest_alert(_alert())
    assert second["aggregated"] is True
    assert third["aggregated"] is True
    assert {first["incident_id"], second["incident_id"], third["incident_id"]} == {
        first["incident_id"]
    }
    incidents = await service.list_incidents()
    assert len(incidents) == 1
    assert incidents[0]["alert_count"] == 3


async def test_different_alerts_create_separate_incidents(service):
    await service.ingest_alert(_alert(name="HighCPU", instance="host-1"))
    await service.ingest_alert(_alert(name="DiskFull", instance="host-1"))
    await service.ingest_alert(_alert(name="HighCPU", instance="host-2"))
    assert len(await service.list_incidents()) == 3


async def test_state_survives_service_restart_via_store(service):
    """P2.1：incident 持久化在存储中，新服务实例（模拟其他副本）可见"""
    created = await service.ingest_alert(_alert(name="CrossReplica"))
    incident_id = created["incident_id"]

    other_replica = IncidentService(store=service._store)
    incident = await other_replica.get_incident(incident_id)
    assert incident is not None
    assert incident["title"] == "CrossReplica"
    # 另一副本可以完成状态迁移
    result = await other_replica.transition(incident_id, "resolved", actor="replica-2")
    assert result["ok"] is True


async def test_resolved_incident_frees_fingerprint_for_new_alert(service):
    """终态后指纹索引清除：同指纹新告警创建新 incident 而非归并"""
    first = await service.ingest_alert(_alert(name="Flappy"))
    await service.transition(first["incident_id"], "resolved")
    second = await service.ingest_alert(_alert(name="Flappy"))
    assert second["aggregated"] is False
    assert second["incident_id"] != first["incident_id"]


async def test_manual_transitions_enforce_state_machine(service):
    result = await service.ingest_alert(_alert())
    incident_id = result["incident_id"]

    # firing → resolved 合法；resolved → resolved 非法
    assert (await service.transition(incident_id, "resolved"))["ok"] is True
    assert (await service.transition(incident_id, "resolved"))["ok"] is False
    assert (await service.transition(incident_id, "closed"))["ok"] is True
    assert (await service.transition(incident_id, "resolved"))["ok"] is False  # closed 是终态
    assert (await service.transition("inc-not-exist", "resolved"))["ok"] is False


def test_diagnosis_prompt_contains_alert_context(service):
    prompt = build_diagnosis_prompt(_alert(name="ESRed", severity="critical"))
    assert "ESRed" in prompt
    assert "critical" in prompt
    assert "host-1" in prompt
    assert "只读排查" in prompt


async def test_autonomous_diagnosis_lifecycle(monkeypatch):
    """自治诊断：firing → diagnosing → diagnosed，报告落库"""
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", True)
    monkeypatch.setattr(config, "incident_dedup_window_seconds", 300.0)
    service = IncidentService(store=MemoryStateStore())

    async def _fake_execute(prompt, session_id="default"):
        assert "ESRed" in prompt
        yield {"type": "status", "stage": "starting", "message": "启动"}
        yield {"type": "complete", "stage": "complete", "response": "# 根因：磁盘水位超限"}

    import app.services.aiops_service as aiops_service_module

    monkeypatch.setattr(
        aiops_service_module.aiops_service, "execute", _fake_execute
    )

    result = await service.ingest_alert(_alert(name="ESRed"))
    incident_id = result["incident_id"]
    # 等后台诊断任务完成
    for _ in range(50):
        if (await service.get_incident(incident_id))["status"] in ("diagnosed", "firing"):
            break
        await asyncio.sleep(0.02)

    incident = await service.get_incident(incident_id)
    assert incident["status"] == "diagnosed"
    assert "磁盘水位超限" in incident["diagnosis_report"]
    assert incident["session_id"] == f"incident-{incident_id}"
    events = [item["event"] for item in incident["timeline"]]
    assert "diagnosis_started" in events
    assert "diagnosis_completed" in events


async def test_autonomous_diagnosis_failure_falls_back_to_firing(monkeypatch):
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", True)
    service = IncidentService(store=MemoryStateStore())

    async def _failing_execute(prompt, session_id="default"):
        yield {"type": "error", "stage": "error", "message": "模型不可用"}

    import app.services.aiops_service as aiops_service_module

    monkeypatch.setattr(aiops_service_module.aiops_service, "execute", _failing_execute)

    result = await service.ingest_alert(_alert(name="Boom"))
    incident_id = result["incident_id"]
    for _ in range(50):
        if (await service.get_incident(incident_id))["status"] == "firing":
            break
        await asyncio.sleep(0.02)

    incident = await service.get_incident(incident_id)
    assert incident["status"] == "firing"
    assert "模型不可用" in incident["diagnosis_error"]


# ------------------- P1-2 C6：severity 差异化 dedup 窗口 -------------------


def test_c6_extract_severity_from_labels_and_toplevel():
    """C6：severity 优先取 labels，未提供时回落顶层字段。"""
    from app.services.incident_service import _extract_severity

    assert _extract_severity({"labels": {"severity": "CRITICAL"}}) == "critical"
    assert _extract_severity({"severity": "error"}) == "error"
    assert _extract_severity({"labels": {}}) == ""
    assert _extract_severity(None) == ""


def test_c6_dedup_window_uses_profile_config(monkeypatch):
    """C6：severity 命中 profile 配置时使用该窗口值。"""
    from app.agent.profiles.loader import DomainProfile
    from app.services.incident_service import _dedup_window_for_alert

    override = DomainProfile(
        name="test",
        incident_dedup_windows_by_severity=(("critical", 30.0), ("warning", 200.0)),
        source="test:inline",
    )
    import app.agent.profiles as profiles_pkg

    monkeypatch.setattr(profiles_pkg, "get_active_profile", lambda: override)
    monkeypatch.setattr(config, "incident_dedup_window_seconds", 300.0)

    assert _dedup_window_for_alert({"labels": {"severity": "critical"}}) == 30.0
    assert _dedup_window_for_alert({"labels": {"severity": "warning"}}) == 200.0
    # 未配置的 severity 走全局默认
    assert _dedup_window_for_alert({"labels": {"severity": "info"}}) == 300.0
    # 无 severity 走全局默认
    assert _dedup_window_for_alert({"labels": {}}) == 300.0


def test_c6_dedup_window_falls_back_when_profile_broken(monkeypatch):
    """C6：profile 层异常时保持全局默认窗口。"""
    from app.services.incident_service import _dedup_window_for_alert

    def _boom():
        raise RuntimeError("broken")

    import app.agent.profiles as profiles_pkg

    monkeypatch.setattr(profiles_pkg, "get_active_profile", _boom)
    monkeypatch.setattr(config, "incident_dedup_window_seconds", 300.0)
    assert _dedup_window_for_alert({"labels": {"severity": "critical"}}) == 300.0


async def test_shutdown_cancels_pending_tasks(monkeypatch):
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", True)
    service = IncidentService(store=MemoryStateStore())

    async def _hanging_execute(prompt, session_id="default"):
        await asyncio.sleep(30)
        yield {"type": "complete", "response": "never"}

    import app.services.aiops_service as aiops_service_module

    monkeypatch.setattr(aiops_service_module.aiops_service, "execute", _hanging_execute)
    await service.ingest_alert(_alert(name="Hang"))
    assert service._tasks
    await service.shutdown()
    assert not service._tasks


# ---- HTTP API ----


def test_events_alert_api_creates_incident(monkeypatch):
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", False)
    payload = {
        "alerts": [
            _alert(name="ApiHigh5xxR2", instance="api-1", severity="critical"),
            _alert(name="ApiHigh5xxR2", instance="api-1", severity="critical"),
        ]
    }
    response = client.post("/api/events/alert", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["aggregated"] == 1


def test_events_alert_api_rejects_empty_payload():
    response = client.post("/api/events/alert", json={})
    assert response.status_code == 422


def test_incidents_listing_and_detail(monkeypatch):
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", False)
    created = client.post(
        "/api/events/alert", json={"title": "LatencyHighR2", "severity": "warning"}
    ).json()
    incident_id = created["incidents"][0]["incident_id"]

    listing = client.get("/api/incidents")
    assert listing.status_code == 200
    assert any(
        item["incident_id"] == incident_id for item in listing.json()["incidents"]
    )

    detail = client.get(f"/api/incidents/{incident_id}")
    assert detail.status_code == 200
    assert detail.json()["title"] == "LatencyHighR2"

    assert client.get("/api/incidents/inc-missing").status_code == 404


def test_incident_resolve_and_close(monkeypatch):
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", False)
    created = client.post(
        "/api/events/alert", json={"title": "NodeDownR2", "severity": "critical"}
    ).json()
    incident_id = created["incidents"][0]["incident_id"]

    resolved = client.post(f"/api/incidents/{incident_id}/resolve", json={"actor": "op-1"})
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"

    closed = client.post(f"/api/incidents/{incident_id}/close", json={"actor": "op-1"})
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"

    # closed 是终态，再次 resolve 返回 409
    assert client.post(f"/api/incidents/{incident_id}/resolve").status_code == 409
