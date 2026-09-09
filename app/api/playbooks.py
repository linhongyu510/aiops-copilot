"""预案库只读 API（P2.4）。

- GET /api/playbooks：列出全部预案（含来源与评审状态）；
- GET /api/playbooks/match?q=...：调试匹配，返回 top-k 与得分。

预案的增改走文件（AIOPS_PLAYBOOKS_PATH 的 JSONL，走代码评审），
不提供写接口——程序性记忆的变更应当和代码一样可审计。
"""

from fastapi import APIRouter, Query

from app.services.playbook_service import get_playbook_service

router = APIRouter()


@router.get("/playbooks")
async def list_playbooks():
    """列出预案库全部预案"""
    service = get_playbook_service()
    playbooks = service.list_playbooks()  # 先触发惰性加载，再读元信息
    return {"source": service.source, "count": len(playbooks), "playbooks": playbooks}


@router.get("/playbooks/match")
async def match_playbooks(q: str = Query(..., min_length=1, description="任务/症状描述")):
    """按相似度匹配预案（调试与评审用）"""
    matched = get_playbook_service().match(q)
    return {
        "query": q,
        "matched": [
            {
                "playbook_id": item.get("playbook_id"),
                "title": item.get("title"),
                "score": item.get("score"),
                "root_cause_hint": item.get("root_cause_hint"),
            }
            for item in matched
        ],
    }
