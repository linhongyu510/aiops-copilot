"""P1.3 + P2.1：审批式变更闭环 ActionGovernor（state_store 后端）"""

from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from mcp.types import CallToolResult, TextContent

from app.agent.action_governor import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ActionGovernor,
    action_governor,
)
from app.agent.tool_registry import ToolSpec, tool_registry
from app.agent.tool_safety import governance_interceptor, role_context
from app.config import config
from app.main import app
from app.state_store import MemoryStateStore

client = TestClient(app)


@pytest.fixture
def risky_tool(monkeypatch):
    """注册一个测试用低危写工具（operator 可批）"""
    monkeypatch.setattr(tool_registry, "_catalog", dict(tool_registry._catalog))
    tool_registry.register(
        ToolSpec(
            name="k8s_rollout_restart",
            category="kubernetes",
            read_only=False,
            risk_level=1,
            required_role="operator",
            description="重启工作负载（低危变更）",
        )
    )
    yield "k8s_rollout_restart"
    tool_registry._catalog.pop("k8s_rollout_restart", None)
    tool_registry._semantic_cache_key = None
    tool_registry._semantic_cache_index = None


@pytest.fixture
def governor():
    return ActionGovernor(store=MemoryStateStore())


async def test_readonly_tools_pass_through(governor, risky_tool):
    assert await governor.evaluate("prom_query", {"query": "up"}) is None
    assert await governor.evaluate("retrieve_knowledge", {"query": "x"}) is None


async def test_risky_tool_produces_pending_proposal(governor, risky_tool):
    proposal = await governor.evaluate(risky_tool, {"namespace": "default", "deployment": "api"})
    assert proposal is not None
    assert proposal.status == STATUS_PENDING
    assert proposal.risk_level == 1
    assert proposal.required_role == "operator"
    text = proposal.to_prompt_text()
    assert "没有执行" in text
    assert "待人工审批" in text
    assert "k8s_rollout_restart" in text


async def test_proposals_persist_across_governor_instances(governor, risky_tool):
    """P2.1：提案持久化在存储中，另一实例（其他副本）可审批与执行"""
    proposal = await governor.evaluate(risky_tool, {"deployment": "api"})
    other = ActionGovernor(store=governor._store)

    ok, status, _ = await other.decide(proposal.proposal_id, True, "op-1", "operator")
    assert ok is True
    assert status == STATUS_APPROVED

    calls: list[str] = []

    async def _action():
        calls.append("executed")
        return "重启完成"

    ok2, summary = await other.execute_approved(proposal.proposal_id, _action)
    assert ok2 is True
    assert "重启完成" in summary
    assert calls == ["executed"]


async def test_approval_requires_sufficient_role(governor, risky_tool):
    proposal = await governor.evaluate(risky_tool, {})
    ok, reason, _ = await governor.decide(proposal.proposal_id, True, "viewer-1", "viewer")
    assert ok is False
    assert "insufficient_role" in reason
    ok, status, _ = await governor.decide(proposal.proposal_id, True, "op-1", "operator")
    assert ok is True
    assert status == STATUS_APPROVED


async def test_rejected_proposal_cannot_execute(governor, risky_tool):
    proposal = await governor.evaluate(risky_tool, {})

    async def _action():
        raise AssertionError("被拒绝的提案绝不能执行")

    await governor.decide(proposal.proposal_id, False, "op-1", "operator")
    ok, reason = await governor.execute_approved(proposal.proposal_id, _action)
    assert ok is False
    assert "not_approved" in reason or "not_pending" in reason


async def test_approved_proposal_executes_once(governor, risky_tool):
    proposal = await governor.evaluate(risky_tool, {"deployment": "api"})
    await governor.decide(proposal.proposal_id, True, "op-1", "operator")

    calls: list[str] = []

    async def _action():
        calls.append("executed")
        return "重启完成，副本已就绪"

    ok, summary = await governor.execute_approved(proposal.proposal_id, _action)
    assert ok is True
    assert "重启完成" in summary
    assert calls == ["executed"]

    # 一次性消费：再执行被拒绝
    ok2, _reason2 = await governor.execute_approved(proposal.proposal_id, _action)
    assert ok2 is False
    assert calls == ["executed"]


async def test_expired_proposal_auto_invalidated(monkeypatch, governor, risky_tool):
    monkeypatch.setattr(config, "action_proposal_ttl_seconds", 0.0)
    proposal = await governor.evaluate(risky_tool, {})
    await governor.decide(proposal.proposal_id, True, "op-1", "operator")
    # TTL=0 → 下一次 get 即过期；过期提案不可执行
    ok, reason = await governor.execute_approved(
        proposal.proposal_id, _never := (lambda: "x")
    )
    assert ok is False


# ------------------- P1-2 C5：提案携带执行时间窗口 -------------------


async def test_c5_proposal_has_default_execution_window(governor, risky_tool):
    """C5：新提案默认写入 [now, now+TTL] 的执行窗口。"""
    proposal = await governor.evaluate(risky_tool, {})
    assert len(proposal.available_between) == 2
    assert proposal.is_within_window() is True


async def test_c5_proposal_to_prompt_text_shows_window(governor, risky_tool):
    """C5：to_prompt_text 应向 LLM 展示允许执行的时间窗口。"""
    proposal = await governor.evaluate(risky_tool, {})
    text = proposal.to_prompt_text()
    assert "允许执行的时间窗口" in text
    assert proposal.available_between[0] in text


async def test_c5_execute_rejects_out_of_window_proposal(governor, risky_tool):
    """C5：审批后的提案若已过执行窗口，禁止执行并标记 expired。"""
    from datetime import datetime, timedelta

    proposal = await governor.evaluate(risky_tool, {})
    # 手动改写窗口为已过去
    past = datetime.now(UTC) - timedelta(hours=2)
    proposal.available_between = [
        (past - timedelta(minutes=10)).isoformat(),
        past.isoformat(),
    ]
    await governor._save(proposal)
    await governor.decide(proposal.proposal_id, True, "op-1", "operator")

    calls = []

    async def _action():
        calls.append("executed")
        return "ok"

    ok, reason = await governor.execute_approved(proposal.proposal_id, _action)
    assert ok is False
    assert reason == "proposal_window_expired"
    assert calls == []


def test_c5_is_within_window_handles_malformed_values():
    """C5：非 2 长度或非法 ISO 时间视为不限制（保守放行）。"""
    from app.agent.action_governor import ActionProposal

    proposal = ActionProposal(
        proposal_id="p1",
        tool_name="t",
        risk_level=1,
        required_role="operator",
        available_between=[],
    )
    assert proposal.is_within_window() is True
    proposal.available_between = ["not-iso", "still-not"]
    assert proposal.is_within_window() is True


async def test_interceptor_intercepts_risky_tool_without_execution(risky_tool):
    """治理拦截器：写工具调用被转换为提案文本，真实 handler 不被触达"""
    touched = []

    async def handler(request):  # noqa: ARG001
        touched.append(request.name)
        return CallToolResult(content=[TextContent(type="text", text="executed!")])

    class _Request:
        name = risky_tool
        args = {"namespace": "default"}
        server_name = "ops"

    with role_context("admin"):  # admin 也拦——拦截与角色无关，只看风险等级
        result = await governance_interceptor(_Request(), handler)  # type: ignore[arg-type]

    assert touched == []
    assert not result.isError
    assert "待人工审批" in result.content[0].text


# ---- HTTP API ----


def test_actions_api_lifecycle(risky_tool):
    import asyncio as _asyncio

    proposal = _asyncio.run(action_governor.evaluate(risky_tool, {"deployment": "api"}))

    listing = client.get("/api/actions/proposals")
    assert listing.status_code == 200
    assert any(
        item["proposal_id"] == proposal.proposal_id
        for item in listing.json()["proposals"]
    )

    approved = client.post(
        f"/api/actions/proposals/{proposal.proposal_id}/approve", json={"actor": "op-1"}
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == STATUS_APPROVED

    # 已批准的提案不能再次决策
    assert (
        client.post(
            f"/api/actions/proposals/{proposal.proposal_id}/approve"
        ).status_code
        == 409
    )
    assert client.get("/api/actions/proposals").status_code == 200
    assert client.post("/api/actions/proposals/act-missing/approve").status_code == 404


def test_actions_api_reject(risky_tool):
    import asyncio as _asyncio

    proposal = _asyncio.run(action_governor.evaluate(risky_tool, {}))
    rejected = client.post(
        f"/api/actions/proposals/{proposal.proposal_id}/reject", json={"actor": "op-1"}
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == STATUS_REJECTED
