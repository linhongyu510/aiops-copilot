"""Webhook 通知渠道：迁移 `IncidentService._notify` 的旧实现。"""

from __future__ import annotations

from typing import Any

import httpx

from app.services.notify.base import Notifier


class WebhookNotifier(Notifier):
    """通用 HTTP POST webhook。Payload 兼容旧格式 `{event, incident}`。"""

    name = "webhook"

    def __init__(self, url: str, *, timeout: float = 10.0, enabled: bool = True):
        super().__init__(enabled=enabled and bool(url))
        self.url = url
        self.timeout = timeout

    async def _deliver(self, incident: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.url,
                json={"event": "incident_update", "incident": incident},
            )
            response.raise_for_status()
