"""P1-1：通知渠道插件化 —— 抽象、渠道错误隔离、配置驱动的组装。"""

from typing import Any

import pytest

from app.config import config
from app.services.incident_service import IncidentService
from app.services.notify import (
    FeishuNotifier,
    Notifier,
    SlackNotifier,
    WebhookNotifier,
    build_notifiers,
)
from app.state_store import MemoryStateStore

# ---- base Notifier 错误隔离 ----


class _AlwaysOk(Notifier):
    name = "always-ok"

    def __init__(self):
        super().__init__(enabled=True)
        self.delivered: list[dict[str, Any]] = []

    async def _deliver(self, incident):
        self.delivered.append(incident)


class _AlwaysFail(Notifier):
    name = "always-fail"

    def __init__(self):
        super().__init__(enabled=True)

    async def _deliver(self, incident):
        raise RuntimeError("boom")


class _NotImpl(Notifier):
    name = "not-impl"

    def __init__(self):
        super().__init__(enabled=True)

    async def _deliver(self, incident):
        raise NotImplementedError("skeleton")


class _Disabled(Notifier):
    name = "disabled"

    def __init__(self):
        super().__init__(enabled=False)
        self.calls = 0

    async def _deliver(self, incident):
        self.calls += 1


async def test_notify_isolates_channel_failures():
    ok = _AlwaysOk()
    fail = _AlwaysFail()
    incident = {"incident_id": "inc-1"}
    assert await ok.notify(incident) is True
    assert await fail.notify(incident) is False
    assert ok.delivered == [incident]


async def test_notify_treats_notimplemented_as_skip_not_failure():
    n = _NotImpl()
    # 不抛异常；返回 False 表示未真正投递（骨架未接入）
    assert await n.notify({"incident_id": "x"}) is False


async def test_notify_respects_disabled_flag():
    n = _Disabled()
    assert await n.notify({"incident_id": "x"}) is False
    assert n.calls == 0


# ---- WebhookNotifier 集成 ----


class _FakeHttpxResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttpxClient:
    """替代 httpx.AsyncClient 的最小实现；`captured` 收集调用现场。"""

    captured: list[dict[str, Any]] = []
    status_code: int = 200

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, *, json=None, **kwargs):
        _FakeHttpxClient.captured.append({"url": url, "json": json})
        return _FakeHttpxResponse(_FakeHttpxClient.status_code)


@pytest.fixture(autouse=False)
def fake_httpx(monkeypatch):
    _FakeHttpxClient.captured = []
    _FakeHttpxClient.status_code = 200
    import app.services.notify.webhook as webhook_module

    monkeypatch.setattr(webhook_module.httpx, "AsyncClient", _FakeHttpxClient)
    return _FakeHttpxClient


async def test_webhook_notifier_posts_payload(fake_httpx):
    notifier = WebhookNotifier("https://hook.example.com/incidents")
    ok = await notifier.notify({"incident_id": "inc-42", "title": "X"})
    assert ok is True
    assert len(fake_httpx.captured) == 1
    call = fake_httpx.captured[0]
    assert call["url"] == "https://hook.example.com/incidents"
    assert call["json"]["event"] == "incident_update"
    assert call["json"]["incident"]["incident_id"] == "inc-42"


async def test_webhook_notifier_handles_non_2xx_as_failure(fake_httpx):
    fake_httpx.status_code = 500
    notifier = WebhookNotifier("https://hook.example.com/x")
    assert await notifier.notify({"incident_id": "y"}) is False


async def test_webhook_notifier_disabled_when_url_empty():
    notifier = WebhookNotifier(url="")
    # enabled=False 时不应触发 _deliver
    assert await notifier.notify({"incident_id": "z"}) is False


# ---- 骨架渠道 ----


async def test_slack_and_feishu_are_skeletons_that_warn():
    slack = SlackNotifier(webhook_url="https://slack.example.com/x")
    feishu = FeishuNotifier(webhook_url="https://open.feishu.cn/x")
    # 骨架不真正投递，返回 False 但不抛异常
    assert await slack.notify({"incident_id": "a"}) is False
    assert await feishu.notify({"incident_id": "a"}) is False


# ---- build_notifiers 配置驱动 ----


def test_build_notifiers_defaults_to_empty_when_no_config(monkeypatch):
    monkeypatch.setattr(config, "incident_notifiers", "")
    monkeypatch.setattr(config, "incident_notify_webhook_url", "")
    monkeypatch.setattr(config, "incident_slack_webhook_url", "")
    monkeypatch.setattr(config, "incident_feishu_webhook_url", "")
    assert build_notifiers() == []


def test_build_notifiers_legacy_webhook_fallback(monkeypatch):
    """只配置 WEBHOOK_URL、未设置 NOTIFIERS 时兼容旧行为：自动启用 webhook。"""
    monkeypatch.setattr(config, "incident_notifiers", "")
    monkeypatch.setattr(
        config, "incident_notify_webhook_url", "https://hook.example.com/x"
    )
    notifiers = build_notifiers()
    assert len(notifiers) == 1
    assert isinstance(notifiers[0], WebhookNotifier)
    assert notifiers[0].url == "https://hook.example.com/x"


def test_build_notifiers_multi_channel(monkeypatch):
    monkeypatch.setattr(config, "incident_notifiers", "webhook,slack,feishu")
    monkeypatch.setattr(config, "incident_notify_webhook_url", "https://hook/x")
    monkeypatch.setattr(config, "incident_slack_webhook_url", "https://slack/x")
    monkeypatch.setattr(config, "incident_feishu_webhook_url", "https://feishu/x")
    notifiers = build_notifiers()
    assert [n.name for n in notifiers] == ["webhook", "slack", "feishu"]


def test_build_notifiers_skips_webhook_without_url(monkeypatch):
    """`webhook` 启用但 URL 为空 → 跳过 webhook；其他渠道不受影响。"""
    monkeypatch.setattr(config, "incident_notifiers", "webhook,slack")
    monkeypatch.setattr(config, "incident_notify_webhook_url", "")
    monkeypatch.setattr(config, "incident_slack_webhook_url", "")
    notifiers = build_notifiers()
    assert [n.name for n in notifiers] == ["slack"]


def test_build_notifiers_ignores_unknown_channel(monkeypatch):
    monkeypatch.setattr(config, "incident_notifiers", "webhook,carrier-pigeon")
    monkeypatch.setattr(config, "incident_notify_webhook_url", "https://hook/x")
    notifiers = build_notifiers()
    assert [n.name for n in notifiers] == ["webhook"]


# ---- IncidentService._notify 集成 ----


def _fake_incident(service: IncidentService):
    """构造一个 minimal Incident，避免走完整 ingest 流程。"""
    from app.services.incident_service import Incident

    incident = Incident(fingerprint="fp-1", title="T", alert={}, trigger_kind="chat")
    return incident


async def test_incident_notify_iterates_all_channels():
    ok = _AlwaysOk()
    fail = _AlwaysFail()
    service = IncidentService(store=MemoryStateStore(), notifiers=[ok, fail])
    incident = _fake_incident(service)
    await service._notify(incident)
    # 每个 notifier 都被调用一次；失败不影响成功渠道
    assert len(ok.delivered) == 1
    assert ok.delivered[0]["incident_id"] == incident.incident_id


async def test_incident_notify_skips_when_no_channels():
    service = IncidentService(store=MemoryStateStore(), notifiers=[])
    incident = _fake_incident(service)
    # 不抛异常，直接返回
    await service._notify(incident)


async def test_incident_notify_uses_config_when_notifiers_not_injected(monkeypatch):
    """未显式注入时应按 config 动态组装 notifier。"""
    monkeypatch.setattr(config, "incident_notifiers", "")
    monkeypatch.setattr(
        config, "incident_notify_webhook_url", "https://hook.example.com/x"
    )
    service = IncidentService(store=MemoryStateStore())
    notifiers = service._notifiers
    assert len(notifiers) == 1
    assert isinstance(notifiers[0], WebhookNotifier)


# ---- end-to-end：诊断完成后触发 notify ----


async def test_diagnosis_completion_notifies_all_channels(monkeypatch):
    monkeypatch.setattr(config, "incident_autonomous_diagnosis_enabled", True)
    ok = _AlwaysOk()
    fail = _AlwaysFail()
    service = IncidentService(store=MemoryStateStore(), notifiers=[ok, fail])

    async def _fake_execute(prompt, session_id="default"):
        yield {"type": "complete", "response": "# 根因：X"}

    import app.services.aiops_service as aiops_service_module

    monkeypatch.setattr(aiops_service_module.aiops_service, "execute", _fake_execute)

    result = await service.ingest_alert(
        {"labels": {"alertname": "NotifyMe", "instance": "host-1"}}
    )

    import asyncio

    for _ in range(50):
        incident = await service.get_incident(result["incident_id"])
        if incident["status"] in ("diagnosed", "firing"):
            break
        await asyncio.sleep(0.02)

    # 两个渠道都被调用；失败渠道不阻塞成功渠道
    assert len(ok.delivered) == 1
    assert ok.delivered[0]["incident_id"] == result["incident_id"]
