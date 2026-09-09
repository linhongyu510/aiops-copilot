"""通知渠道插件包（P1-1）。

- `Notifier` 抽象基类：所有渠道共享 `notify(incident)` 契约与错误隔离逻辑；
- 内置渠道：`WebhookNotifier`（迁移旧实现）、`SlackNotifier` / `FeishuNotifier`（占位骨架）；
- `build_notifiers()` 按 `config.incident_notifiers` 组装启用列表；
- 渠道间**互不影响**：任一渠道失败/未接入，其他渠道仍会执行。
"""

from __future__ import annotations

from loguru import logger

from app.config import config
from app.services.notify.base import Notifier
from app.services.notify.feishu import FeishuNotifier
from app.services.notify.slack import SlackNotifier
from app.services.notify.webhook import WebhookNotifier

__all__ = [
    "Notifier",
    "WebhookNotifier",
    "SlackNotifier",
    "FeishuNotifier",
    "build_notifiers",
]


def build_notifiers() -> list[Notifier]:
    """按 `config.incident_notifiers` 组装 notifier 列表。

    - 逗号分隔：如 `webhook,slack`；空字符串走 legacy 兼容分支；
    - legacy 兼容：未设置 `AIOPS_INCIDENT_NOTIFIERS` 但设置了
      `AIOPS_INCIDENT_NOTIFY_WEBHOOK_URL` 时自动启用 webhook；
    - 未知渠道名跳过并打 warn（不当作错误）。
    """
    raw = (config.incident_notifiers or "").strip()
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]

    if not names and config.incident_notify_webhook_url:
        # legacy 单渠道行为：只配置 webhook URL 时默认启用
        names = ["webhook"]

    notifiers: list[Notifier] = []
    for name in names:
        if name == "webhook":
            url = config.incident_notify_webhook_url
            if not url:
                logger.warning("[notify] webhook 启用但未配置 URL，跳过")
                continue
            notifiers.append(WebhookNotifier(url=url))
        elif name == "slack":
            notifiers.append(SlackNotifier(webhook_url=config.incident_slack_webhook_url))
        elif name == "feishu":
            notifiers.append(FeishuNotifier(webhook_url=config.incident_feishu_webhook_url))
        else:
            logger.warning(f"[notify] 未知渠道名 {name!r}，跳过")
    return notifiers
