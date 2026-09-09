"""诊断链路（Plan-Execute-Replan）确定性工具路由裁剪"""

import importlib
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from app.agent.tool_router import DEFAULT_TOOL_PREFIXES, select_relevant_tools
from app.config import config

# app.agent.aiops.__init__ 将 executor/planner 导出为函数，遮蔽了模块名，需显式导入模块
executor_module = importlib.import_module("app.agent.aiops.executor")
planner_module = importlib.import_module("app.agent.aiops.planner")


def _named_tools(*names: str) -> list:
    return [SimpleNamespace(name=name, description=f"{name} 的描述") for name in names]


# ---- select_relevant_tools 单元行为 ----


def test_select_keyword_hit_includes_relevant_tools():
    tools = _named_tools(
        "retrieve_knowledge", "get_current_time", "mysql_query", "mysql_explain",
        "redis_info", "query_cpu_metrics",
    )
    selected = select_relevant_tools("数据库出现慢查询，请分析", tools, limit=12)
    names = [t.name for t in selected]
    assert "mysql_query" in names
    assert "mysql_explain" in names
    assert "redis_info" not in names


def test_select_no_keyword_hit_falls_back_to_defaults():
    tools = _named_tools(*DEFAULT_TOOL_PREFIXES, "mysql_query", "redis_info")
    selected = select_relevant_tools("随便看看情况", tools, limit=12)
    names = [t.name for t in selected]
    for prefix in ("retrieve_knowledge", "get_current_time", "search_log"):
        assert prefix in names
    assert "mysql_query" not in names


def test_select_respects_limit_and_preserves_order():
    tools = _named_tools(*[f"prom_tool_{i}" for i in range(20)], "prom_query_extra")
    selected = select_relevant_tools("看一下监控指标", tools, limit=5)
    assert len(selected) == 5


def test_select_empty_tool_list_returns_empty():
    assert select_relevant_tools("任意问题", []) == []


def test_select_local_tools_available_via_fallback():
    tools = _named_tools("retrieve_knowledge", "get_current_time", "k8s_list_pods")
    selected = select_relevant_tools("没有命中任何关键词的问题", tools, limit=12)
    names = [t.name for t in selected]
    assert "retrieve_knowledge" in names
    assert "get_current_time" in names


# ---- Executor：裁剪后再 bind_tools ----


class _RecordingLLM:
    """记录 bind_tools 收到的工具列表的假 LLM"""

    def __init__(self):
        self.bound_tools: list = []

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return self

    async def ainvoke(self, messages):  # noqa: ARG002
        return AIMessage(content="步骤结果")


class _FakeMCPClient:
    def __init__(self, tools: list):
        self._tools = tools

    async def get_tools(self):
        return self._tools


def _patch_executor(monkeypatch, llm, tools: list) -> None:
    async def _fake_mcp_client():
        return _FakeMCPClient(tools)

    monkeypatch.setattr(executor_module, "get_mcp_client_with_retry", _fake_mcp_client)
    monkeypatch.setattr(
        executor_module.llm_factory, "create_chat_model", lambda **kwargs: llm
    )


async def test_executor_binds_pruned_tools_within_limit(monkeypatch):
    """绑定给 LLM 的工具数不超过 aiops_max_exposed_tools，且命中关键词的工具入选"""
    mcp_tools = _named_tools(
        "query_cpu_metrics", "query_memory_metrics", "prom_query", "prom_active_alerts",
        *[f"mysql_tool_{i}" for i in range(15)],
        "redis_info", "k8s_list_pods",
    )
    llm = _RecordingLLM()
    _patch_executor(monkeypatch, llm, mcp_tools)

    state = {
        "input": "诊断服务 CPU 飙高",
        "plan": ["查询服务的 CPU 与内存监控指标"],
        "past_steps": [],
        "degraded": False,
        "response": "",
    }
    result = await executor_module.executor(state)

    bound_names = [t.name for t in llm.bound_tools]
    assert 0 < len(bound_names) <= config.aiops_max_exposed_tools
    assert "query_cpu_metrics" in bound_names
    assert "mysql_tool_14" not in bound_names
    assert result["past_steps"][0][1] == "步骤结果"


async def test_executor_fallback_tools_present_without_keyword_hit(monkeypatch):
    """步骤与输入都无关键词命中时，兜底工具（含本地工具）仍可用"""
    mcp_tools = _named_tools(
        "search_log", "prom_active_alerts", "get_current_time",
        *[f"mysql_tool_{i}" for i in range(15)],
    )
    llm = _RecordingLLM()
    _patch_executor(monkeypatch, llm, mcp_tools)

    state = {
        "input": "看看服务状态",
        "plan": ["收集相关信息"],
        "past_steps": [],
        "degraded": False,
        "response": "",
    }
    result = await executor_module.executor(state)

    bound_names = [t.name for t in llm.bound_tools]
    assert "retrieve_knowledge" in bound_names
    assert "get_current_time" in bound_names
    assert result["past_steps"][0][1] == "步骤结果"


# ---- Planner：prompt 只注入裁剪后的工具描述 ----


class _FakeRetrieveTool:
    """离线替代 retrieve_knowledge（避免访问 Milvus）"""

    name = "retrieve_knowledge"
    description = "检索内部运维知识库"

    async def ainvoke(self, args):  # noqa: ARG002
        return ""


class _PromptRecordingLLM:
    """structured output 失败，记录纯文本降级重试时收到的 prompt"""

    def __init__(self):
        self.prompt_text = ""

    def with_structured_output(self, schema, **_kwargs):  # noqa: ARG002
        from langchain_core.runnables import RunnableLambda

        async def _fail(_input):
            raise RuntimeError("structured output 不受支持")

        return RunnableLambda(_fail)

    async def ainvoke(self, messages):
        self.prompt_text = "\n".join(str(m.content) for m in messages)
        return AIMessage(content="步骤1: 查询慢查询\n步骤2: 生成报告")


async def test_planner_prompt_only_describes_selected_tools(monkeypatch):
    """规划 prompt 只包含裁剪后的工具描述，未入选工具不出现在 prompt 中"""
    mcp_tools = _named_tools(
        "mysql_query", "mysql_explain", "query_cpu_metrics",
        *[f"redis_tool_{i}" for i in range(15)],
    )

    async def _fake_mcp_client():
        return _FakeMCPClient(mcp_tools)

    llm = _PromptRecordingLLM()
    monkeypatch.setattr(planner_module, "get_mcp_client_with_retry", _fake_mcp_client)
    monkeypatch.setattr(planner_module, "retrieve_knowledge", _FakeRetrieveTool())
    monkeypatch.setattr(
        planner_module.llm_factory, "create_chat_model", lambda **kwargs: llm
    )
    # P0-1 起 Planner 会先跑 Skill Registry 匹配，命中即短路 LLM。
    # 本测试聚焦 LLM prompt 裁剪逻辑，需要 stub 掉 Registry 让它返回空匹配。
    import app.agent.skills as skills_pkg

    class _EmptyRegistry:
        def match(self, _text):  # noqa: ARG002
            return []

        def format_matched_skills(self, _text):  # noqa: ARG002
            return ""

        def record_activation(self, _skill_id):  # noqa: ARG002
            return None

    monkeypatch.setattr(skills_pkg, "get_skill_registry", lambda: _EmptyRegistry())

    state = {
        "input": "数据库慢查询排查",
        "plan": [],
        "past_steps": [],
        "degraded": False,
        "response": "",
    }
    result = await planner_module.planner(state)

    assert result["degraded"] is False
    assert "mysql_query" in llm.prompt_text
    assert "redis_tool_14" not in llm.prompt_text
