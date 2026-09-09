"""AIOps 诊断链路离线评测：数据集形状、指标计算与 dry-run 全链路冒烟。"""

import json
from collections import Counter
from pathlib import Path

import pytest

from app.services.aiops_service import AIOpsService
from evaluation.aiops_eval import (
    DEFAULT_DATASET,
    DRY_RUN_PLAN,
    DRY_RUN_REPORT,
    NODE_MODULES,
    DryRunLLM,
    FakeMCPClient,
    ToolRecorder,
    build_stub_tools,
    fake_retrieve_knowledge,
    load_cases,
    run_case,
    summarize,
)

EXPECTED_FIELDS = {
    "case_id",
    "category",
    "question",
    "expected_tools",
    "required_keywords",
    "max_steps",
    "label_origin",
    "review_status",
}


def test_dataset_shape_and_labels() -> None:
    cases = load_cases(DEFAULT_DATASET)
    assert len(cases) >= 20
    assert len({case["case_id"] for case in cases}) == len(cases)
    for case in cases:
        assert EXPECTED_FIELDS.issubset(case)
        assert case["expected_tools"]
        assert case["required_keywords"]
        assert case["max_steps"] >= 3
        assert case["label_origin"] == "rule_seeded"
        assert case["review_status"] == "pending"


def test_dataset_expected_tools_are_covered_by_stubs() -> None:
    """数据集中引用的工具必须存在于桩工具集合，否则 precision/recall 无意义。"""
    cases = load_cases(DEFAULT_DATASET)
    stub_names = {tool.name for tool in build_stub_tools(ToolRecorder())}
    for case in cases:
        assert set(case["expected_tools"]).issubset(stub_names), case["case_id"]


def test_dataset_categories_cover_runbook_topics() -> None:
    cases = load_cases(DEFAULT_DATASET)
    categories = Counter(case["category"] for case in cases)
    assert {"cpu", "memory", "disk", "network", "db", "k8s", "mq", "cert"}.issubset(
        categories
    )


def _patch_graph_dependencies(monkeypatch: pytest.MonkeyPatch, llm_factory) -> ToolRecorder:
    """参照 tests/test_aiops_planner.py 的手法：桩 MCP 工具 + 离线知识库 + 假 LLM。

    llm_factory 是零参工厂：节点每次 create_chat_model 都拿到新实例，
    与真实节点行为一致（真实 LLM 无状态，假 LLM 的轮次计数因此按节点隔离）。
    """
    recorder = ToolRecorder()
    mcp_client = FakeMCPClient(build_stub_tools(recorder))

    async def _fake_mcp_client():
        return mcp_client

    for module in NODE_MODULES:
        monkeypatch.setattr(module, "get_mcp_client_with_retry", _fake_mcp_client)
        monkeypatch.setattr(module, "retrieve_knowledge", fake_retrieve_knowledge)
    monkeypatch.setattr(
        NODE_MODULES[0].llm_factory,
        "create_chat_model",
        lambda **kwargs: llm_factory(),
    )
    return recorder


def _dry_run_case() -> dict:
    return {
        "case_id": "dry-001",
        "category": "cpu",
        "question": "告警：CPU 使用率过高，请诊断。",
        "expected_tools": ["prom_active_alerts"],
        "required_keywords": ["CPU", "证书"],
        "max_steps": 5,
        "label_origin": "rule_seeded",
        "review_status": "pending",
    }


async def test_run_case_dry_run_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _patch_graph_dependencies(monkeypatch, DryRunLLM)
    service = AIOpsService()

    result = await run_case(service.graph, _dry_run_case(), recorder, timeout=60)

    assert result["success"] is True
    assert result["error"] is None
    # dry-run 计划有 3 步，全部执行，且全程未触发 replan
    assert result["initial_plan_steps"] == len(DRY_RUN_PLAN)
    assert result["executed_steps"] == len(DRY_RUN_PLAN)
    assert result["replan_count"] == 0
    assert result["degraded"] is False
    assert result["steps_within_budget"] is True
    # 每个执行步骤固定调用一次 prom_active_alerts
    assert result["called_tools"] == ["prom_active_alerts"]
    assert result["tool_calls"] == len(DRY_RUN_PLAN)
    assert result["tool_selection_ok"] is True
    # dry-run 报告包含 "CPU" 与 "证书"，关键词覆盖 100%
    assert result["keyword_coverage"] == 1.0
    assert set(result["matched_keywords"]) == {"CPU", "证书"}
    assert result["latency_ms"] > 0


async def test_run_case_counts_replans(monkeypatch: pytest.MonkeyPatch) -> None:
    """replanner 返回新计划时应计入 replan_count，并拉低首计划命中率口径。"""
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda

    from app.agent.aiops.models import PlanStep
    from app.agent.aiops.planner import Plan
    from app.agent.aiops.replanner import Act, Response

    class _ReplanOnceLLM:
        """首个 replanner 决策返回 replan；决策计数通过共享状态跨实例累计。"""

        def __init__(self, state: dict) -> None:
            self._state = state
            self.round = 0

        def with_structured_output(self, schema, **_kwargs):
            if schema is Plan:
                # P0.2：链式依赖让首批只执行步骤A，replanner 评估时
                # 仍有剩余计划，replan 决策才会被触发（无依赖计划会被并行一次清空）
                return RunnableLambda(
                    lambda _input: Plan(
                        steps=[
                            PlanStep(description="步骤A"),
                            PlanStep(description="步骤B", depends_on=[1]),
                        ]
                    )
                )
            if schema is Act:

                def _decide(_input):
                    self._state["act_calls"] += 1
                    if self._state["act_calls"] == 1:
                        return Act(action="replan", new_steps=["调整后的步骤"])
                    return Act(action="continue")

                return RunnableLambda(_decide)
            if schema is Response:
                return RunnableLambda(lambda _input: Response(response=DRY_RUN_REPORT))
            raise ValueError(schema)

        def bind_tools(self, tools):  # noqa: ARG002
            return self

        async def ainvoke(self, messages):  # noqa: ARG002
            self.round += 1
            if self.round == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "prom_active_alerts",
                            "args": {},
                            "id": f"call-{id(self)}",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(content="步骤结果")

    state = {"act_calls": 0}
    recorder = _patch_graph_dependencies(monkeypatch, lambda: _ReplanOnceLLM(state))
    service = AIOpsService()

    result = await run_case(service.graph, _dry_run_case(), recorder, timeout=60)

    assert result["replan_count"] == 1
    # 步骤A + 调整后的步骤 = 2 次执行
    assert result["executed_steps"] == 2
    assert result["success"] is True


async def test_run_case_marks_failure_when_expected_tool_not_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _patch_graph_dependencies(monkeypatch, DryRunLLM)
    service = AIOpsService()
    case = _dry_run_case()
    case["expected_tools"] = ["k8s_get_logs"]  # dry-run 只会调用 prom_active_alerts

    result = await run_case(service.graph, case, recorder, timeout=60)

    assert result["tool_selection_ok"] is False
    assert result["success"] is False


def _result(**overrides) -> dict:
    base = {
        "case_id": "c-1",
        "category": "cpu",
        "success": True,
        "error": None,
        "expected_tools": ["prom_query"],
        "called_tools": ["prom_query"],
        "tool_details": {"prom_query": {"calls": 1}},
        "tool_calls": 1,
        "keyword_coverage": 1.0,
        "initial_plan_steps": 3,
        "executed_steps": 3,
        "max_steps": 5,
        "steps_within_budget": True,
        "replan_count": 0,
        "degraded": False,
        "latency_ms": 100.0,
        "llm_calls": 5,
        "input_tokens": 1000,
        "output_tokens": 200,
        "cost_usd": 0.001,
    }
    base.update(overrides)
    return base


def test_summarize_aggregates_all_metric_groups() -> None:
    results = [
        _result(),
        _result(
            case_id="c-2",
            category="k8s",
            success=False,
            called_tools=[],
            tool_details={},
            tool_calls=0,
            keyword_coverage=0.25,
            initial_plan_steps=4,
            executed_steps=8,
            steps_within_budget=False,
            replan_count=2,
            degraded=True,
            latency_ms=300.0,
            input_tokens=3000,
            output_tokens=600,
            cost_usd=0.003,
        ),
    ]

    report = summarize(results, dry_run=True, dataset_path=Path("cases.jsonl"))

    assert report["case_count"] == 2
    assert report["case_success_rate"] == 0.5
    # 规划质量
    assert report["average_initial_plan_steps"] == 3.5
    assert report["plan_steps_distribution"] == {3: 1, 4: 1}
    assert report["first_plan_hit_rate"] == 0.5
    assert report["replan_rate"] == 0.5
    assert report["average_executed_steps"] == 5.5
    assert report["steps_within_budget_rate"] == 0.5
    assert report["degraded_rate"] == 0.5
    # 工具选择：c-2 缺少 expected tool → recall 0.5，无 false positive → precision 1.0
    assert report["tool_selection"]["recall"] == 0.5
    assert report["tool_selection"]["precision"] == 1.0
    assert report["average_tool_calls"] == 0.5
    # 延迟与成本
    assert report["p50_latency_ms"] == 100.0
    assert report["p95_latency_ms"] == 300.0
    assert report["p99_latency_ms"] == 300.0
    assert report["average_input_tokens"] == 2000.0
    assert report["average_output_tokens"] == 400.0
    assert report["total_cost_usd"] == 0.004
    assert report["average_cost_usd"] == 0.002
    # 分类汇总
    categories = {item["category"]: item for item in report["category_summaries"]}
    assert categories["cpu"]["success_rate"] == 1.0
    assert categories["k8s"]["success_rate"] == 0.0


def test_dry_run_report_contains_smoke_disclaimer() -> None:
    from evaluation.aiops_eval import markdown_report

    report = summarize([_result()], dry_run=True, dataset_path=Path("cases.jsonl"))
    text = markdown_report(report)

    assert "dry-run" in text
    assert "未经人工复核" in text
    assert "rule_seeded" in text


def test_stub_tools_are_deterministic_and_record_calls() -> None:
    import asyncio

    recorder = ToolRecorder()
    tools = {tool.name: tool for tool in build_stub_tools(recorder)}

    first = asyncio.run(tools["query_cpu_metrics"].ainvoke({}))
    second = asyncio.run(tools["query_cpu_metrics"].ainvoke({}))

    assert first == second
    assert recorder.calls == ["query_cpu_metrics", "query_cpu_metrics"]
    assert "CPU" in first
    json.dumps(first)  # 桩输出必须是可序列化文本
