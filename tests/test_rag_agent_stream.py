"""F6：RagAgentService 中间件挂载、SystemMessage 注入、流式错误事件与确定性路由"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import app.services.rag_agent_service as rag_agent_service_module
from app.config import config as app_config
from app.services.rag_agent_service import RagAgentService


async def test_create_agent_receives_system_prompt_and_trim_middleware(monkeypatch):
    """F6a/F6b：create_agent 必须挂载 trim 中间件，并通过 system_prompt 注入系统提示词"""
    service = RagAgentService(streaming=False)
    captured: dict = {}

    class _FakeMCPClient:
        async def get_tools(self):
            return []

    async def _fake_mcp_client():
        return _FakeMCPClient()

    def _fake_create_agent(model, tools=None, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(rag_agent_service_module, "get_mcp_client_with_retry", _fake_mcp_client)
    monkeypatch.setattr(rag_agent_service_module, "create_agent", _fake_create_agent)

    await service._initialize_agent()

    assert captured["system_prompt"] == service.system_prompt
    assert rag_agent_service_module.trim_messages_middleware in captured["middleware"]
    assert captured["checkpointer"] is service.checkpointer


async def test_query_does_not_append_system_message_per_request(monkeypatch):
    """F6b：同一 thread 第 N 轮请求不得再向 checkpoint 追加 SystemMessage"""
    service = RagAgentService(streaming=False)
    recorded: list[dict] = []

    async def _no_init():
        service._agent_initialized = True

        class _Agent:
            async def ainvoke(self, input, config):  # noqa: ARG002
                recorded.append(input)
                return {"messages": [HumanMessage(content="q"), AIMessage(content="回答")]}

        service.agent = _Agent()

    monkeypatch.setattr(service, "_initialize_agent", _no_init)

    answer1 = await service.query("问题1", session_id="thread-1")
    answer2 = await service.query("问题2", session_id="thread-1")

    assert answer1 == "回答"
    assert answer2 == "回答"
    assert len(recorded) == 2
    for agent_input in recorded:
        messages = agent_input["messages"]
        # 每次请求只携带用户消息；系统提示词由 create_agent 的 system_prompt 动态注入
        assert len(messages) == 1
        assert all(not isinstance(m, SystemMessage) for m in messages)


async def test_query_stream_yields_single_error_event_without_raising(monkeypatch):
    """F6c：query_stream 出错时只 yield 一条 error 事件，不向调用方 raise"""
    service = RagAgentService(streaming=True)

    async def _no_init():
        service._agent_initialized = True

        class _Agent:
            def astream(self, *args, **kwargs):  # noqa: ARG002
                async def _gen():
                    raise RuntimeError("agent exploded")
                    yield  # pragma: no cover

                return _gen()

        service.agent = _Agent()

    monkeypatch.setattr(service, "_initialize_agent", _no_init)

    events = [event async for event in service.query_stream("你好", session_id="s1")]

    # 客户端只会收到这一条 error（若再 raise，api/chat.py 会补发第二条）
    assert events == [{"type": "error", "data": "agent exploded"}]


async def test_query_stream_internal_knowledge_route_matches_query(monkeypatch):
    """F6d：流式分支与非流式共用"内部运维知识库"确定性路由，产出等价事件"""
    service = RagAgentService(streaming=True)
    agent_used = False

    async def _no_init():
        service._agent_initialized = True

        class _Agent:
            def astream(self, *args, **kwargs):  # noqa: ARG002
                nonlocal agent_used
                agent_used = True
                raise AssertionError("内部知识库路由不应进入 ReAct Agent")

        service.agent = _Agent()

    monkeypatch.setattr(service, "_initialize_agent", _no_init)

    async def _fake_retrieve(question):  # noqa: ARG002
        return "内部知识内容"

    class _FakeModel:
        async def ainvoke(self, messages):  # noqa: ARG002
            return AIMessage(content="内部知识回答")

    monkeypatch.setattr(service, "_retrieve_internal_knowledge", _fake_retrieve)
    monkeypatch.setattr(service, "model", _FakeModel())

    events = [
        event
        async for event in service.query_stream("请基于内部运维知识库回答如何排查", session_id="s2")
    ]

    assert agent_used is False
    assert events[0] == {"type": "content", "data": "内部知识回答"}
    assert events[-1] == {"type": "complete"}


async def test_query_passes_recursion_limit_to_graph(monkeypatch):
    """graph ainvoke 的 config 必须携带 chat recursion_limit，防止 Agent 无限循环"""
    service = RagAgentService(streaming=False)
    captured: dict = {}

    async def _no_init():
        service._agent_initialized = True

        class _Agent:
            async def ainvoke(self, input, config):  # noqa: ARG002
                captured.update(config)
                return {"messages": [AIMessage(content="回答")]}

        service.agent = _Agent()

    monkeypatch.setattr(service, "_initialize_agent", _no_init)

    await service.query("问题", session_id="thread-rl")

    assert captured["recursion_limit"] == app_config.chat_recursion_limit
    assert captured["configurable"]["thread_id"] == "thread-rl"


async def test_query_stream_passes_recursion_limit_to_graph(monkeypatch):
    """graph astream 的 config 必须携带与非流式一致的 recursion_limit"""
    service = RagAgentService(streaming=True)
    captured: dict = {}

    async def _no_init():
        service._agent_initialized = True

        class _Agent:
            def astream(self, input, config, **kwargs):  # noqa: ARG002
                captured.update(config)

                async def _gen():
                    return
                    yield  # pragma: no cover

                return _gen()

        service.agent = _Agent()

    monkeypatch.setattr(service, "_initialize_agent", _no_init)

    events = [event async for event in service.query_stream("问题", session_id="thread-rl")]

    assert captured["recursion_limit"] == app_config.chat_recursion_limit
    assert events[-1] == {"type": "complete"}


def test_is_internal_knowledge_query_shared_predicate():
    """F6d：路由判定条件与非流式 query() 完全一致"""
    assert RagAgentService._is_internal_knowledge_query("请基于内部运维知识库回答") is True
    assert RagAgentService._is_internal_knowledge_query("今天天气如何") is False
