"""事件接入与自治诊断（P1.1，P2.1 起状态存储后端化）：告警 → incident 状态机 → 自动触发诊断。

让系统从「被动问答」变成「事件驱动自治」：
- POST /api/events/alert 接收 Prometheus Alertmanager webhook 格式告警；
- POST /api/events/generic 接收 chat/scheduled/slash_command 等通用触发事件
  （P0-4 通过 `TriggerSource` 抽象统一入口）；
- 指纹去重 + 聚合窗口：同根因事件在聚合窗口内归并为一个 incident，只计次数；
- 自治诊断：新 incident 后台触发 Plan-Execute-Replan（独立并发预算，
  不与用户发起的诊断抢占 admission control），产出报告后进入
  diagnosed 状态等待人工决策；
- 通知：`Notifier` 插件化（P1-1），按 `AIOPS_INCIDENT_NOTIFIERS` 组装
  webhook / slack / feishu 等渠道；未配置则明确跳过（不伪造通知）。
  单渠道失败不影响其他渠道；只配置了 `AIOPS_INCIDENT_NOTIFY_WEBHOOK_URL`
  时保持 legacy 单 webhook 行为；
- 状态机：firing → diagnosing → diagnosed → resolved / closed；
  诊断失败回退 firing 并记录错误。

P2.1：实体持久化走 state_store（默认内存，coordination=redis 时 Redis 化，
多副本共享 incident 视图；指纹→活跃 incident 用二级索引，避免全表扫描）。

当前全部工具只读，自治诊断只做「排查与建议」，不做任何变更；
执行变更需等 ActionGovernor（P1.3）上线。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from app.config import config
from app.services.notify import Notifier, build_notifiers
from app.services.triggers import AlertmanagerTrigger, TriggerSource
from app.state_store import MemoryStateStore, state_store_runtime

# incident 状态机
STATUS_FIRING = "firing"
STATUS_DIAGNOSING = "diagnosing"
STATUS_DIAGNOSED = "diagnosed"
STATUS_RESOLVED = "resolved"
STATUS_CLOSED = "closed"

_ACTIVE_STATUSES = {STATUS_FIRING, STATUS_DIAGNOSING, STATUS_DIAGNOSED}

# 允许的人工状态迁移：diagnosed → resolved/closed；active 可直接关闭
_MANUAL_TRANSITIONS = {
    STATUS_DIAGNOSED: {STATUS_RESOLVED, STATUS_CLOSED},
    STATUS_FIRING: {STATUS_RESOLVED, STATUS_CLOSED},
    STATUS_DIAGNOSING: {STATUS_CLOSED},
    STATUS_RESOLVED: {STATUS_CLOSED},
}

_KIND = "incident"
_FP_INDEX = "incident_fp"

# 终态 incident 在存储中的保留时长（秒）；活跃 incident 不设 TTL
_TERMINAL_TTL_SECONDS = 7 * 24 * 3600


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _epoch(iso: str, fallback: float) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return fallback


def _extract_severity(alert: dict[str, Any] | None) -> str:
    """从 alert payload 中抽取 severity（labels 优先，其次顶层字段）。"""
    if not isinstance(alert, dict):
        return ""
    labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
    severity = labels.get("severity") if isinstance(labels, dict) else None
    if not severity:
        severity = alert.get("severity")
    return str(severity or "").strip().lower()


def _dedup_window_for_alert(alert: dict[str, Any] | None) -> float:
    """按 severity 选择去重窗口（C6）：profile 未配置时回落全局默认。"""
    fallback = float(config.incident_dedup_window_seconds)
    severity = _extract_severity(alert)
    if not severity:
        return fallback
    try:
        from app.agent.profiles import get_active_profile

        return float(get_active_profile().dedup_window_for_severity(severity, fallback))
    except Exception:  # noqa: BLE001
        return fallback


class Incident:
    """一个告警聚合而成的事件（同指纹在聚合窗口内归并）。"""

    def __init__(
        self,
        fingerprint: str,
        title: str,
        alert: dict[str, Any],
        trigger_kind: str = AlertmanagerTrigger.kind,
    ):
        self.incident_id = f"inc-{uuid.uuid4().hex[:12]}"
        self.fingerprint = fingerprint
        self.title = title
        self.status = STATUS_FIRING
        self.alert_count = 1
        self.first_alert_at = _now_iso()
        self.last_alert_at = self.first_alert_at
        self.latest_alert = alert
        self.trigger_kind = trigger_kind
        self.diagnosis_report: str = ""
        self.diagnosis_error: str = ""
        self.diagnosed_at: str = ""
        self.session_id: str = ""
        self.timeline: list[dict[str, str]] = [
            {"at": self.first_alert_at, "event": "created", "detail": title}
        ]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Incident:
        incident = cls.__new__(cls)
        incident.incident_id = str(data.get("incident_id", ""))
        incident.fingerprint = str(data.get("fingerprint", ""))
        incident.title = str(data.get("title", ""))
        incident.status = str(data.get("status", STATUS_FIRING))
        incident.alert_count = int(data.get("alert_count", 1))
        incident.first_alert_at = str(data.get("first_alert_at", ""))
        incident.last_alert_at = str(data.get("last_alert_at", ""))
        incident.latest_alert = dict(data.get("latest_alert", {}) or {})
        incident.trigger_kind = str(data.get("trigger_kind", AlertmanagerTrigger.kind))
        incident.diagnosis_report = str(data.get("diagnosis_report", ""))
        incident.diagnosis_error = str(data.get("diagnosis_error", ""))
        incident.diagnosed_at = str(data.get("diagnosed_at", ""))
        incident.session_id = str(data.get("session_id", ""))
        incident.timeline = list(data.get("timeline", []) or [])
        return incident

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "fingerprint": self.fingerprint,
            "title": self.title,
            "status": self.status,
            "alert_count": self.alert_count,
            "first_alert_at": self.first_alert_at,
            "last_alert_at": self.last_alert_at,
            "latest_alert": self.latest_alert,
            "trigger_kind": self.trigger_kind,
            "diagnosis_report": self.diagnosis_report,
            "diagnosis_error": self.diagnosis_error,
            "diagnosed_at": self.diagnosed_at,
            "session_id": self.session_id,
            "timeline": self.timeline,
        }

    def record(self, event: str, detail: str = "") -> None:
        self.timeline.append({"at": _now_iso(), "event": event, "detail": detail})

    @property
    def is_active(self) -> bool:
        return self.status in _ACTIVE_STATUSES


def build_diagnosis_prompt(
    alert: dict[str, Any], trigger: TriggerSource | None = None
) -> str:
    """把触发事件转换为自治诊断任务描述（只读排查）。

    - `trigger` 提供时按 trigger.kind 走差异化措辞（alertmanager/chat/
      scheduled/slash_command）；不提供时按 Alertmanager 语义构造，
      保持与 P0-3 之前的输出完全一致。
    - 未来接入 profile.diagnosis 模板时，可在此叠加 profile 语气；当前
      为避免过早抽象，只做入口 kind 差异化，不引入模板引擎。
    """
    labels = alert.get("labels", {}) or {}
    annotations = alert.get("annotations", {}) or {}
    name = labels.get("alertname") or alert.get("title") or "未命名事件"
    severity = labels.get("severity") or "unknown"
    instance = labels.get("instance") or labels.get("job") or "unknown"
    description = (
        annotations.get("description")
        or annotations.get("summary")
        or alert.get("description")
        or ""
    )
    starts_at = alert.get("startsAt") or ""

    kind = trigger.kind if trigger else AlertmanagerTrigger.kind
    if kind == "chat":
        header = "【聊天/工单诊断请求】"
        name_label = "问题标题"
        severity_label = "紧急度"
        target_label = "关联对象"
    elif kind == "scheduled":
        header = "【定时巡检诊断任务】"
        name_label = "巡检项"
        severity_label = "级别"
        target_label = "巡检目标"
    elif kind == "slash_command":
        header = "【Slash 命令诊断请求】"
        name_label = "命令目标"
        severity_label = "紧急度"
        target_label = "关联对象"
    else:  # alertmanager 与未知 kind 保持 legacy 措辞
        header = "【自动告警诊断任务】"
        name_label = "告警名称"
        severity_label = "告警级别"
        target_label = "告警对象"

    lines = [
        header,
        f"{name_label}: {name}",
        f"{severity_label}: {severity}",
        f"{target_label}: {instance}",
    ]
    if description:
        lines.append("详细描述: " + description)
    if starts_at:
        lines.append(f"触发时间: {starts_at}")
    hint = trigger.diagnosis_hint() if trigger else ""
    if hint:
        lines.append(hint)
    lines.append(
        "请对该事件进行根因诊断：查询相关监控指标与日志，定位受影响的服务与组件，"
        "给出根因结论与处理建议。本次为只读排查，不得执行任何变更操作。"
    )
    return "\n".join(lines)


class IncidentService:
    """事件状态机 + 自治诊断编排（状态经 state_store 持久化）。"""

    def __init__(
        self,
        store: MemoryStateStore | None = None,
        notifiers: list[Notifier] | None = None,
    ):
        self._explicit_store = store
        self._diagnosis_semaphore = asyncio.Semaphore(
            max(1, config.incident_max_concurrent_diagnoses)
        )
        self._tasks: set[asyncio.Task] = set()
        # 本进程内正在内存中更新的 incident 缓存（写穿，读优先），跨进程以存储为准
        self._local: dict[str, Incident] = {}
        # 通知渠道列表：显式注入 > 配置构建；测试可注入 fake，生产走
        # build_notifiers() 按 `incident_notifiers` 组装。
        self._explicit_notifiers = notifiers

    @property
    def _store(self):
        # 动态解析：main lifespan 切换 Redis 后，已有单例立即生效
        return self._explicit_store or state_store_runtime.store

    @property
    def _notifiers(self) -> list[Notifier]:
        if self._explicit_notifiers is not None:
            return self._explicit_notifiers
        # 每次读实时构建：允许配置热更新在下一次通知生效，也避免全局单例
        # 在测试之间残留 monkeypatch 后的旧 notifier。
        return build_notifiers()

    # ---- 持久化辅助 ----

    async def _save(self, incident: Incident) -> None:
        self._local[incident.incident_id] = incident
        await self._store.put(
            _KIND,
            incident.incident_id,
            incident.to_dict(),
            score=_epoch(incident.first_alert_at, time.time()),
            ttl_seconds=None if incident.is_active else _TERMINAL_TTL_SECONDS,
        )
        if incident.is_active:
            await self._store.set_index(_FP_INDEX, incident.fingerprint, incident.incident_id)
        else:
            # 离开活跃态后清除指纹索引，后续同指纹告警开新 incident
            await self._store.set_index(_FP_INDEX, incident.fingerprint, None)

    async def _load(self, incident_id: str) -> Incident | None:
        local = self._local.get(incident_id)
        if local is not None:
            return local
        data = await self._store.get(_KIND, incident_id)
        if data is None:
            return None
        incident = Incident.from_dict(data)
        self._local[incident_id] = incident
        return incident

    # ---- 告警接入 ----

    @staticmethod
    def _fingerprint(alert: dict[str, Any]) -> str:
        labels = alert.get("labels", {}) or {}
        # Alertmanager 指纹优先；否则用 alertname+instance 聚合同一对象的同类告警
        if alert.get("fingerprint"):
            return str(alert["fingerprint"])
        name = labels.get("alertname") or alert.get("title") or "unknown"
        instance = labels.get("instance") or labels.get("job") or "unknown"
        return f"{name}@{instance}"

    async def _find_active_by_fingerprint(
        self, fingerprint: str, alert: dict[str, Any] | None = None
    ) -> Incident | None:
        incident_id = await self._store.get_index(_FP_INDEX, fingerprint)
        if not incident_id:
            return None
        incident = await self._load(incident_id)
        if incident is None or not incident.is_active:
            return None
        window = _dedup_window_for_alert(alert or incident.latest_alert)
        if time.time() - _epoch(incident.last_alert_at, 0.0) > window:
            return None
        return incident

    async def ingest_alert(self, alert: dict[str, Any]) -> dict[str, Any]:
        """接入单条 Alertmanager 告警：兼容入口，内部委托到 ingest_trigger。"""
        return await self.ingest_trigger(AlertmanagerTrigger.from_alert(alert))

    async def ingest_trigger(self, trigger: TriggerSource) -> dict[str, Any]:
        """接入任意来源的触发事件：归并或创建 incident，必要时触发自治诊断。"""
        alert_payload = trigger.alert_dict()
        fingerprint = trigger.fingerprint()
        existing = await self._find_active_by_fingerprint(fingerprint, alert_payload)
        if existing is not None:
            existing.alert_count += 1
            existing.last_alert_at = _now_iso()
            existing.latest_alert = alert_payload
            existing.record("alert_aggregated", f"累计告警 {existing.alert_count} 次")
            await self._save(existing)
            logger.info(
                f"[{trigger.kind}] 归并到现有 incident {existing.incident_id}"
                f"（{existing.alert_count} 次）"
            )
            return {"incident_id": existing.incident_id, "aggregated": True}

        title = trigger.title or f"事件 {fingerprint}"
        incident = Incident(fingerprint, title, alert_payload, trigger_kind=trigger.kind)
        await self._save(incident)
        logger.info(f"[{trigger.kind}] 新 incident {incident.incident_id}: {title}")

        if config.incident_autonomous_diagnosis_enabled:
            self._spawn_diagnosis(incident, trigger=trigger)
        else:
            incident.record("autonomous_diagnosis_disabled")
            await self._save(incident)
        return {"incident_id": incident.incident_id, "aggregated": False}

    # ---- 自治诊断 ----

    def _spawn_diagnosis(
        self, incident: Incident, trigger: TriggerSource | None = None
    ) -> None:
        """后台启动诊断任务；并发预算独立于 HTTP admission control。"""
        incident.status = STATUS_DIAGNOSING
        incident.session_id = f"incident-{incident.incident_id}"
        incident.record("diagnosis_started")

        async def _run() -> None:
            from app.agent.skills.context import use_skill_context
            from app.services.aiops_service import aiops_service

            prompt = build_diagnosis_prompt(incident.latest_alert, trigger=trigger)
            async with self._diagnosis_semaphore:
                try:
                    report = ""
                    # P1-3：为整个诊断任务建立 incident_id 归因，
                    # ActionGovernor 生成的提案会自动关联到该 incident。
                    with use_skill_context(
                        incident_id=incident.incident_id, merge=False
                    ):
                        async for event in aiops_service.execute(
                            prompt, session_id=incident.session_id
                        ):
                            if event.get("type") == "complete":
                                report = str(event.get("response", ""))
                            elif event.get("type") == "error":
                                raise RuntimeError(str(event.get("message", "诊断失败")))
                    if not report:
                        raise RuntimeError("诊断完成但未产出报告")
                    incident.diagnosis_report = report
                    incident.diagnosed_at = _now_iso()
                    incident.status = STATUS_DIAGNOSED
                    incident.record("diagnosis_completed", f"报告长度 {len(report)}")
                    logger.info(f"incident {incident.incident_id} 自治诊断完成")
                except Exception as exc:
                    incident.status = STATUS_FIRING
                    incident.diagnosis_error = str(exc)
                    incident.record("diagnosis_failed", str(exc))
                    logger.warning(
                        f"incident {incident.incident_id} 自治诊断失败: {exc}"
                    )
                finally:
                    await self._save(incident)
                    await self._notify(incident)

        async def _runner() -> None:
            await self._save(incident)
            await _run()

        task = asyncio.create_task(
            _runner(), name=f"incident-diagnosis-{incident.incident_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _notify(self, incident: Incident) -> None:
        """遍历配置的 notifier 列表，每个渠道失败不影响其他；未配置则跳过。"""
        notifiers = self._notifiers
        if not notifiers:
            logger.debug("未配置任何 incident notifier，跳过通知")
            return
        payload = incident.to_dict()
        # 并发触发但独立错误：每个 notifier 已在 base 里自吞异常并返回 bool
        results = await asyncio.gather(
            *(n.notify(payload) for n in notifiers), return_exceptions=False
        )
        succeeded = sum(1 for ok in results if ok)
        logger.info(
            f"incident {incident.incident_id} 通知：{succeeded}/{len(notifiers)} 成功"
        )

    # ---- 查询与人工流转 ----

    async def list_incidents(self, status: str | None = None) -> list[dict[str, Any]]:
        items = await self._store.list(_KIND, limit=config.incident_store_max)
        if status:
            items = [item for item in items if item.get("status") == status]
        return items

    async def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        incident = await self._load(incident_id)
        return incident.to_dict() if incident else None

    async def transition(
        self, incident_id: str, target: str, actor: str = "manual"
    ) -> dict[str, Any]:
        """人工状态迁移（resolve/close），非法迁移被拒绝。"""
        incident = await self._load(incident_id)
        if incident is None:
            return {"ok": False, "error": "incident_not_found"}
        allowed = _MANUAL_TRANSITIONS.get(incident.status, set())
        if target not in allowed:
            return {
                "ok": False,
                "error": f"illegal_transition:{incident.status}->{target}",
            }
        incident.status = target
        incident.record(f"transitioned:{target}", f"by {actor}")
        await self._save(incident)
        return {"ok": True, "incident": incident.to_dict()}

    async def shutdown(self) -> None:
        """应用退出时取消仍在运行的诊断任务。"""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


# 全局单例（存储后端由 main lifespan 按 coordination 配置注入）
incident_service = IncidentService()
