"""事件接入与 incident 管理 API（P1.1 + P0-4）。

- POST /api/events/alert：接收 Prometheus Alertmanager webhook 格式告警，
  同时兼容单告警简化格式；去重聚合后按需触发自治诊断；
- POST /api/events/generic：接收通用触发载荷（chat/scheduled/slash_command），
  通过 `TriggerSource` 抽象走同一条 incident 状态机；
- GET  /api/incidents：列出 incident（可按 status 过滤）；
- GET  /api/incidents/{id}：incident 详情（含诊断报告与时间线）；
- POST /api/incidents/{id}/resolve | /close：人工状态迁移。

角色沿用全局中间件：POST 需 operator，GET 需 viewer。
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field

from app.services.incident_service import incident_service
from app.services.triggers import parse_generic_payload

router = APIRouter()


class AlertPayload(BaseModel):
    """Alertmanager webhook 载荷（alerts 数组）或单条告警的简化格式。"""

    alerts: list[dict[str, Any]] = Field(default_factory=list)
    # 简化单告警格式（兼容非 Alertmanager 来源）
    title: str = ""
    description: str = ""
    severity: str = ""
    labels: dict[str, str] = Field(default_factory=dict)


class GenericEventPayload(BaseModel):
    """通用触发事件载荷（P0-4）。

    - `kind` 可选，缺省视作 `chat`；支持 `alertmanager` / `chat` / `scheduled` /
      `slash_command`；未知 kind 回落到 chat 语义（宽松）。
    - `title` 必填，其他字段按 kind 语义可选。
    - kind-specific 字段（`user_id` / `schedule_name` / `command` / `args` /
      `alert`）通过 `extra=allow` 透传给 TriggerSource。
    """

    kind: str = "chat"
    title: str
    description: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    source: str = ""
    starts_at: str = ""
    user_id: str = ""
    schedule_name: str = ""
    command: str = ""
    args: str = ""
    alert: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class TransitionRequest(BaseModel):
    actor: str = "manual"


def _normalize_alerts(payload: AlertPayload) -> list[dict[str, Any]]:
    if payload.alerts:
        return [alert for alert in payload.alerts if isinstance(alert, dict)]
    simplified = {
        "labels": dict(payload.labels or {}),
        "annotations": {},
    }
    if payload.title:
        simplified["labels"]["alertname"] = payload.title
    if payload.severity:
        simplified["labels"]["severity"] = payload.severity
    if payload.description:
        simplified["annotations"]["description"] = payload.description
    return [simplified] if simplified["labels"] else []


@router.post("/events/alert")
async def ingest_alert(payload: AlertPayload, request: Request):
    """接收告警并接入 incident 状态机（去重聚合 + 可选自治诊断）"""
    alerts = _normalize_alerts(payload)
    if not alerts:
        raise HTTPException(status_code=422, detail="no_valid_alerts")

    results = []
    for alert in alerts[:50]:  # 单次 webhook 最多处理 50 条，防滥用
        results.append(await incident_service.ingest_alert(alert))

    accepted = sum(1 for item in results if not item["aggregated"])
    aggregated = len(results) - accepted
    logger.info(
        f"[request {getattr(request.state, 'request_id', '-')}] "
        f"告警接入完成：新建 {accepted}，归并 {aggregated}"
    )
    return {"accepted": accepted, "aggregated": aggregated, "incidents": results}


@router.post("/events/generic")
async def ingest_generic_event(payload: GenericEventPayload, request: Request):
    """接收通用触发事件（chat/scheduled/slash_command/alertmanager）。

    比 `/events/alert` 更宽松：任何带 `title` 的载荷都可以进入 incident 状态机，
    走完 Plan-Execute-Replan。用于让工单、聊天求助、定时体检等非告警场景复用
    同一条诊断链路。
    """
    try:
        trigger = parse_generic_payload(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = await incident_service.ingest_trigger(trigger)
    logger.info(
        f"[request {getattr(request.state, 'request_id', '-')}] "
        f"通用事件接入：kind={trigger.kind} title={trigger.title!r} "
        f"aggregated={result['aggregated']}"
    )
    return {
        "kind": trigger.kind,
        "accepted": 0 if result["aggregated"] else 1,
        "aggregated": 1 if result["aggregated"] else 0,
        "incident": result,
    }


@router.get("/incidents")
async def list_incidents(status: str | None = None):
    """列出 incident，按创建时间倒序；可用 ?status= 过滤"""
    return {"incidents": await incident_service.list_incidents(status=status)}


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    incident = await incident_service.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident_not_found")
    return incident


@router.post("/incidents/{incident_id}/resolve")
async def resolve_incident(incident_id: str, payload: TransitionRequest | None = None):
    """人工标记 incident 已解决（diagnosed/firing → resolved）"""
    actor = (payload.actor if payload else "manual")[:64]
    result = await incident_service.transition(incident_id, "resolved", actor=actor)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["error"])
    return result["incident"]


@router.post("/incidents/{incident_id}/close")
async def close_incident(incident_id: str, payload: TransitionRequest | None = None):
    """人工关闭 incident（任意非 closed 状态 → closed）"""
    actor = (payload.actor if payload else "manual")[:64]
    result = await incident_service.transition(incident_id, "closed", actor=actor)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["error"])
    return result["incident"]
