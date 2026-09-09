"""变更提案审批 API（P1.3；提案经 state_store 持久化，P2.1）。

- GET  /api/actions/proposals：列出提案（默认全部，可 ?status=pending 过滤）；
- POST /api/actions/proposals/{id}/approve：批准（角色需满足提案 required_role）；
- POST /api/actions/proposals/{id}/reject：拒绝。

审批只改变提案状态；实际执行由工具属主在批准后通过
action_governor.execute_approved 驱动（当前仓库无写操作工具，
提案轨道先于工具落地——接入第一个写工具时即受此闭环约束）。
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.agent.action_governor import action_governor
from app.agent.tool_safety import get_current_role

router = APIRouter()


class DecisionRequest(BaseModel):
    actor: str = "manual"


@router.get("/actions/proposals")
async def list_proposals(status: str | None = None):
    """列出变更提案，按创建时间倒序"""
    proposals = await action_governor.list_proposals(status)
    return {"proposals": [proposal.model_dump() for proposal in proposals]}


async def _decide(
    proposal_id: str, approve: bool, payload: DecisionRequest | None
) -> dict:
    # 审批角色：HTTP 身份（中间件注入的角色上下文），无身份回退 operator 下限
    actor_role = get_current_role()
    ok, reason, _proposal = await action_governor.decide(
        proposal_id,
        approve=approve,
        actor=(payload.actor if payload else "manual"),
        actor_role=actor_role,
    )
    if not ok:
        status_code = 404 if reason == "proposal_not_found" else 409
        raise HTTPException(status_code=status_code, detail=reason)
    return {"proposal_id": proposal_id, "status": reason}


@router.post("/actions/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: str, payload: DecisionRequest | None = None, request: Request = None  # noqa: ARG001
):
    return await _decide(proposal_id, True, payload)


@router.post("/actions/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: str, payload: DecisionRequest | None = None, request: Request = None  # noqa: ARG001
):
    return await _decide(proposal_id, False, payload)
