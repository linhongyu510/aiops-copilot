"""飞书通知骨架（P1-1 占位）。

真正接入需要补齐飞书自建机器人 webhook + 卡片消息 schema。当前实现让
`notify()` 走 `NotImplementedError` 分支，被 base 基类降级为 warn+skip，
不影响 webhook / 其他渠道的实际投递。
"""

from __future__ import annotations

from typing import Any

from app.services.notify.base import Notifier


class FeishuNotifier(Notifier):
    name = "feishu"

    def __init__(self, webhook_url: str = "", *, enabled: bool = True):
        super().__init__(enabled=enabled)
        self.webhook_url = webhook_url

    async def _deliver(self, incident: dict[str, Any]) -> None:
        raise NotImplementedError("FeishuNotifier 尚未接入，请补齐 webhook 与卡片模板")
