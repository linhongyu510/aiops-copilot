"""Alertmanager 告警指纹（零依赖版本）。

严格对齐 ``app.services.triggers.AlertmanagerTrigger.fingerprint()``，
指纹字节级一致；本模块只做纯函数，不引入 dataclass 与状态。
"""

from __future__ import annotations

import hashlib
from typing import Any


def alert_fingerprint(alert: dict[str, Any]) -> str:
    """按旧版 AlertmanagerTrigger 逻辑生成指纹（zero-dep, pure function）。

    优先级：
    1. 若载荷已有 ``fingerprint`` 字段，直接透传（str）；
    2. 否则用 ``labels.alertname@labels.instance``，缺失时回退 ``title`` / ``job`` / ``unknown``。
    """
    if not isinstance(alert, dict):
        return "unknown@unknown"

    if alert.get("fingerprint"):
        return str(alert["fingerprint"])

    labels_raw = alert.get("labels") or {}
    labels: dict[str, Any] = labels_raw if isinstance(labels_raw, dict) else {}
    name = (
        labels.get("alertname")
        or alert.get("title")
        or "unknown"
    )
    instance = (
        labels.get("instance")
        or labels.get("job")
        or "unknown"
    )
    return f"{name}@{instance}"


def stable_fingerprint(*parts: str) -> str:
    """给非 Alertmanager 入口（chat / cron / slash 命令）使用的 blake2b 指纹。

    与 ``app.services.triggers._stable_fingerprint`` 字节级一致。
    """
    cleaned = [str(p).strip() for p in parts if p is not None and str(p).strip()]
    if not cleaned:
        cleaned = ["_empty"]
    kind = cleaned[0]
    payload = "|".join(cleaned)
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()
    return f"{kind}:{digest}"
