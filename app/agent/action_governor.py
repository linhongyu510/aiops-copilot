"""审批式变更闭环（P1.3；P2.1 起提案经 state_store 持久化）。

当前 38 个工具全部只读；本模块为接入写操作工具铺设安全轨道：
- risk_level > 0 的工具调用一律不执行，而是生成 ActionProposal（变更提案），
  由模型转述给用户，等待人工审批；
- 审批按工具元数据的 required_role 校验（低危 operator、高危 admin）；
- 提案经 state_store 持久化（内存 / Redis），TTL 过期自动作废，
  容量由 TTL 约束而非固定条数；
- 不提供「跳过审批」的开关——没有人工批准，变更永远不执行。

挂载点：governance_interceptor（MCP 客户端咽喉）在权限校验之后、
真实调用之前评估提案；审批与执行由 /api/actions/* 驱动。

跨进程边界说明：批准→执行的一次性消费在单进程内是原子的；
多副本同时审批同一提案的互斥需要分布式锁，接入真实写工具时补齐。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from app.agent.tool_registry import tool_registry
from app.config import config
from app.security import ROLE_LEVEL
from app.state_store import MemoryStateStore, state_store_runtime

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"
STATUS_EXECUTED = "executed"
STATUS_FAILED = "failed"

_RISK_LABEL = {0: "只读", 1: "低危变更", 2: "高危变更"}

_KIND = "proposal"


def _default_available_between() -> list[str]:
    """默认执行窗口 = [now, now + TTL]，与提案 TTL 对齐避免歧义（C5）。"""
    now = datetime.now(UTC)
    return [
        now.isoformat(),
        (now + timedelta(seconds=config.action_proposal_ttl_seconds)).isoformat(),
    ]


class ActionProposal(BaseModel):
    """一次未执行的变更提案。"""

    proposal_id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    risk_level: int
    required_role: str
    status: str = STATUS_PENDING
    rationale: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    decided_by: str = ""
    decided_at: str = ""
    executed_at: str = ""
    result_summary: str = ""
    # C5：执行窗口 [start_iso, end_iso]；审批后执行时会二次校验，超窗直接失效。
    # 序列化为 list 以便 JSON 存储；长度非 2 表示未限定（保守放行）。
    available_between: list[str] = Field(default_factory=_default_available_between)
    # P1-3：审批 UI 与审计所需的 Skill 归因；空字符串表示未在 Skill 上下文触发。
    skill_id: str = ""
    skill_step_id: str = ""
    incident_id: str = ""

    def to_prompt_text(self) -> str:
        """返回给模型的说明：操作未执行，等待人工审批。"""
        risk_label = _RISK_LABEL.get(self.risk_level, "变更")
        window_hint = ""
        if len(self.available_between) == 2:
            window_hint = (
                f"\n允许执行的时间窗口: {self.available_between[0]} ~ "
                f"{self.available_between[1]}（超过则失效）"
            )
        skill_hint = ""
        if self.skill_id:
            step_desc = f" 的步骤 {self.skill_step_id}" if self.skill_step_id else ""
            skill_hint = f"\n触发来源: Skill '{self.skill_id}'{step_desc}"
        elif self.skill_step_id:
            skill_hint = f"\n触发来源: 步骤 {self.skill_step_id}"
        if self.incident_id:
            skill_hint += f"\n关联事件: {self.incident_id}"
        return (
            f"[变更提案 {self.proposal_id}] 工具 {self.tool_name} 属于{risk_label}操作，"
            f"已生成提案并等待人工审批，本次调用**没有执行**。\n"
            f"提案参数: {self.args}\n"
            f"风险等级: {self.risk_level}（审批需要 {self.required_role} 角色）"
            f"{skill_hint}"
            f"{window_hint}\n"
            f"请在最终报告中如实呈现该提案内容与预期效果，"
            f"并明确说明「待人工审批」，不要声称已经执行。"
        )

    def is_within_window(self, now: datetime | None = None) -> bool:
        """当前时间是否落在允许执行窗口内；空/非法窗口视为不限制。"""
        if len(self.available_between) != 2:
            return True
        current = now or datetime.now(UTC)
        try:
            start = datetime.fromisoformat(self.available_between[0])
            end = datetime.fromisoformat(self.available_between[1])
        except ValueError:
            return True
        return start <= current <= end


def _expire_stale(proposal: ActionProposal) -> ActionProposal:
    """超过 TTL 或超出执行窗口的 pending 提案标记为 expired（惰性判定，C5）。"""
    if proposal.status != STATUS_PENDING:
        return proposal
    try:
        created = datetime.fromisoformat(proposal.created_at).timestamp()
    except ValueError:
        return proposal
    if time.time() - created > config.action_proposal_ttl_seconds:
        proposal.status = STATUS_EXPIRED
        return proposal
    if not proposal.is_within_window():
        proposal.status = STATUS_EXPIRED
    return proposal


class ActionGovernor:
    """变更提案登记、审批与执行编排（经 state_store 持久化）。"""

    def __init__(self, store: MemoryStateStore | None = None):
        self._explicit_store = store

    @property
    def _store(self):
        # 动态解析：main lifespan 切换 Redis 后，已有单例立即生效
        return self._explicit_store or state_store_runtime.store

    # ---- 提案生成（工具调用咽喉处） ----

    async def evaluate(
        self, tool_name: str, args: dict[str, Any] | None
    ) -> ActionProposal | None:
        """risk_level == 0 直接放行（返回 None）；否则生成待审批提案。

        P1-3：从 asyncio contextvar 读取 Skill / Step / Incident 归因，
        写入 proposal，供审批 UI 展示「因为 Skill X 的步骤 Y 触发该变更」。
        contextvar 未设置时字段留空，保持向后兼容。
        """
        spec = tool_registry.spec_for(tool_name)
        if spec.risk_level <= 0:
            return None
        # 延迟 import，避免 skills 包与 governor 之间的循环引用
        from app.agent.skills.context import get_current_skill_context

        ctx = get_current_skill_context()
        proposal = ActionProposal(
            proposal_id=f"act-{uuid.uuid4().hex[:10]}",
            tool_name=tool_name,
            args=dict(args or {}),
            risk_level=spec.risk_level,
            required_role=spec.required_role,
            skill_id=ctx.skill_id,
            skill_step_id=ctx.skill_step_id,
            incident_id=ctx.incident_id,
        )
        await self._store.put(
            _KIND,
            proposal.proposal_id,
            proposal.model_dump(),
            score=time.time(),
            # 提案存活 = TTL + 决策结果的保留窗口
            ttl_seconds=config.action_proposal_ttl_seconds + 3600.0,
        )
        origin = ""
        if ctx.skill_id or ctx.incident_id:
            origin = (
                f"（skill={ctx.skill_id or '-'} step={ctx.skill_step_id or '-'} "
                f"incident={ctx.incident_id or '-'}）"
            )
        logger.warning(
            f"拦截变更动作 {tool_name}（风险 {spec.risk_level}），"
            f"生成提案 {proposal.proposal_id} 等待审批{origin}"
        )
        return proposal

    # ---- 审批 ----

    async def get(self, proposal_id: str) -> ActionProposal | None:
        data = await self._store.get(_KIND, proposal_id)
        if data is None:
            return None
        return _expire_stale(ActionProposal.model_validate(data))

    async def list_proposals(self, status: str | None = None) -> list[ActionProposal]:
        items = [
            _expire_stale(ActionProposal.model_validate(data))
            for data in await self._store.list(_KIND, limit=200)
        ]
        if status:
            items = [item for item in items if item.status == status]
        return items

    async def _save(self, proposal: ActionProposal) -> None:
        await self._store.put(
            _KIND,
            proposal.proposal_id,
            proposal.model_dump(),
            score=time.time(),
            ttl_seconds=3600.0,
        )

    async def decide(
        self, proposal_id: str, approve: bool, actor: str, actor_role: str
    ) -> tuple[bool, str, ActionProposal | None]:
        """人工审批。审批角色必须满足提案的 required_role。"""
        proposal = await self.get(proposal_id)
        if proposal is None:
            return False, "proposal_not_found", None
        if proposal.status != STATUS_PENDING:
            return False, f"proposal_not_pending:{proposal.status}", proposal
        if ROLE_LEVEL.get(actor_role, 0) < ROLE_LEVEL.get(proposal.required_role, 10_000):
            return (
                False,
                f"insufficient_role:{actor_role}<{proposal.required_role}",
                proposal,
            )
        proposal.status = STATUS_APPROVED if approve else STATUS_REJECTED
        proposal.decided_by = actor[:64]
        proposal.decided_at = datetime.now(UTC).isoformat()
        await self._save(proposal)
        logger.info(
            f"提案 {proposal_id} 被 {actor} "
            f"{'批准' if approve else '拒绝'}（{proposal.status}）"
        )
        return True, proposal.status, proposal

    # ---- 执行（批准后由工具属主驱动） ----

    async def execute_approved(
        self,
        proposal_id: str,
        action: Callable[[], Awaitable[str]],
    ) -> tuple[bool, str]:
        """执行已批准的提案；action 为真实工具调用，返回 (ok, 结果摘要)。

        只读不变量：仅 status == approved 的提案可执行，且一次性消费。
        C5：执行前二次校验时间窗口；超窗提案立即标记为 expired，禁止执行。
        """
        proposal = await self.get(proposal_id)
        if proposal is None:
            return False, "proposal_not_found"
        if proposal.status == STATUS_EXPIRED:
            return False, "proposal_window_expired"
        if proposal.status != STATUS_APPROVED:
            return False, f"proposal_not_approved:{proposal.status}"
        if not proposal.is_within_window():
            proposal.status = STATUS_EXPIRED
            await self._save(proposal)
            logger.warning(
                f"提案 {proposal_id} 超过执行窗口 {proposal.available_between}，"
                "拒绝执行并标记为 expired"
            )
            return False, "proposal_window_expired"
        try:
            summary = await action()
        except Exception as exc:
            proposal.status = STATUS_FAILED
            proposal.result_summary = str(exc)
            await self._save(proposal)
            return False, f"execution_failed:{exc}"
        proposal.status = STATUS_EXECUTED
        proposal.executed_at = datetime.now(UTC).isoformat()
        proposal.result_summary = str(summary)[:500]
        await self._save(proposal)
        return True, proposal.result_summary


# 全局单例（存储后端由 main lifespan 按 coordination 配置注入）
action_governor = ActionGovernor()
