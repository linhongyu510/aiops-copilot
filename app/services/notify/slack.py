"""Slack 通知骨架（P1-1 占位）。

`SlackNotifier` 保留统一接口和渠道注册路径，但不做真正投递：接入时补
`SLACK_WEBHOOK_URL` / bot token 与消息格式。当前实现让 `notify()` 走
`NotImplementedError` 分支，被 base 基类降级为 warn+skip，不影响其他渠道。
"""

from __future__ import annotations

from typing import Any

from app.services.notify.base import Notifier


class SlackNotifier(Notifier):
    name = "slack"

    def __init__(self, webhook_url: str = "", *, enabled: bool = True):
        super().__init__(enabled=enabled)
        self.webhook_url = webhook_url

    async def _deliver(self, incident: dict[str, Any]) -> None:
        raise NotImplementedError("SlackNotifier 尚未接入，请补齐 webhook 与消息模板")
