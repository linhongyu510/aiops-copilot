"""LLM 调用埋点：回调计数、token 提取、成本计算与端点输出。"""

import asyncio
from uuid import uuid4

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.api.metrics import get_llm_metrics, get_prometheus_metrics
from app.config import config
from app.core.llm_factory import llm_factory
from app.observability import llm_metrics
from app.observability.llm_metrics import (
    LLMMetricsCallback,
    LLMMetricsRegistry,
    _extract_usage,
    diff_snapshots,
)


def _result_with_usage_metadata(input_tokens: int, output_tokens: int) -> LLMResult:
    message = AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )
    return LLMResult(generations=[[ChatGeneration(message=message)]])


def test_registry_aggregates_calls_latency_and_tokens() -> None:
    registry = LLMMetricsRegistry()
    registry.record("deepseek-v4-flash", True, 100, input_tokens=10, output_tokens=5)
    registry.record("deepseek-v4-flash", False, 300)
    registry.record("qwen-max", True, 200, input_tokens=20, output_tokens=10)

    snapshot = registry.snapshot()

    assert snapshot["total_calls"] == 3
    assert snapshot["total_successes"] == 2
    assert snapshot["total_failures"] == 1
    assert snapshot["success_rate"] == 0.6667
    assert snapshot["p50_latency_ms"] == 200
    assert snapshot["total_input_tokens"] == 30
    assert snapshot["total_output_tokens"] == 15
    item = snapshot["models"]["deepseek-v4-flash"]
    assert item["calls"] == 2
    assert item["success_rate"] == 0.5
    assert item["p95_latency_ms"] == 300

    prometheus = registry.render_prometheus()
    assert (
        'aiops_llm_calls_total{model="deepseek-v4-flash",outcome="success"} 1' in prometheus
    )
    assert (
        'aiops_llm_calls_total{model="deepseek-v4-flash",outcome="failure"} 1' in prometheus
    )
    assert 'aiops_llm_tokens_total{model="qwen-max",type="input"} 20' in prometheus
    assert 'aiops_llm_tokens_total{model="qwen-max",type="output"} 10' in prometheus
    assert 'aiops_llm_cost_usd_total{model="qwen-max"} 0.0' in prometheus
    assert 'aiops_llm_latency_milliseconds{model="qwen-max",quantile="0.95"} 200' in prometheus


def test_extract_usage_prefers_usage_metadata() -> None:
    result = _result_with_usage_metadata(12, 7)
    assert _extract_usage(result) == (12, 7)


def test_diff_snapshots_returns_per_interval_totals() -> None:
    """评测脚本按 case 前后取 snapshot 差分，得到单次诊断的 token 与成本。"""
    registry = LLMMetricsRegistry()
    registry.record("deepseek-v4-flash", True, 100, input_tokens=10, output_tokens=5)
    before = registry.snapshot()

    registry.record(
        "deepseek-v4-flash", True, 200, input_tokens=20, output_tokens=8, cost_usd=0.001
    )
    registry.record("deepseek-v4-flash", False, 50)
    after = registry.snapshot()

    assert diff_snapshots(before, after) == {
        "calls": 2,
        "failures": 1,
        "input_tokens": 20,
        "output_tokens": 8,
        "cost_usd": 0.001,
    }
    # 空区间差分全为 0
    assert diff_snapshots(after, after) == {
        "calls": 0,
        "failures": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }


def test_extract_usage_falls_back_to_llm_output_token_usage() -> None:
    result = LLMResult(
        generations=[[ChatGeneration(message=AIMessage(content="ok"))]],
        llm_output={"token_usage": {"prompt_tokens": 3, "completion_tokens": 4}},
    )
    assert _extract_usage(result) == (3, 4)


def test_extract_usage_tolerates_missing_fields() -> None:
    assert _extract_usage(LLMResult(generations=[[]])) == (0, 0)
    assert _extract_usage(LLMResult(generations=[[]], llm_output=None)) == (0, 0)
    assert _extract_usage(LLMResult(generations=[[]], llm_output={"token_usage": None})) == (
        0,
        0,
    )


def test_callback_records_success_failure_and_cost(monkeypatch) -> None:
    monkeypatch.setattr(config, "llm_price_input_per_million", 2.0)
    monkeypatch.setattr(config, "llm_price_output_per_million", 8.0)
    registry = LLMMetricsRegistry()
    handler = LLMMetricsCallback("deepseek-v4-flash", registry=registry)

    run_id = uuid4()
    handler.on_chat_model_start({}, [[AIMessage(content="hi")]], run_id=run_id)
    handler.on_llm_end(_result_with_usage_metadata(10, 5), run_id=run_id)

    failed_run = uuid4()
    handler.on_chat_model_start({}, [[AIMessage(content="hi")]], run_id=failed_run)
    handler.on_llm_error(RuntimeError("boom"), run_id=failed_run)

    snapshot = registry.snapshot()
    item = snapshot["models"]["deepseek-v4-flash"]
    assert item["calls"] == 2
    assert item["successes"] == 1
    assert item["failures"] == 1
    assert item["input_tokens"] == 10
    assert item["output_tokens"] == 5
    # (10 * 2.0 + 5 * 8.0) / 1_000_000
    assert item["cost_usd"] == 0.00006
    assert snapshot["total_cost_usd"] == 0.00006


def test_callback_counts_calls_via_fake_chat_model() -> None:
    """模型级 callbacks 会被 invoke 及 bind 派生的 runnable 继承。"""
    registry = LLMMetricsRegistry()
    handler = LLMMetricsCallback("fake-model", registry=registry)
    model = GenericFakeChatModel(
        messages=iter([AIMessage(content="hello"), AIMessage(content="world")]),
        callbacks=[handler],
    )

    model.invoke("hi")
    model.bind(stop=["x"]).invoke("hi")

    snapshot = registry.snapshot()
    assert snapshot["total_calls"] == 2
    assert snapshot["total_successes"] == 2
    assert snapshot["models"]["fake-model"]["average_latency_ms"] >= 0.0


def test_llm_factory_attaches_metrics_callback() -> None:
    llm = llm_factory.create_chat_model(model=config.rag_model, streaming=False)
    handlers = [h for h in (llm.callbacks or []) if isinstance(h, LLMMetricsCallback)]
    assert len(handlers) == 1
    assert handlers[0]._model_name == config.rag_model


def test_metrics_endpoints_expose_llm_metrics() -> None:
    llm_metrics.reset()
    llm_metrics.record(
        "deepseek-v4-flash", True, 120, input_tokens=10, output_tokens=5, cost_usd=0.001
    )
    detail = asyncio.run(get_llm_metrics())
    assert detail["total_calls"] == 1
    assert detail["models"]["deepseek-v4-flash"]["cost_usd"] == 0.001

    prometheus = asyncio.run(get_prometheus_metrics())
    assert 'aiops_llm_calls_total{model="deepseek-v4-flash",outcome="success"} 1' in prometheus
    assert 'aiops_llm_tokens_total{model="deepseek-v4-flash",type="input"} 10' in prometheus
    assert 'aiops_llm_cost_usd_total{model="deepseek-v4-flash"} 0.001' in prometheus
    llm_metrics.reset()
