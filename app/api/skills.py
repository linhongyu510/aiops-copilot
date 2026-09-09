"""Skill Registry 只读 API（P0-1）。

- GET  /api/skills            列出全部 Skill（含 disabled_reason）
- GET  /api/skills/{id}       Skill 详情（原始 Pydantic dump）
- GET  /api/skills/match?q=   调试匹配（融合 alertname/labels/symptoms）
- GET  /api/skills/metrics    activated/succeeded/abandoned 计数
- POST /api/skills/reload     显式重载 YAML/JSONL 目录（需 operator）
"""

from fastapi import APIRouter, HTTPException, Query

from app.agent.skills import get_skill_registry

router = APIRouter()


@router.get("/skills")
async def list_skills():
    registry = get_skill_registry()
    packs = registry.list_skills()
    return {
        "count": len(packs),
        "sources": registry.source_summary,
        "skills": registry.catalog_snapshot(),
    }


@router.get("/skills/match")
async def match_skills(
    q: str = Query(..., min_length=1, description="任务描述/告警标题"),
    alertname: str = Query("", description="Alertmanager alertname，可空"),
):
    labels = {"alertname": alertname} if alertname else None
    matches = get_skill_registry().match(q, alert_labels=labels)
    return {
        "query": q,
        "alertname": alertname,
        "matched": [
            {
                "skill_id": item.skill.skill_id,
                "title": item.skill.title,
                "score": item.score,
                "reasons": item.reasons,
                "required_tools": list(item.skill.required_tools),
            }
            for item in matches
        ],
    }


@router.get("/skills/metrics")
async def skill_metrics():
    return {"skills": get_skill_registry().metrics_snapshot()}


@router.get("/skills/{skill_id}")
async def get_skill(skill_id: str):
    pack = get_skill_registry().get(skill_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="skill_not_found")
    return pack.model_dump()


@router.post("/skills/reload")
async def reload_skills():
    registry = get_skill_registry()
    registry.reload()
    return {
        "reloaded": True,
        "count": len(registry.list_skills()),
        "sources": registry.source_summary,
    }
