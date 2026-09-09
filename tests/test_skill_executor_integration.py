"""P0-2：Executor 参数模板渲染集成测试。

只覆盖模板渲染路径 —— LLM / MCP / tool_router 均已替换为可控 fake。
"""

import importlib
import json

from langchain_core.messages import AIMessage
from langchain_core.tools import tool

executor_module = importlib.import_module("app.agent.aiops.executor")


class _RecordingLLM:
    """记录 bind_tools 后 LLM 收到的 HumanMessage 内容。"""

    def __init__(self):
        self.human_messages: list[str] = []

    def bind_tools(self, tools):  # noqa: ARG002
        return self

    async def ainvoke(self, messages):
        for message in messages:
            if getattr(message, "type", "") == "human":
                self.human_messages.append(str(message.content))
        return AIMessage(content="fake final step result")


class _FakeMCPClient:
    def __init__(self, tools):
        self._tools = tools

    async def get_tools(self):
        return self._tools


def _patch_executor(monkeypatch, llm: _RecordingLLM) -> None:
    @tool
    async def query_es_health(cluster: str) -> str:
        """查询 ES 集群健康"""
        return f"health({cluster})=green"

    async def _fake_client():
        return _FakeMCPClient([query_es_health])

    monkeypatch.setattr(executor_module, "get_mcp_client_with_retry", _fake_client)
    monkeypatch.setattr(
        executor_module.llm_factory, "create_chat_model", lambda **_: llm
    )


async def test_executor_renders_skill_tool_args_template_into_task(monkeypatch):
    """Skill 步骤的 tool_args_template 会被渲染并作为「Skill 建议参数」附加到任务描述。"""
    llm = _RecordingLLM()
    _patch_executor(monkeypatch, llm)

    plan = [
        {
            "step_id": "sk:collect",
            "description": "查询集群健康",
            "tool_hint": "query_es_health",
            "expected_output": "健康状态",
            "depends_on": [],
            "skill_id": "es_red_health",
            "skill_step_id": "collect",
            "tool_args_template": {"cluster": "{{ context.cluster }}"},
            "optional": False,
        }
    ]
    state = {
        "input": "ES 集群 es-01 变红",
        "plan": plan,
        "past_steps": [],
        "artifacts": {},
        "completed_steps": [],
        "skill_id": "es_red_health",
        "skill_context": {
            "skill_id": "es_red_health",
            "cluster": "es-01",
            "labels": {"cluster": "es-01"},
        },
        "response": "",
    }

    result = await executor_module.executor(state)

    # 至少收到一次 HumanMessage，且内容里出现渲染后的建议参数
    assert llm.human_messages, "LLM 应被调用至少一次"
    combined = "\n".join(llm.human_messages)
    assert "Skill 建议参数" in combined
    # 渲染结果通过 json.dumps 序列化，键排序稳定
    assert json.dumps({"cluster": "es-01"}, ensure_ascii=False, sort_keys=True) in combined
    # 事件结构不变
    assert result["past_steps"][0][1] == "fake final step result"


async def test_executor_skips_template_hint_when_no_template(monkeypatch):
    """普通 plan 步骤（非 Skill 步）不应在任务描述里挂上 Skill 建议参数。"""
    llm = _RecordingLLM()
    _patch_executor(monkeypatch, llm)

    state = {
        "input": "普通任务",
        "plan": ["查询集群健康"],  # 字符串步骤 → normalize_plan 内部会归一
        "past_steps": [],
        "artifacts": {},
        "completed_steps": [],
        "response": "",
    }

    await executor_module.executor(state)

    combined = "\n".join(llm.human_messages)
    assert "Skill 建议参数" not in combined


async def test_executor_leaves_unresolved_placeholder_intact(monkeypatch):
    """占位符没命中 render_context 时，原样保留、不阻塞执行。"""
    llm = _RecordingLLM()
    _patch_executor(monkeypatch, llm)

    plan = [
        {
            "step_id": "sk:collect",
            "description": "查询集群健康",
            "tool_hint": "query_es_health",
            "expected_output": "健康状态",
            "depends_on": [],
            "skill_id": "es_red_health",
            "skill_step_id": "collect",
            # context 里没有 unknown 键，占位符必须原样保留
            "tool_args_template": {"cluster": "{{ context.unknown }}"},
            "optional": False,
        }
    ]
    state = {
        "input": "ES 集群变红",
        "plan": plan,
        "past_steps": [],
        "artifacts": {},
        "completed_steps": [],
        "skill_id": "es_red_health",
        "skill_context": {"skill_id": "es_red_health"},
        "response": "",
    }

    result = await executor_module.executor(state)

    combined = "\n".join(llm.human_messages)
    assert "{{ context.unknown }}" in combined
    # 步骤照常完成（LLM 未阻塞）
    assert result["past_steps"][0][1] == "fake final step result"
