"""F2：Executor 多轮（链式）工具调用循环"""

import importlib

from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from app.config import config

# app.agent.aiops.__init__ 将 executor 导出为函数，遮蔽了模块名，需显式导入模块
executor_module = importlib.import_module("app.agent.aiops.executor")


def _tool_call_msg(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


class _ScriptedLLM:
    """按脚本依次返回响应的假 LLM；bind_tools 返回自身"""

    def __init__(self, responses: list[AIMessage]):
        self._responses = list(responses)
        self.calls = 0

    def bind_tools(self, tools):  # noqa: ARG002
        return self

    async def ainvoke(self, messages):  # noqa: ARG002
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


class _FakeMCPClient:
    def __init__(self, tools: list):
        self._tools = tools

    async def get_tools(self):
        return self._tools


def _make_cls_tools(calls: list) -> list:
    """模拟 CLS 的两段式工具：先查 topic_id，再查日志"""

    @tool
    async def search_topic_by_service_name(service_name: str) -> str:
        """根据服务名查找 CLS 日志主题"""
        calls.append(("search_topic_by_service_name", service_name))
        return '{"topics": [{"topic_id": "topic-001"}]}'

    @tool
    async def search_log(topic_id: str) -> str:
        """按 topic_id 查询 CLS 日志"""
        calls.append(("search_log", topic_id))
        return "ERROR 数据库连接池耗尽"

    return [search_topic_by_service_name, search_log]


def _patch_dependencies(monkeypatch, llm: _ScriptedLLM, tools: list) -> None:
    async def _fake_mcp_client():
        return _FakeMCPClient(tools)

    monkeypatch.setattr(executor_module, "get_mcp_client_with_retry", _fake_mcp_client)
    monkeypatch.setattr(
        executor_module.llm_factory, "create_chat_model", lambda **kwargs: llm
    )


def _state() -> dict:
    return {
        "input": "诊断告警",
        "plan": ["查询 data-sync-service 的 CLS 日志"],
        "past_steps": [],
        "degraded": False,
        "response": "",
    }


async def test_executor_chains_tool_calls_until_final_answer(monkeypatch):
    """F2：第一轮返回 tool_calls、第二轮仍返回 tool_calls 时，必须继续执行而非丢弃"""
    calls: list = []
    llm = _ScriptedLLM(
        [
            _tool_call_msg("search_topic_by_service_name", {"service_name": "data-sync"}, "c1"),
            _tool_call_msg("search_log", {"topic_id": "topic-001"}, "c2"),
            AIMessage(content="步骤结果：日志显示数据库连接池耗尽"),
        ]
    )
    _patch_dependencies(monkeypatch, llm, _make_cls_tools(calls))

    result = await executor_module.executor(_state())

    # 两个工具按顺序都被调用（链式两段调用未被丢弃）
    assert [name for name, _ in calls] == ["search_topic_by_service_name", "search_log"]
    # LLM 共调用 3 次：2 次工具决策 + 1 次最终总结
    assert llm.calls == 3
    # 输出事件结构不变：plan 移除已执行步骤，past_steps 追加结果
    assert result["plan"] == []
    step, step_result = result["past_steps"][0]
    assert step == "查询 data-sync-service 的 CLS 日志"
    assert "数据库连接池耗尽" in step_result


async def test_executor_without_tool_calls_returns_directly(monkeypatch):
    """F2 回归：LLM 不请求工具调用时直接返回其内容"""
    llm = _ScriptedLLM([AIMessage(content="无需工具的直接回答")])
    _patch_dependencies(monkeypatch, llm, _make_cls_tools([]))

    result = await executor_module.executor(_state())

    assert llm.calls == 1
    assert result["past_steps"][0][1] == "无需工具的直接回答"


async def test_executor_stops_at_max_iterations_and_summarizes_without_tools(monkeypatch):
    """F2：达到迭代上限后，最后一轮不再绑定工具，强制生成步骤总结"""
    monkeypatch.setattr(config, "agent_step_max_iterations", 2)
    calls: list = []
    llm = _ScriptedLLM(
        [
            _tool_call_msg("search_topic_by_service_name", {"service_name": "data-sync"}, "c1"),
            _tool_call_msg("search_log", {"topic_id": "topic-001"}, "c2"),
            AIMessage(content="基于已有工具结果的强制总结"),
        ]
    )
    _patch_dependencies(monkeypatch, llm, _make_cls_tools(calls))

    result = await executor_module.executor(_state())

    # 2 轮绑定工具的循环 + 1 轮未绑定工具的总结
    assert llm.calls == 3
    assert [name for name, _ in calls] == ["search_topic_by_service_name", "search_log"]
    assert result["past_steps"][0][1] == "基于已有工具结果的强制总结"


async def test_executor_failure_keeps_event_structure(monkeypatch):
    """F2 回归：步骤异常时仍返回 plan/past_steps 结构"""
    async def _failing_mcp_client():
        raise RuntimeError("MCP 不可用")

    monkeypatch.setattr(executor_module, "get_mcp_client_with_retry", _failing_mcp_client)

    result = await executor_module.executor(_state())

    assert result["plan"] == []
    assert "执行失败" in result["past_steps"][0][1]
