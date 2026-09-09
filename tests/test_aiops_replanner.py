import importlib

import pytest

from app.agent.aiops.models import normalize_plan
from app.agent.aiops.replanner import Act, Response

module = importlib.import_module("app.agent.aiops.replanner")


class FakeChain:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.payload = None

    async def ainvoke(self, payload):
        self.payload = payload
        if self.error:
            raise self.error
        return self.result


class FakePrompt:
    def __init__(self, chain):
        self.chain = chain

    def __or__(self, _other):
        return self.chain


class FakeLLM:
    def with_structured_output(self, _schema, **_kwargs):
        return self


@pytest.fixture
def llm(monkeypatch):
    class Client:
        async def get_tools(self):
            return []

    monkeypatch.setattr(
        module.llm_factory, "create_chat_model", lambda **_kwargs: FakeLLM()
    )
    monkeypatch.setattr(
        module, "get_mcp_client_with_retry", lambda: _async_value(Client())
    )


async def _async_value(value):
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (Act(action="continue"), {}),
        (
            Act(action="replan", new_steps=["a", "b", "c"]),
            # 截断为剩余步骤数后归一化；id_offset=1（已有 1 个已完成步骤）避开历史 id
            {"plan": normalize_plan(["a", "b"], id_offset=1)},
        ),
        ({"action": "replan", "new_steps": []}, {}),
    ],
)
async def test_replanner_decision_branches(monkeypatch, llm, decision, expected) -> None:
    chain = FakeChain(decision)
    monkeypatch.setattr(module, "replanner_prompt", FakePrompt(chain))

    result = await module.replanner(
        {"input": "incident", "plan": ["one", "two"], "past_steps": [("x", "ok")]}
    )
    assert result == expected
    assert chain.payload["tools_description"]


@pytest.mark.asyncio
async def test_replanner_responds_and_blocks_late_replan(monkeypatch, llm) -> None:
    generated = {"response": "final"}

    async def fake_generate(_state):
        return generated

    monkeypatch.setattr(module, "_generate_response", fake_generate)
    monkeypatch.setattr(
        module,
        "replanner_prompt",
        FakePrompt(FakeChain(Act(action="replan", new_steps=["replacement"]))),
    )
    late = await module.replanner(
        {
            "input": "incident",
            "plan": ["one"],
            "past_steps": [(str(index), "ok") for index in range(5)],
        }
    )
    exhausted = await module.replanner(
        {
            "input": "incident",
            "plan": ["one"],
            "past_steps": [(str(index), "ok") for index in range(8)],
        }
    )
    assert late == generated
    assert exhausted == generated


@pytest.mark.asyncio
async def test_replanner_falls_back_when_tool_or_decision_fails(monkeypatch, llm) -> None:
    async def broken_client():
        raise ConnectionError("mcp down")

    monkeypatch.setattr(module, "get_mcp_client_with_retry", broken_client)
    monkeypatch.setattr(
        module,
        "replanner_prompt",
        FakePrompt(FakeChain(error=RuntimeError("bad structured output"))),
    )
    result = await module.replanner(
        {"input": "incident", "plan": ["one"], "past_steps": [("x", "result")]}
    )
    assert result == {}


@pytest.mark.asyncio
async def test_generate_response_success_and_fallback(monkeypatch, llm) -> None:
    success_chain = FakeChain(Response(response="# report"))
    monkeypatch.setattr(module, "response_prompt", FakePrompt(success_chain))
    result = await module._generate_response(
        {"input": "incident", "past_steps": [("check", "evidence")]}
    )
    assert result["response"] == "# report"
    assert isinstance(result["diagnosis_outcome"], dict)

    monkeypatch.setattr(
        module,
        "response_prompt",
        FakePrompt(FakeChain(error=TimeoutError("model timeout"))),
    )
    fallback = await module._generate_response(
        {"input": "incident", "past_steps": [("check", "x" * 250)]}
    )
    assert "任务执行结果" in fallback["response"]
    assert "..." in module._format_simple_steps([("check", "x" * 250)])
    assert module._format_simple_steps([]) == "无"


# --------------- P1-2 C1 & C3：profile 步数上限 + 结构化 DiagnosisOutcome ---------------


@pytest.mark.asyncio
async def test_c1_force_respond_uses_profile_max_steps(monkeypatch, llm) -> None:
    """C1：replanner_max_steps_before_force_respond 由 profile 提供时应生效。"""
    from app.agent.profiles.loader import DomainProfile

    override = DomainProfile(
        name="test", replanner_max_steps_before_force_respond=3, source="test:inline"
    )
    monkeypatch.setattr(module, "get_active_profile", lambda: override)

    generated = {"response": "final"}

    async def fake_generate(_state):
        return generated

    monkeypatch.setattr(module, "_generate_response", fake_generate)
    # 3 步 → 触发 profile 上限，直接生成响应
    result = await module.replanner(
        {
            "input": "incident",
            "plan": ["one"],
            "past_steps": [("s1", "ok"), ("s2", "ok"), ("s3", "ok")],
        }
    )
    assert result == generated


@pytest.mark.asyncio
async def test_c1_falls_back_to_hardcoded_when_profile_unset(monkeypatch, llm) -> None:
    """C1：profile 未配置 max_steps_before_force_respond 时回落到 8 步硬编码兜底。"""
    from app.agent.profiles.loader import DomainProfile

    override = DomainProfile(name="test", source="test:inline")
    monkeypatch.setattr(module, "get_active_profile", lambda: override)
    chain = FakeChain(Act(action="continue"))
    monkeypatch.setattr(module, "replanner_prompt", FakePrompt(chain))

    # 7 步 → 未超过兜底 8，走 LLM 决策路径而非强制响应
    result = await module.replanner(
        {
            "input": "incident",
            "plan": ["one"],
            "past_steps": [(f"s{i}", "ok") for i in range(7)],
        }
    )
    assert result == {}


def test_c3_diagnosis_outcome_extraction_from_state():
    """C3：_build_diagnosis_outcome 能从执行历史与 markdown 报告抽出结构化字段。"""
    state = {
        "input": "test",
        "past_steps": [
            ("查询 CPU 指标", "CPU 96%"),
            ("查询慢查询日志", "发现锁等待"),
            ("[变更提案 abc] 重启 pod api", "已生成提案，待审批"),
        ],
        "skill_context": {"verification_status": "passed"},
    }
    response = "## 根因\n慢查询导致连接池耗尽\n\n处理建议：...\n"
    outcome = module._build_diagnosis_outcome(state, response)
    assert outcome["root_cause"] == "慢查询导致连接池耗尽"
    assert outcome["verification_status"] == "passed"
    assert len(outcome["evidence"]) == 3
    assert len(outcome["actions_taken"]) == 3
    assert len(outcome["actions_proposed"]) == 1
    assert "变更提案" in outcome["actions_proposed"][0]


def test_c3_diagnosis_outcome_default_when_no_skill():
    """C3：未命中 Skill 时 verification_status 保持 unknown。"""
    outcome = module._build_diagnosis_outcome(
        {"input": "x", "past_steps": [], "skill_context": {}},
        "任意报告",
    )
    assert outcome["verification_status"] == "unknown"
    assert outcome["evidence"] == []
    assert outcome["actions_taken"] == []
