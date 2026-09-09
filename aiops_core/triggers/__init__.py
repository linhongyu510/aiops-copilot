"""Alertmanager 告警归一化 + 指纹（零依赖）。"""

from aiops_core.triggers.fingerprint import alert_fingerprint, stable_fingerprint
from aiops_core.triggers.normalize import extract_alerts, normalize_alert

__all__ = [
    "alert_fingerprint",
    "stable_fingerprint",
    "normalize_alert",
    "extract_alerts",
]
