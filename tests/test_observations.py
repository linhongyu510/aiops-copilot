"""P0.4：观测压缩 + 执行存档（artifacts）"""

import importlib

from langchain_core.messages import AIMessage

from app.agent.aiops.observations import compact_observation
from app.config import config

executor_module = importlib.import_module("app.agent.aiops.executor")
replanner_module = importlib.import_module("app.agent.aiops.replanner")


# ---- compact_observation ----


def test_short_text_unchanged():
    text = "CPU 使用率 96%，持续 10 分钟"
    assert compact_observation(text, 1200) == text


def test_long_text_keeps_head_and_tail_with_marker():
    text = "HEAD>>>" + "x" * 5000 + "<<<TAIL"
    compacted = compact_observation(text, 1000)
    assert compacted.startswith("HEAD>>>")
    assert compacted.endswith("<<<TAIL")
    assert "已省略中间" in compacted
    assert len(compacted) < 1200  # 显著小于原文


def test_compact_zero_or_negative_limit_returns_text():
    assert compact_observation("abc", 0) == "abc"


# ---- Executor 集成：past_steps 压缩 / artifacts 存档 ----


class _NoToolLLM:
    def bind_tools(self, tools):  # noqa: ARG002
        return self

    async def ainvoke(self, messages):  # noqa: ARG002
        return AIMessage(content="日志证据" + "y" * 5000 + "结论：连接池耗尽")


class _FakeMCPClient:
    def __init__(self, tools):
        self._tools = tools

    async def get_tools(self):
        return self._tools


async def test_executor_stores_compacted_view_and_full_artifact(monkeypatch):
    async def _fake_mcp_client():
        return _FakeMCPClient([])

    monkeypatch.setattr(executor_module, "get_mcp_client_with_retry", _fake_mcp_client)
    monkeypatch.setattr(
        executor_module.llm_factory, "create_chat_model", lambda **kwargs: _NoToolLLM()
    )

    from app.agent.aiops.models import normalize_plan

    state = {
        "input": "诊断日志",
        "plan": normalize_plan(["查询日志证据"]),
        "completed_steps": [],
        "past_steps": [],
        "artifacts": {},
        "degraded": False,
        "response": "",
    }
    result = await executor_module.executor(state)

    step, view = result["past_steps"][0]
    assert step == "查询日志证据"
    # 决策视图被压缩
    assert len(view) < config.aiops_observation_max_chars + 200
    assert "已省略中间" in view
    # 存档保留接近完整的原文（含头尾证据）
    artifact = result["artifacts"]["查询日志证据"]
    assert artifact.startswith("日志证据")
    assert artifact.endswith("结论：连接池耗尽")


# ---- _generate_response 优先引用存档 ----


async def test_generate_response_prefers_artifact_full_text(monkeypatch):
    """报告生成的执行历史应包含存档全文，而非 past_steps 的压缩视图"""
    captured: dict = {}

    class _FakeLLM:
        def with_structured_output(self, schema, **_kwargs):  # noqa: ARG002
            from langchain_core.runnables import RunnableLambda

            async def _ok(_input):
                return replanner_module.Response(response="# report")

            return RunnableLambda(_ok)

        async def ainvoke(self, messages):  # noqa: ARG002
            raise AssertionError("不应直接调用 ainvoke")

    class _Chain:
        async def ainvoke(self, payload):
            captured["messages"] = payload["messages"]
            return replanner_module.Response(response="# report")

    class _Prompt:
        def __or__(self, _other):
            return _Chain()

    monkeypatch.setattr(replanner_module, "response_prompt", _Prompt())
    monkeypatch.setattr(
        replanner_module.llm_factory, "create_chat_model", lambda **kwargs: _FakeLLM()
    )

    long_result = "开头证据" + "z" * 3000 + "结尾根因"
    state = {
        "input": "诊断",
        "past_steps": [("查询日志", compact_observation(long_result, config.aiops_observation_max_chars))],
        "artifacts": {"查询日志": long_result},
        "plan": [],
        "degraded": False,
        "response": "",
    }
    result = await replanner_module._generate_response(state)

    assert result["response"] == "# report"
    assert result["diagnosis_outcome"]["root_cause"] == "report"
    history_text = "\n".join(str(m[1]) for m in captured["messages"])
    # 存档全文进入报告上下文（压缩视图中被省略的中段不应出现，原文两端应出现）
    assert "开头证据" in history_text
    assert "结尾根因" in history_text
