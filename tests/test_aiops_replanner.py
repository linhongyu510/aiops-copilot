import importlib

import pytest

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
    def with_structured_output(self, _schema):
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
        (Act(action="replan", new_steps=["a", "b", "c"]), {"plan": ["a", "b"]}),
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
    assert result == {"response": "# report"}

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
