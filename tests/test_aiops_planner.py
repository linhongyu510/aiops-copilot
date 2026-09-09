"""F3：Planner 失败重试与降级计划标记"""

import importlib

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.agent.aiops.models import normalize_plan
from app.agent.aiops.planner import DEFAULT_PLAN, _parse_plan_steps

# app.agent.aiops.__init__ 将 planner 导出为函数，遮蔽了模块名，需显式导入模块
planner_module = importlib.import_module("app.agent.aiops.planner")


class _FakeRetrieveTool:
    """离线替代 retrieve_knowledge（避免访问 Milvus）"""

    name = "retrieve_knowledge"
    description = "检索内部运维知识库"

    async def ainvoke(self, args):  # noqa: ARG002
        return ""


class _FakeMCPClient:
    async def get_tools(self):
        return []


class _AlwaysFailingLLM:
    """structured output 与纯文本调用都失败的 LLM"""

    def with_structured_output(self, schema, **_kwargs):  # noqa: ARG002
        async def _fail(_input):
            raise RuntimeError("structured output 不受支持")

        return RunnableLambda(_fail)

    async def ainvoke(self, messages):  # noqa: ARG002
        raise RuntimeError("LLM 服务不可用")


class _PlainTextFallbackLLM:
    """structured output 失败，但纯文本降级重试成功的 LLM"""

    def __init__(self, text: str):
        self._text = text
        self.ainvoke_calls = 0

    def with_structured_output(self, schema, **_kwargs):  # noqa: ARG002
        async def _fail(_input):
            raise RuntimeError("structured output 不受支持")

        return RunnableLambda(_fail)

    async def ainvoke(self, messages):  # noqa: ARG002
        self.ainvoke_calls += 1
        return AIMessage(content=self._text)


def _patch_dependencies(monkeypatch, llm) -> None:
    async def _fake_mcp_client():
        return _FakeMCPClient()

    monkeypatch.setattr(planner_module, "get_mcp_client_with_retry", _fake_mcp_client)
    monkeypatch.setattr(planner_module, "retrieve_knowledge", _FakeRetrieveTool())
    monkeypatch.setattr(planner_module.llm_factory, "create_chat_model", lambda **kwargs: llm)


def _state() -> dict:
    return {
        "input": "诊断系统告警",
        "plan": [],
        "past_steps": [],
        "degraded": False,
        "response": "",
    }


async def test_planner_returns_default_plan_with_degraded_flag_after_retry(monkeypatch):
    """F3：structured output 失败且重试仍失败时，返回默认计划并显式标记 degraded"""
    _patch_dependencies(monkeypatch, _AlwaysFailingLLM())

    result = await planner_module.planner(_state())

    assert result["plan"] == normalize_plan(DEFAULT_PLAN)
    assert result["degraded"] is True


async def test_planner_plain_text_retry_succeeds_without_degraded(monkeypatch):
    """F3：structured output 失败后，纯文本降级重试成功则不标记 degraded"""
    llm = _PlainTextFallbackLLM("步骤1: 查询当前活跃告警\n步骤2: 分析告警日志\n步骤3: 生成诊断报告")
    _patch_dependencies(monkeypatch, llm)

    result = await planner_module.planner(_state())

    assert result["plan"] == normalize_plan(
        ["查询当前活跃告警", "分析告警日志", "生成诊断报告"]
    )
    assert result["degraded"] is False
    # 确认确实走了纯文本重试路径
    assert llm.ainvoke_calls == 1


def test_parse_plan_steps_formats():
    """F3：纯文本兜底解析支持 步骤N:/数字序号/列表符 等常见格式"""
    text = """
步骤1: 查询告警列表
2. 分析日志证据
- 生成诊断报告
4、汇总结论

"""
    assert _parse_plan_steps(text) == ["查询告警列表", "分析日志证据", "生成诊断报告", "汇总结论"]


def test_parse_plan_steps_empty_text():
    assert _parse_plan_steps("") == []
    assert _parse_plan_steps("\n\n  \n") == []
