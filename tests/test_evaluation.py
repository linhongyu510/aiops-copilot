from collections import Counter

from langchain_core.documents import Document

from evaluation.agent_eval import (
    call_event_delta,
    failure_reason,
    parameter_correctness,
    summarize_tools,
    tool_deltas,
)
from evaluation.generate_datasets import build_agent_dataset, build_rag_dataset
from evaluation.rag_baselines import BM25Index, diversify_ranked_sources
from evaluation.rag_eval import hashing_embedding


def test_generated_dataset_size_and_ids_are_stable() -> None:
    rows = build_rag_dataset()
    assert len(rows) == 600
    assert len({row["id"] for row in rows}) == 600
    assert all(row["expected_sources"] for row in rows)
    assert all(row["review_status"] == "pending" for row in rows)
    assert Counter(row["split"] for row in rows) == {"train": 360, "dev": 120, "test": 120}

    # 同一问题模板的 6 种改写必须位于同一集合，避免模板泄漏。
    grouped_splits: dict[str, set[str]] = {}
    for row in rows:
        template_id = "-".join(row["id"].split("-")[:3])
        grouped_splits.setdefault(template_id, set()).add(row["split"])
    assert all(len(splits) == 1 for splits in grouped_splits.values())


def test_agent_dataset_covers_all_categories() -> None:
    rows = build_agent_dataset()
    assert len(rows) == 42
    scenarios = [row for row in rows if row.get("fixture_id")]
    assert len(scenarios) == 12
    assert Counter(row["scenario"] for row in scenarios) == {
        "success": 4,
        "timeout": 4,
        "error": 4,
    }
    assert {row["expected_tools"][0] for row in scenarios} == {
        "search_log",
        "query_cpu_metrics",
        "mysql_read_query",
        "web_search",
    }


def test_agent_tool_report_uses_deterministic_fault_fixtures() -> None:
    results = []
    for tool in ("search_log", "query_cpu_metrics", "mysql_read_query", "web_search"):
        results.extend(
            [
                {
                    "expected_tools": [tool],
                    "tool_details": {tool: {"calls": 1, "successes": 1}},
                    "success": True,
                    "latency_ms": 10,
                },
                {
                    "expected_tools": [tool],
                    "tool_details": {tool: {"calls": 1, "successes": 0, "failures": 1}},
                    "success": False,
                    "scenario": "timeout",
                    "timed_out": True,
                    "latency_ms": 20,
                },
                {
                    "expected_tools": [tool],
                    "tool_details": {tool: {"calls": 1, "successes": 0, "failures": 1}},
                    "success": False,
                    "scenario": "error",
                    "failure_reason": "tool_error",
                    "latency_ms": 30,
                },
            ]
        )
    summaries = summarize_tools(results)
    assert len(summaries) == 4
    assert all(item["success_rate"] == 0.3333 for item in summaries)
    assert all(item["p50_latency_ms"] == 20 for item in summaries)
    assert all(item["p95_latency_ms"] == 30 for item in summaries)
    assert all(item["failure_reasons"] == {"timeout": 1, "tool_error": 1} for item in summaries)


def test_tool_delta_keeps_legacy_totals_and_adds_per_tool_detail() -> None:
    called, calls, successes, details = tool_deltas(
        {"tools": {"search_log": {"calls": 2, "successes": 1}}},
        {"tools": {"search_log": {"calls": 3, "successes": 1}}},
    )
    assert (called, calls, successes) == (["search_log"], 1, 0)
    assert details == {"search_log": {"calls": 1, "successes": 0, "failures": 1}}
    assert failure_reason({"success": False, "error": "read timed out"}) == "timeout"


def test_parameter_correctness_uses_value_free_call_schemas() -> None:
    events = call_event_delta(
        {"recent_calls": [{"sequence": 3}]},
        {
            "recent_calls": [
                {"sequence": 3, "tool": "old"},
                {
                    "sequence": 4,
                    "tool": "search_log",
                    "argument_schema": {
                        "topic_id": "str",
                        "start_time": "int",
                        "end_time": "int",
                    },
                },
            ]
        },
    )
    assert parameter_correctness(
        {
            "search_log": {
                "topic_id": "str",
                "start_time": "int",
                "end_time": "int",
            }
        },
        events,
    )
    assert all("arguments" not in event for event in events)


def test_hashing_embedding_is_normalized() -> None:
    vector = hashing_embedding("CPU 使用率过高")
    squared_norm = sum(item * item for item in vector)
    assert abs(squared_norm - 1.0) < 1e-9


def test_bm25_ranks_matching_document_first() -> None:
    index = BM25Index(["CPU 进程负载排查", "磁盘空间文件清理", "接口延迟日志"])
    assert index.search("CPU 负载", limit=3)[0] == 0


def test_source_diversification_preserves_rank_and_coverage() -> None:
    chunks = [
        Document(page_content="a1", metadata={"source": "a.md"}),
        Document(page_content="a2", metadata={"source": "a.md"}),
        Document(page_content="b1", metadata={"source": "b.md"}),
        Document(page_content="c1", metadata={"source": "c.md"}),
    ]
    assert diversify_ranked_sources([0, 1, 2, 3], chunks, 3) == [0, 2, 3]
