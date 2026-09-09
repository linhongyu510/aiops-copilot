"""Alertmanager 告警载荷归一化：把外部载荷抠出 title/description/labels。

原 ``app.services.triggers.AlertmanagerTrigger.from_alert`` 里包含 dataclass 状态；
这里只保留纯函数版本，返回 dict 而不是 Trigger 对象，减少上层耦合。
"""

from __future__ import annotations

from typing import Any


def normalize_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """从 Alertmanager 单条告警 dict 抽出诊断/匹配所需的关键字段。

    Returns:
        标准化后的 dict：
        - title: str（labels.alertname 或原 title）
        - description: str
        - labels: dict[str, str]
        - annotations: dict[str, str]
        - starts_at: str（可选）
        - source: str
        - raw: dict（原样透传）
    """
    if not isinstance(alert, dict):
        return {
            "title": "unknown",
            "description": "",
            "labels": {},
            "annotations": {},
            "starts_at": "",
            "source": "",
            "raw": {},
        }

    labels_raw = alert.get("labels", {}) or {}
    labels = {str(k): str(v) for k, v in labels_raw.items()} if isinstance(labels_raw, dict) else {}

    annotations_raw = alert.get("annotations", {}) or {}
    annotations = (
        {str(k): str(v) for k, v in annotations_raw.items()}
        if isinstance(annotations_raw, dict)
        else {}
    )

    title = (
        labels.get("alertname")
        or str(alert.get("title") or "")
        or "未命名告警"
    )
    description = (
        str(annotations.get("description") or "")
        or str(annotations.get("summary") or "")
        or str(alert.get("description") or "")
    )

    return {
        "title": title,
        "description": description,
        "labels": labels,
        "annotations": annotations,
        "starts_at": str(alert.get("startsAt") or ""),
        "source": str(alert.get("source") or "alertmanager"),
        "raw": dict(alert),
    }


def extract_alerts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """接受 Alertmanager webhook 或 CLI 载荷：

    - ``{"alerts": [{...}, ...]}`` → 直接取 alerts；
    - ``{"labels": {...}, ...}``   → 单条告警包装成列表；
    - ``[{...}, ...]``             → 直接透传（虽然 dict 参数类型，运行时用 isinstance 兼容）；
    - 其他形态 → 返回空列表。
    """
    if not payload:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    alerts = payload.get("alerts")
    if isinstance(alerts, list):
        return [item for item in alerts if isinstance(item, dict)]
    if "labels" in payload or "alertname" in payload or "annotations" in payload:
        return [payload]
    return []
