"""P0.2：结构化计划 PlanStep + DAG 并行执行"""

import asyncio
import importlib
import time

from langchain_core.messages import AIMessage

from app.agent.aiops.models import (
    MAX_PLAN_STEPS,
    PlanStep,
    normalize_plan,
    ready_batch,
    step_description,
    step_task_text,
)

executor_module = importlib.import_module("app.agent.aiops.executor")
planner_module = importlib.import_module("app.agent.aiops.planner")


# ---- normalize_plan ----


def test_normalize_plan_translates_position_deps_to_stable_ids():
    steps = [
        PlanStep(description="查日志", depends_on=[]),
        PlanStep(description="查指标", depends_on=[]),
        PlanStep(description="关联分析", depends_on=[1, 2]),
    ]
    plan = normalize_plan(steps)
    assert [s["step_id"] for s in plan] == ["s1", "s2", "s3"]
    assert plan[2]["depends_on"] == ["s1", "s2"]
    assert plan[0]["depends_on"] == []


def test_normalize_plan_accepts_strings_and_dicts():
    plan = normalize_plan(
        ["纯字符串步骤", {"description": "dict 步骤", "tool_hint": "prom_query"}]
    )
    assert [s["description"] for s in plan] == ["纯字符串步骤", "dict 步骤"]
    assert plan[1]["tool_hint"] == "prom_query"


def test_normalize_plan_drops_invalid_and_self_deps_and_caps_length():
    plan = normalize_plan(
        [
            PlanStep(description="a"),
            PlanStep(description="b", depends_on=[1]),  # 合法前向依赖
            PlanStep(description="c", depends_on=[99]),  # 越界 → 丢弃
            PlanStep(description="d", depends_on=[4]),  # 自依赖 → 丢弃
        ]
        + [PlanStep(description=f"extra-{i}") for i in range(MAX_PLAN_STEPS)]
    )
    assert len(plan) == MAX_PLAN_STEPS
    assert plan[1]["depends_on"] == ["s1"]
    assert plan[2]["depends_on"] == []
    assert plan[3]["depends_on"] == []


def test_normalize_plan_id_offset_avoids_completed_collision():
    plan = normalize_plan(
        [PlanStep(description="新步骤1"), PlanStep(description="新步骤2", depends_on=[1])],
        id_offset=3,
    )
    assert [s["step_id"] for s in plan] == ["s4", "s5"]
    assert plan[1]["depends_on"] == ["s4"]


def test_step_task_text_includes_hints():
    plan = normalize_plan(
        [{"description": "查询指标", "tool_hint": "prom_query", "expected_output": "QPS 曲线"}]
    )
    text = step_task_text(plan[0])
    assert "查询指标" in text
    assert "prom_query" in text
    assert "QPS 曲线" in text
    assert step_description("裸字符串") == "裸字符串"


# ---- ready_batch ----


def _mini_plan():
    return normalize_plan(
        [
            PlanStep(description="独立A"),
            PlanStep(description="独立B"),
            PlanStep(description="依赖AB", depends_on=[1, 2]),
        ]
    )


def test_ready_batch_returns_only_independent_steps():
    plan = _mini_plan()
    batch = ready_batch(plan, completed_ids=set(), limit=3)
    assert [s["description"] for s in batch] == ["独立A", "独立B"]


def test_ready_batch_releases_dependent_after_completion():
    plan = _mini_plan()
    # 真实流程中已完成步骤会从剩余计划移除
    remaining = [s for s in plan if s["step_id"] == "s3"]
    batch = ready_batch(remaining, completed_ids={"s1", "s2"}, limit=3)
    assert [s["description"] for s in batch] == ["依赖AB"]


def test_normalize_plan_is_idempotent_on_normalized_dicts():
    """Executor 会对 state.plan 二次归一化：规范形态必须透传，字符串 id 依赖不丢失"""
    plan = _mini_plan()
    again = normalize_plan(plan)
    assert again == plan
    assert again[2]["depends_on"] == ["s1", "s2"]


def test_ready_batch_respects_limit_and_forces_first_on_deadlock():
    plan = _mini_plan()
    assert len(ready_batch(plan, set(), limit=1)) == 1
    # 依赖环（s1↔s2）导致无就绪步骤时，强制取第一个防止图卡死
    cyclic = [
        {"step_id": "s1", "description": "a", "depends_on": ["s2"]},
        {"step_id": "s2", "description": "b", "depends_on": ["s1"]},
    ]
    batch = ready_batch(cyclic, set(), limit=2)
    assert [s["step_id"] for s in batch] == ["s1"]


# ---- Executor DAG 调度 ----


class _NoToolLLM:
    """每个步骤直接返回文本结论的假 LLM"""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.calls = 0

    def bind_tools(self, tools):  # noqa: ARG002
        return self

    async def ainvoke(self, messages):  # noqa: ARG002
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return AIMessage(content=f"结论#{self.calls}")


class _FakeMCPClient:
    def __init__(self, tools: list):
        self._tools = tools

    async def get_tools(self):
        return self._tools


def _patch_executor(monkeypatch, llm, tools: list | None = None) -> None:
    async def _fake_mcp_client():
        return _FakeMCPClient(tools or [])

    monkeypatch.setattr(executor_module, "get_mcp_client_with_retry", _fake_mcp_client)
    monkeypatch.setattr(
        executor_module.llm_factory, "create_chat_model", lambda **kwargs: llm
    )


async def test_executor_runs_independent_steps_in_parallel(monkeypatch):
    """两个无依赖步骤在同一 executor 调用中并行完成"""
    llm = _NoToolLLM(delay=0.15)
    _patch_executor(monkeypatch, llm)

    plan = normalize_plan(
        [PlanStep(description="查CPU"), PlanStep(description="查内存"), PlanStep(description="汇总", depends_on=[1, 2])]
    )
    state = {
        "input": "诊断性能问题",
        "plan": plan,
        "completed_steps": [],
        "past_steps": [],
        "degraded": False,
        "response": "",
    }
    start = time.perf_counter()
    result = await executor_module.executor(state)
    elapsed = time.perf_counter() - start

    # 批次1：两个独立步骤并行（各 0.15s），总耗时应接近单步而非两倍
    assert elapsed < 0.28, f"独立步骤未并行执行，耗时 {elapsed:.3f}s"
    assert [step for step, _ in result["past_steps"]] == ["查CPU", "查内存"]
    assert result["completed_steps"] == ["s1", "s2"]
    # 依赖步骤 s3 留待下一批
    assert [step["step_id"] for step in result["plan"]] == ["s3"]

    # 第二次调用（completed 已含 s1/s2）执行依赖步骤
    state2 = {**state, "plan": result["plan"], "completed_steps": result["completed_steps"]}
    result2 = await executor_module.executor(state2)
    assert [step for step, _ in result2["past_steps"]] == ["汇总"]
    assert result2["plan"] == []


async def test_executor_respects_parallel_limit(monkeypatch):
    """并行上限 aiops_max_parallel_steps 生效：4 个独立步骤分两批"""
    monkeypatch.setattr(executor_module.config, "aiops_max_parallel_steps", 2)
    llm = _NoToolLLM()
    _patch_executor(monkeypatch, llm)

    plan = normalize_plan([PlanStep(description=f"步骤{i}") for i in range(1, 5)])
    state = {"input": "x", "plan": plan, "completed_steps": [], "past_steps": [], "degraded": False, "response": ""}
    result = await executor_module.executor(state)
    assert len(result["past_steps"]) == 2
    assert [s["step_id"] for s in result["plan"]] == ["s3", "s4"]


async def test_executor_string_plan_still_executes_single_batch(monkeypatch):
    """旧格式 list[str] 计划：全部视为无依赖步骤，兼容执行"""
    llm = _NoToolLLM()
    _patch_executor(monkeypatch, llm)

    state = {"input": "x", "plan": ["第一步", "第二步"], "completed_steps": [], "past_steps": [], "degraded": False, "response": ""}
    result = await executor_module.executor(state)
    assert [step for step, _ in result["past_steps"]] == ["第一步", "第二步"]
    assert result["plan"] == []
    assert result["completed_steps"] == ["s1", "s2"]


# ---- Planner 结构化输出 ----


async def test_planner_structured_output_normalizes_deps(monkeypatch):
    """structured output 路径：PlanStep 依赖被翻译为稳定 step_id"""

    class _StructuredLLM:
        def with_structured_output(self, schema, **_kwargs):  # noqa: ARG002
            from langchain_core.runnables import RunnableLambda

            async def _ok(_input):
                return planner_module.Plan(
                    steps=[
                        PlanStep(description="查日志"),
                        PlanStep(description="查指标"),
                        PlanStep(description="关联分析", depends_on=[1, 2]),
                    ]
                )

            return RunnableLambda(_ok)

        async def ainvoke(self, messages):  # noqa: ARG002
            raise AssertionError("不应走到纯文本降级")

    async def _fake_mcp_client():
        return _FakeMCPClient([])

    monkeypatch.setattr(planner_module, "get_mcp_client_with_retry", _fake_mcp_client)

    class _FakeRetrieveTool:
        name = "retrieve_knowledge"
        description = "检索知识库"

        async def ainvoke(self, args):  # noqa: ARG002
            return ""

    monkeypatch.setattr(planner_module, "retrieve_knowledge", _FakeRetrieveTool())
    monkeypatch.setattr(
        planner_module.llm_factory, "create_chat_model", lambda **kwargs: _StructuredLLM()
    )

    state = {"input": "诊断", "plan": [], "past_steps": [], "degraded": False, "response": ""}
    result = await planner_module.planner(state)

    assert result["degraded"] is False
    plan = result["plan"]
    assert [s["step_id"] for s in plan] == ["s1", "s2", "s3"]
    assert plan[2]["depends_on"] == ["s1", "s2"]
