"""P1-3：ActionProposal 携带 Skill 上下文。

覆盖：
- skill contextvar 的写入/清空语义（含 use_skill_context 的 merge 行为）
- action_governor.evaluate 从 contextvar 读取 skill_id/skill_step_id/incident_id
  并塞入 ActionProposal
- ActionProposal.to_prompt_text 中呈现 Skill / 步骤 / incident 归因
- 无 skill 上下文时字段留空，保持向后兼容
"""

from __future__ import annotations

import pytest

from app.agent.action_governor import (
    STATUS_PENDING,
    ActionGovernor,
    ActionProposal,
)
from app.agent.skills.context import (
    SkillTaskContext,
    get_current_skill_context,
    use_skill_context,
)
from app.agent.tool_registry import ToolSpec, tool_registry
from app.state_store import MemoryStateStore


@pytest.fixture
def risky_tool(monkeypatch):
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


# ---------------- skill contextvar 基本语义 ----------------


def test_default_skill_context_is_empty():
    ctx = get_current_skill_context()
    assert isinstance(ctx, SkillTaskContext)
    assert ctx.skill_id == ""
    assert ctx.skill_step_id == ""
    assert ctx.incident_id == ""


def test_use_skill_context_writes_and_restores():
    outer = get_current_skill_context()
    with use_skill_context(
        skill_id="skill-a", skill_step_id="s1", incident_id="inc-x"
    ) as ctx:
        assert ctx.skill_id == "skill-a"
        assert ctx.skill_step_id == "s1"
        assert ctx.incident_id == "inc-x"
        assert get_current_skill_context() == ctx
    # 退出后必须回滚
    assert get_current_skill_context() == outer


def test_use_skill_context_merge_preserves_unspecified_fields():
    """merge=True（默认）：新调用未指定的字段沿用父上下文。"""
    with use_skill_context(skill_id="skill-a", incident_id="inc-x"):
        # 只覆盖 step_id：skill_id / incident_id 应保留
        with use_skill_context(skill_step_id="s2"):
            ctx = get_current_skill_context()
            assert ctx.skill_id == "skill-a"
            assert ctx.skill_step_id == "s2"
            assert ctx.incident_id == "inc-x"


def test_use_skill_context_no_merge_clears_parent():
    """merge=False：显式建立干净归因，父上下文字段被清空。"""
    with use_skill_context(skill_id="skill-a", incident_id="inc-x"):
        with use_skill_context(incident_id="inc-y", merge=False):
            ctx = get_current_skill_context()
            assert ctx.skill_id == ""
            assert ctx.skill_step_id == ""
            assert ctx.incident_id == "inc-y"


# ---------------- ActionGovernor.evaluate 归因写入 ----------------


async def test_evaluate_reads_skill_context_into_proposal(governor, risky_tool):
    with use_skill_context(
        skill_id="mysql-slow-query-pool-exhaustion",
        skill_step_id="verify:0",
        incident_id="inc-abc123",
    ):
        proposal = await governor.evaluate(risky_tool, {"deployment": "api"})
    assert proposal is not None
    assert proposal.status == STATUS_PENDING
    assert proposal.skill_id == "mysql-slow-query-pool-exhaustion"
    assert proposal.skill_step_id == "verify:0"
    assert proposal.incident_id == "inc-abc123"


async def test_evaluate_without_skill_context_leaves_fields_empty(
    governor, risky_tool
):
    """未设置 contextvar 时提案字段保持空字符串，向后兼容。"""
    proposal = await governor.evaluate(risky_tool, {})
    assert proposal is not None
    assert proposal.skill_id == ""
    assert proposal.skill_step_id == ""
    assert proposal.incident_id == ""


async def test_proposal_persisted_with_skill_attribution(governor, risky_tool):
    """归因字段被 model_dump 一同持久化到 state_store，可被跨副本审批读取。"""
    with use_skill_context(
        skill_id="es-red", skill_step_id="s2", incident_id="inc-42"
    ):
        proposal = await governor.evaluate(risky_tool, {})

    other = ActionGovernor(store=governor._store)
    reloaded = await other.get(proposal.proposal_id)
    assert reloaded is not None
    assert reloaded.skill_id == "es-red"
    assert reloaded.skill_step_id == "s2"
    assert reloaded.incident_id == "inc-42"


# ---------------- to_prompt_text 展示归因 ----------------


def test_prompt_text_shows_skill_and_step():
    proposal = ActionProposal(
        proposal_id="act-1",
        tool_name="k8s_rollout_restart",
        risk_level=1,
        required_role="operator",
        skill_id="mysql-slow-query",
        skill_step_id="verify:1",
        incident_id="inc-9",
    )
    text = proposal.to_prompt_text()
    assert "Skill 'mysql-slow-query'" in text
    assert "步骤 verify:1" in text
    assert "inc-9" in text


def test_prompt_text_omits_skill_hint_when_missing():
    """未提供 skill 归因时不追加冗余提示行。"""
    proposal = ActionProposal(
        proposal_id="act-2",
        tool_name="k8s_rollout_restart",
        risk_level=1,
        required_role="operator",
    )
    text = proposal.to_prompt_text()
    assert "触发来源" not in text
    assert "关联事件" not in text


def test_prompt_text_shows_incident_only_when_no_skill():
    proposal = ActionProposal(
        proposal_id="act-3",
        tool_name="k8s_rollout_restart",
        risk_level=1,
        required_role="operator",
        incident_id="inc-solo",
    )
    text = proposal.to_prompt_text()
    assert "关联事件: inc-solo" in text
    # 无 skill 时不应硬拼 Skill 字样
    assert "Skill '" not in text
