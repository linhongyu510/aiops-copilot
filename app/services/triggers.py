"""TriggerSource：把「触发一次诊断」的入口抽象成统一类型（P0-4）。

一次诊断可能来自：
- Alertmanager webhook（AlertmanagerTrigger）
- 用户在聊天里发起（ChatTrigger）
- 定时巡检（ScheduledTrigger）
- IM Slash 命令（SlashCommandTrigger）

不同入口的**载荷**、**指纹策略**、**诊断 prompt** 不一样，但下游
`IncidentService` 只关心「拿到一份可诊断的语义化事件描述」。Trigger 抽象
把入口差异吸收在类型里，让 IncidentService / Notifier 只对 `Trigger`
编程。

设计边界（避免过早抽象）：
- 只保留下游需要的 3 类信息：`title` / `alert_dict()` / `fingerprint()`；
- 不引入插件注册机制 —— P0-4 只有 4 个内置来源，等真正接入第 5 个来源
  再看是否需要动态注册；
- 向后兼容：`AlertmanagerTrigger.fingerprint()` 与旧
  `IncidentService._fingerprint` 结果一致，`alert_dict()` 直接透传原告警，
  这样 legacy 告警行为零变化。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass
class TriggerSource:
    """Trigger 抽象基类。所有子类需要提供 title / labels / alert_dict / fingerprint。"""

    #: Trigger 的稳定类型标识，用于日志、指标、profile 模板选择。
    kind: ClassVar[str] = "unknown"

    title: str
    description: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    #: 冗余的自由文本 source 标识（如 `chat:userA`、`cron:daily-check`）。
    source: str = ""
    #: 触发时间（ISO8601 字符串），可选。
    starts_at: str = ""
    #: 原始 payload，仅在需要复盘时使用；不会进入指纹与 prompt。
    raw: dict[str, Any] = field(default_factory=dict)

    def alert_dict(self) -> dict[str, Any]:
        """转换为下游 IncidentService 期望的 alert 字典。

        默认实现覆盖了 legacy `build_diagnosis_prompt` 使用的所有字段：
        labels.alertname / labels.severity / labels.instance /
        annotations.description / title / description / startsAt。
        """
        labels = dict(self.labels)
        labels.setdefault("alertname", self.title)
        annotations: dict[str, str] = {}
        if self.description:
            annotations["description"] = self.description
        payload: dict[str, Any] = {
            "labels": labels,
            "annotations": annotations,
            "title": self.title,
            "description": self.description,
        }
        if self.starts_at:
            payload["startsAt"] = self.starts_at
        if self.source:
            payload["source"] = self.source
        payload["trigger_kind"] = self.kind
        return payload

    def fingerprint(self) -> str:
        """默认按 kind + title + 关键 label 计算指纹。子类可覆盖。"""
        labels = self.labels or {}
        instance = labels.get("instance") or labels.get("job") or ""
        return _stable_fingerprint(self.kind, self.title, instance)

    def diagnosis_hint(self) -> str:
        """向 build_diagnosis_prompt 提供额外的上下文说明。默认空串。"""
        return ""


@dataclass
class AlertmanagerTrigger(TriggerSource):
    """Prometheus Alertmanager webhook 载荷。"""

    kind: ClassVar[str] = "alertmanager"

    #: 原始告警字典（包含 labels / annotations / fingerprint / startsAt）。
    alert: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_alert(cls, alert: dict[str, Any]) -> AlertmanagerTrigger:
        """从 Alertmanager webhook 单条告警 dict 构造 Trigger。"""
        labels = dict(alert.get("labels", {}) or {})
        annotations = alert.get("annotations", {}) or {}
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
        return cls(
            title=title,
            description=description,
            labels=labels,
            source=str(alert.get("source") or "alertmanager"),
            starts_at=str(alert.get("startsAt") or ""),
            raw=dict(alert),
            alert=dict(alert),
        )

    def alert_dict(self) -> dict[str, Any]:
        """直接透传原告警，保证 legacy 指纹与 prompt 与旧版一致。"""
        merged = dict(self.alert)
        merged.setdefault("labels", dict(self.labels))
        merged.setdefault("trigger_kind", self.kind)
        return merged

    def fingerprint(self) -> str:
        """与旧 `IncidentService._fingerprint` 完全一致。"""
        if self.alert.get("fingerprint"):
            return str(self.alert["fingerprint"])
        labels = self.labels or {}
        name = labels.get("alertname") or self.title or "unknown"
        instance = labels.get("instance") or labels.get("job") or "unknown"
        return f"{name}@{instance}"


@dataclass
class ChatTrigger(TriggerSource):
    """用户在聊天/工单里发起的诊断请求。"""

    kind: ClassVar[str] = "chat"

    user_id: str = ""

    def fingerprint(self) -> str:
        return _stable_fingerprint(self.kind, self.user_id or self.source, self.title)

    def diagnosis_hint(self) -> str:
        who = self.user_id or self.source or "用户"
        return f"该诊断请求来自聊天/工单入口（{who}）"


@dataclass
class ScheduledTrigger(TriggerSource):
    """定时巡检 / 健康检查触发。"""

    kind: ClassVar[str] = "scheduled"

    #: 巡检任务名（用于按任务聚合）
    schedule_name: str = ""

    def fingerprint(self) -> str:
        return _stable_fingerprint(
            self.kind, self.schedule_name or self.title, self.source
        )

    def diagnosis_hint(self) -> str:
        return (
            "该诊断任务来自定时巡检；如无异常请明确回答『无异常』，"
            "不要为了产出报告而制造问题。"
        )


@dataclass
class SlashCommandTrigger(TriggerSource):
    """IM Slash 命令触发（如 /oncall diagnose <target>）。"""

    kind: ClassVar[str] = "slash_command"

    command: str = ""
    args: str = ""

    def fingerprint(self) -> str:
        return _stable_fingerprint(
            self.kind, self.command or self.title, self.args, self.source
        )

    def diagnosis_hint(self) -> str:
        return f"该诊断请求来自 Slash 命令：/{self.command} {self.args}".strip()


# ------------------------- Generic 载荷解析 -------------------------


def parse_generic_payload(payload: dict[str, Any]) -> TriggerSource:
    """把 `POST /api/events/generic` 的载荷解析成对应的 TriggerSource 子类。

    载荷形如 `{title, description, labels, source, kind?, ...kind-specific}`。
    未提供 `kind` 或不认识时回落到 ChatTrigger（最宽松语义）。
    """
    kind = str(payload.get("kind") or "chat").strip().lower()
    common: dict[str, Any] = {
        "title": str(payload.get("title") or "").strip(),
        "description": str(payload.get("description") or "").strip(),
        "labels": {
            str(k): str(v)
            for k, v in (payload.get("labels") or {}).items()
            if k is not None
        },
        "source": str(payload.get("source") or "").strip(),
        "starts_at": str(payload.get("starts_at") or payload.get("startsAt") or "").strip(),
        "raw": dict(payload),
    }
    if not common["title"]:
        raise ValueError("title 为空")
    if kind == "alertmanager":
        return AlertmanagerTrigger(
            **common,
            alert=payload.get("alert") if isinstance(payload.get("alert"), dict) else dict(payload),
        )
    if kind == "scheduled":
        return ScheduledTrigger(
            **common,
            schedule_name=str(payload.get("schedule_name") or common["title"]),
        )
    if kind == "slash_command":
        return SlashCommandTrigger(
            **common,
            command=str(payload.get("command") or ""),
            args=str(payload.get("args") or ""),
        )
    # 默认 chat
    return ChatTrigger(**common, user_id=str(payload.get("user_id") or ""))


# ------------------------- 工具 -------------------------


def _stable_fingerprint(*parts: str) -> str:
    """基于关键片段生成稳定指纹（16 位 hex 前缀）。

    对空/None 输入健壮：全空时返回 kind 前缀 + 恒定 tail，避免所有空 payload
    聚合成同一个 fingerprint 却让 IncidentService 无法辨识 kind。
    """
    cleaned = [str(p).strip() for p in parts if p is not None and str(p).strip()]
    if not cleaned:
        cleaned = ["_empty"]
    kind = cleaned[0]
    payload = "|".join(cleaned)
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()
    return f"{kind}:{digest}"
