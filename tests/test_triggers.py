"""P0-4：TriggerSource 抽象与 `/api/events/generic` 端点。"""

import pytest
from fastapi.testclient import TestClient

from app.config import config
from app.main import app
from app.services.incident_service import IncidentService, build_diagnosis_prompt
from app.services.triggers import (
    AlertmanagerTrigger,
    ChatTrigger,
    ScheduledTrigger,
    SlashCommandTrigger,
    TriggerSource,
    parse_generic_payload,
)
from app.state_store import MemoryStateStore

client = TestClient(app)


# ---- TriggerSource 单元测试 ----


def test_alertmanager_trigger_from_alert_preserves_legacy_fingerprint():
    """AlertmanagerTrigger 指纹与 legacy `IncidentService._fingerprint` 完全一致。"""
    alert = {
        "labels": {"alertname": "HighCPU", "severity": "warning", "instance": "host-1"},
        "annotations": {"description": "CPU 高"},
    }
    trigger = AlertmanagerTrigger.from_alert(alert)
    assert trigger.kind == "alertmanager"
    assert trigger.title == "HighCPU"
    assert trigger.fingerprint() == "HighCPU@host-1"


def test_alertmanager_trigger_uses_explicit_fingerprint_when_present():
    alert = {"fingerprint": "abc123", "labels": {"alertname": "X"}}
    trigger = AlertmanagerTrigger.from_alert(alert)
    assert trigger.fingerprint() == "abc123"


def test_chat_trigger_fingerprint_differs_by_user():
    a = ChatTrigger(title="Redis 慢", user_id="userA")
    b = ChatTrigger(title="Redis 慢", user_id="userB")
    c = ChatTrigger(title="Redis 慢", user_id="userA")
    assert a.fingerprint() == c.fingerprint()
    assert a.fingerprint() != b.fingerprint()
    assert a.fingerprint().startswith("chat:")


def test_scheduled_trigger_fingerprint_groups_by_schedule():
    a = ScheduledTrigger(title="任意标题", schedule_name="daily-health-check")
    b = ScheduledTrigger(title="别的标题", schedule_name="daily-health-check")
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint().startswith("scheduled:")


def test_slash_command_trigger_fingerprint_uses_command_and_args():
    a = SlashCommandTrigger(title="/oncall diag", command="diagnose", args="es-red")
    b = SlashCommandTrigger(title="/oncall diag", command="diagnose", args="kafka-lag")
    assert a.fingerprint() != b.fingerprint()
    assert a.fingerprint().startswith("slash_command:")


def test_trigger_alert_dict_contains_labels_and_kind():
    trigger = ChatTrigger(
        title="Redis 慢",
        description="p99 3s",
        labels={"service": "profile", "severity": "warning"},
        user_id="userA",
    )
    payload = trigger.alert_dict()
    assert payload["trigger_kind"] == "chat"
    assert payload["labels"]["alertname"] == "Redis 慢"
    assert payload["labels"]["service"] == "profile"
    assert payload["annotations"]["description"] == "p99 3s"


def test_parse_generic_payload_dispatches_by_kind():
    chat = parse_generic_payload({"kind": "chat", "title": "求助", "user_id": "u1"})
    assert isinstance(chat, ChatTrigger)
    assert chat.user_id == "u1"

    sched = parse_generic_payload(
        {"kind": "scheduled", "title": "每日巡检", "schedule_name": "daily"}
    )
    assert isinstance(sched, ScheduledTrigger)
    assert sched.schedule_name == "daily"

    slash = parse_generic_payload(
        {"kind": "slash_command", "title": "/diag", "command": "diagnose", "args": "es"}
    )
    assert isinstance(slash, SlashCommandTrigger)
    assert slash.command == "diagnose"

    alert = parse_generic_payload(
        {
            "kind": "alertmanager",
            "title": "HighCPU",
            "labels": {"alertname": "HighCPU", "instance": "host-1"},
        }
    )
    assert isinstance(alert, AlertmanagerTrigger)


def test_parse_generic_payload_defaults_to_chat_for_unknown_kind():
    trigger = parse_generic_payload({"kind": "webhook-xyz", "title": "未知来源"})
    assert isinstance(trigger, ChatTrigger)


def test_parse_generic_payload_rejects_empty_title():
    with pytest.raises(ValueError):
        parse_generic_payload({"title": ""})


# ---- build_diagnosis_prompt 分类语气 ----


def _base_alert(kind: str = "alertmanager") -> dict:
    return {
        "labels": {"alertname": "SvcSlow", "severity": "warning", "instance": "api-1"},
        "annotations": {"description": "p99 高于阈值"},
        "trigger_kind": kind,
    }


def test_prompt_default_uses_alertmanager_wording():
    prompt = build_diagnosis_prompt(_base_alert())
    assert "【自动告警诊断任务】" in prompt
    assert "告警名称: SvcSlow" in prompt


def test_prompt_chat_wording():
    trigger = ChatTrigger(title="SvcSlow", user_id="opA")
    prompt = build_diagnosis_prompt(_base_alert("chat"), trigger=trigger)
    assert "【聊天/工单诊断请求】" in prompt
    assert "问题标题: SvcSlow" in prompt
    assert "聊天/工单入口" in prompt


def test_prompt_scheduled_wording():
    trigger = ScheduledTrigger(title="daily-health", schedule_name="daily-health")
    prompt = build_diagnosis_prompt(_base_alert("scheduled"), trigger=trigger)
    assert "【定时巡检诊断任务】" in prompt
    assert "无异常" in prompt  # hint 强调不要制造问题


def test_prompt_slash_command_wording():
    trigger = SlashCommandTrigger(title="/diagnose", command="diagnose", args="es-red")
    prompt = build_diagnosis_prompt(_base_alert("slash_command"), trigger=trigger)
    assert "【Slash 命令诊断请求】" in prompt
    assert "diagnose" in prompt


# ---- IncidentService.ingest_trigger 集成 ----


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(config, "incident_dedup_window_seconds", 300.0)
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", False)
    monkeypatch.setattr(config, "incident_store_max", 50)
    return IncidentService(store=MemoryStateStore())


async def test_ingest_chat_trigger_creates_incident_with_trigger_kind(service):
    trigger = ChatTrigger(
        title="Redis 慢",
        description="p99 3s",
        labels={"service": "profile"},
        user_id="userA",
    )
    result = await service.ingest_trigger(trigger)
    assert result["aggregated"] is False

    incident = await service.get_incident(result["incident_id"])
    assert incident["title"] == "Redis 慢"
    assert incident["trigger_kind"] == "chat"
    assert incident["latest_alert"]["trigger_kind"] == "chat"
    assert incident["latest_alert"]["labels"]["service"] == "profile"


async def test_ingest_alert_still_marks_incident_as_alertmanager(service):
    """向后兼容：ingest_alert 委托到 AlertmanagerTrigger，trigger_kind = alertmanager"""
    await service.ingest_alert(
        {
            "labels": {"alertname": "HighCPU", "instance": "host-1"},
            "annotations": {"description": "CPU 高"},
        }
    )
    incidents = await service.list_incidents()
    assert len(incidents) == 1
    assert incidents[0]["trigger_kind"] == "alertmanager"


async def test_different_trigger_kinds_do_not_aggregate(service):
    """不同 kind 的相同 title 不应归并（指纹前缀不同）。"""
    chat = ChatTrigger(title="RedisSlow", user_id="u1")
    slash = SlashCommandTrigger(title="RedisSlow", command="diagnose", args="")
    r1 = await service.ingest_trigger(chat)
    r2 = await service.ingest_trigger(slash)
    assert r2["aggregated"] is False
    assert r1["incident_id"] != r2["incident_id"]


async def test_repeated_chat_trigger_aggregates(service):
    """同 user_id + title 的 chat trigger 应归并。"""
    trigger = ChatTrigger(title="RedisSlow", user_id="u1")
    first = await service.ingest_trigger(trigger)
    second = await service.ingest_trigger(trigger)
    assert first["incident_id"] == second["incident_id"]
    assert second["aggregated"] is True


# ---- HTTP /api/events/generic ----


def test_events_generic_accepts_chat_payload(monkeypatch):
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", False)
    payload = {
        "kind": "chat",
        "title": "PaymentApiHighLatency",
        "description": "支付接口 p99 超过 2s",
        "labels": {"service": "payment"},
        "user_id": "op-alice",
    }
    response = client.post("/api/events/generic", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "chat"
    assert body["accepted"] == 1
    assert body["aggregated"] == 0
    incident_id = body["incident"]["incident_id"]
    detail = client.get(f"/api/incidents/{incident_id}").json()
    assert detail["trigger_kind"] == "chat"
    assert detail["title"] == "PaymentApiHighLatency"


def test_events_generic_scheduled_health_check(monkeypatch):
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", False)
    payload = {
        "kind": "scheduled",
        "title": "DailyDiskCheck",
        "schedule_name": "daily-disk-check",
        "source": "cron",
    }
    response = client.post("/api/events/generic", json=payload)
    assert response.status_code == 200
    assert response.json()["kind"] == "scheduled"


def test_events_generic_slash_command(monkeypatch):
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", False)
    payload = {
        "kind": "slash_command",
        "title": "/diagnose es-red",
        "command": "diagnose",
        "args": "es-red",
        "source": "feishu",
    }
    response = client.post("/api/events/generic", json=payload)
    assert response.status_code == 200
    assert response.json()["kind"] == "slash_command"


def test_events_generic_rejects_empty_title():
    response = client.post("/api/events/generic", json={"kind": "chat"})
    # Pydantic 422 (title 必填)
    assert response.status_code == 422


def test_events_generic_defaults_kind_to_chat(monkeypatch):
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", False)
    response = client.post("/api/events/generic", json={"title": "NoKindProvided"})
    assert response.status_code == 200
    assert response.json()["kind"] == "chat"


async def test_autonomous_diagnosis_uses_chat_prompt(monkeypatch):
    """自治诊断闭环：chat trigger 生成的 prompt 走 chat 措辞。"""
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", True)
    service = IncidentService(store=MemoryStateStore())

    captured: list[str] = []

    async def _fake_execute(prompt, session_id="default"):
        captured.append(prompt)
        yield {"type": "complete", "response": "根因：慢查询"}

    import app.services.aiops_service as aiops_service_module

    monkeypatch.setattr(aiops_service_module.aiops_service, "execute", _fake_execute)

    trigger = ChatTrigger(title="RedisSlow", user_id="u1")
    result = await service.ingest_trigger(trigger)

    import asyncio

    for _ in range(50):
        incident = await service.get_incident(result["incident_id"])
        if incident["status"] in ("diagnosed", "firing"):
            break
        await asyncio.sleep(0.02)

    assert captured, "诊断 prompt 未被捕获"
    assert "【聊天/工单诊断请求】" in captured[0]
    incident = await service.get_incident(result["incident_id"])
    assert incident["status"] == "diagnosed"
    assert "慢查询" in incident["diagnosis_report"]


# ---- TriggerSource 基类默认行为 ----


def test_base_trigger_defaults_kind_unknown_and_uses_alertmanager_prompt():
    """未知 kind 的 TriggerSource 应回退到 alertmanager 措辞。"""
    trigger = TriggerSource(title="X")
    prompt = build_diagnosis_prompt(trigger.alert_dict(), trigger=trigger)
    assert "【自动告警诊断任务】" in prompt
