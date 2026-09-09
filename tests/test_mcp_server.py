"""``aiops_core.mcp_server`` 决策工具的最小回归。

目标：不真的起 stdio 子进程，只跑 impl 函数——这样测试可以离线、稳定、幂等。
"""

from __future__ import annotations

import pytest

from aiops_core.mcp_server import (
    _alert_fingerprint_impl,
    _render_report_impl,
    _runbook_lookup_impl,
    _skill_match_impl,
    _skill_plan_impl,
    build_server,
)

ALERT_SAMPLE = {
    "alerts": [
        {
            "labels": {
                "alertname": "KafkaConsumerLagHigh",
                "service": "checkout",
                "consumergroup": "checkout-order",
                "topic": "orders",
            },
            "annotations": {
                "summary": "Kafka 消费者滞后过高",
                "description": "checkout-order 消费速度落后 orders",
            },
            "startsAt": "2026-08-23T12:00:00Z",
        }
    ]
}


def test_skill_match_returns_top_k_and_reasons() -> None:
    result = _skill_match_impl(ALERT_SAMPLE, top_k=3)
    assert set(result.keys()) == {"query", "labels", "matches"}
    assert isinstance(result["matches"], list)
    assert result["matches"], "预期至少命中一个 Skill"
    first = result["matches"][0]
    for key in (
        "skill_id",
        "title",
        "domain",
        "score",
        "reasons",
        "runbook_refs",
        "required_tools",
        "disabled_reason",
    ):
        assert key in first
    assert isinstance(first["runbook_refs"], list)


def test_skill_plan_missing_returns_error_payload() -> None:
    plan = _skill_plan_impl("does.not.exist")
    assert plan["skill_id"] == "does.not.exist"
    assert plan["steps"] == []
    assert "error" in plan


def test_skill_plan_returns_steps_for_known_skill() -> None:
    match = _skill_match_impl(ALERT_SAMPLE, top_k=1)
    skill_id = match["matches"][0]["skill_id"]
    plan = _skill_plan_impl(skill_id, context={"labels": match["labels"]})
    assert plan["skill_id"] == skill_id
    assert isinstance(plan["steps"], list) and plan["steps"]
    assert "verifications" in plan


def test_runbook_lookup_empty_inputs_return_empty() -> None:
    assert _runbook_lookup_impl([], "kafka") == []
    assert _runbook_lookup_impl(["kafka_consumer_lag"], "") == []


def test_runbook_lookup_hits_known_wiki() -> None:
    hits = _runbook_lookup_impl(
        doc_ids=["kafka_consumer_lag"],
        query="Kafka 消费者滞后",
        top_k=2,
    )
    assert hits, "预期命中 aiops-docs/kafka_consumer_lag.md 至少一段"
    for h in hits:
        assert h["doc_id"] == "kafka_consumer_lag"
        assert h["section_title"]
        assert h["content"]


def test_alert_fingerprint_tool_returns_stable_prefix() -> None:
    a = _alert_fingerprint_impl(ALERT_SAMPLE)
    b = _alert_fingerprint_impl(ALERT_SAMPLE)
    assert a["fingerprint"] == b["fingerprint"], "同一告警指纹必须稳定"
    assert a["normalized"]["labels"]["alertname"] == "KafkaConsumerLagHigh"


def test_render_report_covers_all_sections() -> None:
    report = _render_report_impl(
        skill_match={"skill_id": "demo", "title": "Demo", "match_score": 0.9},
        plan=[{"step_id": "s1", "description": "测试", "tool_hint": "echo"}],
        evidence_by_step={"s1": {"summary": "ok", "status": "success"}},
        verifications=[
            {"tool": "prom_query", "success_expr": "result.value < 1", "description": "延迟"}
        ],
        trigger={"title": "x", "labels": {"alertname": "x"}, "fingerprint": "fp"},
        runbook_hits=[
            {"doc_id": "kafka_consumer_lag", "section_title": "止损与恢复", "content": "hello"}
        ],
    )
    for anchor in ("# 诊断报告", "## 触发", "## 匹配到的 Skill", "## 执行计划", "## 证据摘要", "## 验收清单", "## Runbook 参考"):
        assert anchor in report


def test_build_server_registers_five_tools() -> None:
    fastmcp = pytest.importorskip("fastmcp")
    _ = fastmcp
    server = build_server()
    manager = getattr(server, "_tool_manager", None)
    tools = None
    if manager is not None and hasattr(manager, "_tools"):
        tools = list(manager._tools.keys())  # type: ignore[attr-defined]
    if tools is None and hasattr(server, "tools"):
        tools = list(getattr(server, "tools", {}).keys())
    if tools is None:
        pytest.skip("fastmcp 版本未暴露工具枚举接口")
    expected = {
        "skill_match",
        "skill_plan",
        "runbook_lookup",
        "alert_fingerprint_tool",
        "render_report",
    }
    assert expected.issubset(set(tools))
