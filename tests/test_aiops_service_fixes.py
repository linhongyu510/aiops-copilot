"""F1/F3/F5：AIOpsService 跨请求状态隔离、降级计划事件、recursion_limit 与总超时"""

import asyncio
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.config import config
from app.services.aiops_service import AIOpsService


class _FakeGraph:
    """记录每次 astream 的 config，并立即产出最终响应的假图"""

    def __init__(self):
        self.configs: list[dict] = []

    async def astream(self, input, config, stream_mode):  # noqa: ARG002
        self.configs.append(config)
        yield {"replanner": {"response": "诊断报告"}}

    async def aget_state(self, config):  # noqa: ARG002
        return SimpleNamespace(values={"response": "诊断报告"})


class _SlowGraph:
    """先产出一个 plan 事件，然后长时间阻塞的假图（用于总超时测试）"""

    async def astream(self, input, config, stream_mode):  # noqa: ARG002
        yield {"planner": {"plan": ["步骤1"], "degraded": False}}
        await asyncio.sleep(60)

    async def aget_state(self, config):  # noqa: ARG002
        return SimpleNamespace(values={})


def _make_service(graph) -> AIOpsService:
    """绕过 __init__（避免构建真实图），直接注入假图"""
    service = AIOpsService.__new__(AIOpsService)
    service.graph = graph
    return service


def test_configure_checkpointer_rebuilds_real_diagnosis_graph() -> None:
    service = AIOpsService()
    replacement = MemorySaver()

    service.configure_checkpointer(replacement)

    assert service.checkpointer is replacement
    assert service.graph.checkpointer is replacement


async def test_each_execute_uses_unique_uuid_thread_id():
    """F1：同一 session_id 连续两次诊断必须使用不同的 uuid4 thread_id"""
    graph = _FakeGraph()
    service = _make_service(graph)

    events1 = [event async for event in service.execute("task", session_id="same-session")]
    events2 = [event async for event in service.execute("task", session_id="same-session")]

    tid1 = graph.configs[0]["configurable"]["thread_id"]
    tid2 = graph.configs[1]["configurable"]["thread_id"]

    # 两次诊断 thread_id 不同，且不复用调用方 session_id
    assert tid1 != tid2
    assert tid1 != "same-session"
    assert tid2 != "same-session"
    # uuid4().hex 为 32 位十六进制
    for tid in (tid1, tid2):
        assert len(tid) == 32
        assert all(c in "0123456789abcdef" for c in tid)

    # thread_id 通过事件透出，便于调用方追踪
    assert events1[0]["type"] == "status"
    assert events1[0]["thread_id"] == tid1
    assert events1[-1]["type"] == "complete"
    assert events1[-1]["thread_id"] == tid1
    assert events2[0]["thread_id"] == tid2


async def test_execute_passes_recursion_limit_to_graph():
    """F5：graph.astream 的 config 必须携带 recursion_limit"""
    graph = _FakeGraph()
    service = _make_service(graph)

    async for _ in service.execute("task"):
        pass

    assert graph.configs[0]["recursion_limit"] == config.aiops_recursion_limit


async def test_execute_emits_timeout_error_event_on_total_timeout(monkeypatch):
    """F5：超过总时间预算后终止图的执行，并发出明确的 timeout 错误事件"""
    monkeypatch.setattr(config, "aiops_total_timeout_seconds", 0.2)
    service = _make_service(_SlowGraph())

    events = [event async for event in service.execute("task")]

    # 已收到 starting 与 plan 事件，最后一个事件为 timeout 错误
    assert events[0]["type"] == "status"
    assert events[1]["type"] == "plan"
    last = events[-1]
    assert last["type"] == "error"
    assert last["stage"] == "timeout"
    assert "总时间预算" in last["message"]
    assert last["thread_id"]


def test_planner_event_marks_degraded_plan():
    """F3：降级计划在 SSE 事件中显式标记 degraded 并在 message 中注明"""
    service = _make_service(None)

    degraded_event = service._format_planner_event(
        {"plan": ["收集相关信息", "分析数据", "生成报告"], "degraded": True}
    )
    assert degraded_event["type"] == "plan"
    assert degraded_event["degraded"] is True
    assert "降级" in degraded_event["message"]

    normal_event = service._format_planner_event({"plan": ["步骤1"], "degraded": False})
    assert normal_event["degraded"] is False
    assert "降级" not in normal_event["message"]


@pytest.mark.parametrize("bad_output", [{}, None])
def test_planner_event_handles_empty_state(bad_output):
    """Planner 节点空输出时保持原有 status 事件结构"""
    service = _make_service(None)
    event = service._format_planner_event(bad_output)
    if bad_output is None:
        assert event["type"] == "status"
    else:
        assert event["type"] == "plan"
        assert event["degraded"] is False
