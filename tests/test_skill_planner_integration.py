"""P0-1：Planner 命中 Skill 时直接采用 skill.steps 作为 plan（跳过 LLM）。"""

from __future__ import annotations

import importlib

from app.agent.skills.models import SkillPack, SkillStep, SkillTrigger, SkillVerification
from app.agent.skills.registry import SkillMetric, SkillRegistry

planner_module = importlib.import_module("app.agent.aiops.planner")


class _FakeRetrieveTool:
    name = "retrieve_knowledge"
    description = ""

    async def ainvoke(self, args):  # noqa: ARG002
        return ""


class _FakeMCPClient:
    async def get_tools(self):
        return []


class _ExplodingLLM:
    """Skill 命中时 planner 不应触达 LLM；LLM 一被调用就报错。"""

    def with_structured_output(self, schema, **_kwargs):  # noqa: ARG002
        async def _boom(_input):
            raise AssertionError("Skill 命中时不应触达 LLM")

        from langchain_core.runnables import RunnableLambda

        return RunnableLambda(_boom)

    async def ainvoke(self, messages):  # noqa: ARG002
        raise AssertionError("Skill 命中时不应触达 LLM")


def _install_registry_with(match_skill: SkillPack, monkeypatch):
    registry = SkillRegistry(skills_dir=None, include_legacy_playbooks=False)
    registry._skills = [match_skill]
    registry._rebuild_index()
    registry._metrics = {match_skill.skill_id: SkillMetric()}
    registry._loaded = True

    import app.agent.skills as skills_pkg

    monkeypatch.setattr(skills_pkg, "skill_registry", registry, raising=False)
    monkeypatch.setattr(skills_pkg, "get_skill_registry", lambda: registry)
    return registry


def _patch_planner_deps(monkeypatch, llm):
    async def _fake_mcp_client():
        return _FakeMCPClient()

    monkeypatch.setattr(planner_module, "get_mcp_client_with_retry", _fake_mcp_client)
    monkeypatch.setattr(planner_module, "retrieve_knowledge", _FakeRetrieveTool())
    monkeypatch.setattr(planner_module.llm_factory, "create_chat_model", lambda **kwargs: llm)


def _state(input_text: str) -> dict:
    return {
        "input": input_text,
        "plan": [],
        "past_steps": [],
        "artifacts": {},
        "degraded": False,
        "skill_id": "",
        "skill_context": {},
        "response": "",
    }


async def test_skill_match_bypasses_llm_and_returns_skill_plan(monkeypatch):
    """Kafka lag query 命中 Skill 时，plan 应完全来自 skill.steps，且带 skill_id。"""
    skill = SkillPack(
        skill_id="test.kafka",
        title="Kafka 消费积压排障",
        trigger=SkillTrigger(symptoms=["kafka lag", "消费积压", "消息堆积"]),
        required_tools=[],
        steps=[
            SkillStep(id="lag", description="查询 lag 曲线", tool_hint="prom_query_range"),
            SkillStep(
                id="log",
                description="消费者日志异常",
                tool_hint="loki_query",
                depends_on=["lag"],
            ),
        ],
        verifications=[SkillVerification(tool="prom_query", success_expr="ok")],
    )
    _install_registry_with(skill, monkeypatch)
    _patch_planner_deps(monkeypatch, _ExplodingLLM())

    result = await planner_module.planner(_state("Kafka 消费积压严重，请排查"))

    assert result["skill_id"] == "test.kafka"
    assert result["degraded"] is False
    assert len(result["plan"]) == 2
    ids = [step["step_id"] for step in result["plan"]]
    assert ids == ["sk:lag", "sk:log"]
    assert result["plan"][1]["depends_on"] == ["sk:lag"]
    assert result["plan"][0]["skill_id"] == "test.kafka"
    assert result["plan"][0]["tool_hint"] == "prom_query_range"
    # skill_context 应携带 match 信号与 verifications 快照
    ctx = result["skill_context"]
    assert ctx["skill_id"] == "test.kafka"
    assert ctx["match_score"] > 0
    assert ctx["verifications"] and ctx["verifications"][0]["tool"] == "prom_query"
