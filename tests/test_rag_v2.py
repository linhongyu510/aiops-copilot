import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.tool_router import TOOL_METADATA, select_relevant_tools
from app.api.rag import search_rag
from app.config import config
from app.models.retrieval import (
    QueryExpansion,
    RagSearchRequest,
    RetrievalCandidate,
    RetrievalTrace,
)
from app.services.hybrid_retrieval_service import (
    HybridRetrievalService,
    RetrievalOptions,
)
from app.services.query_expansion_service import QueryExpansionService
from app.services.retrieval_backend import reset_retrieval_backend
from evaluation.generate_rag_v2_dataset import build_dataset
from evaluation.rag_generation_eval import deterministic_scores, parse_judge
from evaluation.rag_v2_eval import evaluate


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


def _candidate(chunk_id: str, branch: str, rank: int, source: str) -> RetrievalCandidate:
    return RetrievalCandidate(
        chunk_id=chunk_id,
        content=f"{source}-{chunk_id}",
        metadata={"_file_name": source},
        branch_ranks={branch: rank},
        raw_scores={branch: 1.0 / rank},
    )


def test_query_expansion_parses_fenced_json() -> None:
    payload = QueryExpansionService._parse_json(
        """```json
        {"rewritten_query":"Pod OOM 排查","alternative_queries":["内存限制","节点压力","泄漏"],
         "hypothetical_document":"这是用于检索的假设文档。"}
        ```"""
    )
    assert payload["rewritten_query"] == "Pod OOM 排查"


@pytest.mark.asyncio
async def test_query_expansion_failure_degrades_to_original_query(monkeypatch) -> None:
    class _BrokenModel:
        async def ainvoke(self, messages):
            raise TimeoutError("fixture timeout")

    service = QueryExpansionService()
    monkeypatch.setattr(config, "rag_expansion_enabled", True)
    monkeypatch.setattr(service, "_get_model", lambda: _BrokenModel())
    expansion = await service.expand("  Pod   OOM  ")
    assert expansion.degraded is True
    assert expansion.original_query == "Pod OOM"
    assert expansion.rewritten_query == "Pod OOM"
    assert expansion.alternative_queries == []
    assert expansion.hypothetical_document == ""


@pytest.mark.asyncio
async def test_query_expansion_cache_avoids_duplicate_llm_calls(monkeypatch) -> None:
    class _Model:
        calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "rewritten_query": "Kubernetes Pod OOMKilled 排查",
                        "alternative_queries": ["内存限制", "节点压力", "应用泄漏"],
                        "hypothetical_document": "这是一段用于检索的故障处置假设文档。",
                    },
                    ensure_ascii=False,
                )
            )

    model = _Model()
    service = QueryExpansionService()
    monkeypatch.setattr(config, "rag_expansion_enabled", True)
    monkeypatch.setattr(service, "_get_model", lambda: model)
    first = await service.expand("Pod OOM")
    second = await service.expand("Pod   OOM")
    assert model.calls == 1
    assert second == first


def test_rrf_is_rank_based_and_rewards_cross_branch_hits() -> None:
    first = [
        _candidate("shared", "dense", 1, "a.md"),
        _candidate("dense-only", "dense", 2, "b.md"),
    ]
    second = [
        _candidate("keyword-only", "bm25", 1, "c.md"),
        _candidate("shared", "bm25", 2, "a.md"),
    ]
    fused = HybridRetrievalService.reciprocal_rank_fusion([first, second], rrf_k=60, limit=3)
    assert fused[0].chunk_id == "shared"
    assert fused[0].branch_ranks == {"dense": 1, "bm25": 2}


def test_diversify_limits_sources_and_near_duplicates(monkeypatch) -> None:
    monkeypatch.setattr(config, "rag_max_chunks_per_source", 1)
    values = [
        _candidate("a1", "dense", 1, "a.md"),
        _candidate("a2", "dense", 2, "a.md"),
        _candidate("b1", "dense", 3, "b.md"),
    ]
    selected = HybridRetrievalService.diversify(values, 5)
    assert [item.chunk_id for item in selected] == ["a1", "b1"]


@pytest.mark.asyncio
async def test_hybrid_search_executes_all_eight_recall_branches(monkeypatch) -> None:
    monkeypatch.setattr(config, "retrieval_backend", "milvus")
    reset_retrieval_backend()
    service = HybridRetrievalService()
    expansion = QueryExpansion(
        original_query="原始问题",
        rewritten_query="改写问题",
        alternative_queries=["症状问题", "原因问题", "处置问题"],
        hypothetical_document="假设性故障处置文档",
    )

    async def fake_expand(query: str):
        return expansion

    dense_branches: list[str] = []
    bm25_branches: list[str] = []

    def fake_dense(vector, top_k, branch: str):
        dense_branches.append(branch)
        return [_candidate("shared", branch, 1, "shared.md")]

    def fake_bm25(query: str, top_k, branch: str):
        bm25_branches.append(branch)
        return [_candidate("shared", branch, 1, "shared.md")]

    async def fake_rerank(query: str, candidates):
        for candidate in candidates:
            candidate.reranker_score = 0.9
        return candidates

    monkeypatch.setattr(
        "app.services.hybrid_retrieval_service.query_expansion_service.expand",
        fake_expand,
    )
    monkeypatch.setattr(
        service,
        "_embed_queries_sync",
        lambda values: [[0.1] * 4 for _ in values],
    )
    monkeypatch.setattr(
        "app.services.hybrid_retrieval_service.vector_embedding_service.embed_documents",
        lambda values: [[0.2] * 4 for _ in values],
    )
    monkeypatch.setattr(service.backend, "dense_search", fake_dense)
    monkeypatch.setattr(service.backend, "bm25_search", fake_bm25)
    monkeypatch.setattr(
        "app.services.hybrid_retrieval_service.bge_reranker_service.rerank",
        fake_rerank,
    )

    candidates, trace = await service.search("原始问题")
    assert set(dense_branches) == {
        "dense_original",
        "dense_rewrite",
        "dense_multi_1",
        "dense_multi_2",
        "dense_multi_3",
        "dense_hyde",
    }
    assert set(bm25_branches) == {"bm25_original", "bm25_rewrite"}
    assert set(trace.branch_counts) == set(dense_branches + bm25_branches)
    assert set(trace.branch_rankings) == set(trace.branch_counts)
    assert trace.rrf_ranking == ["shared"]
    assert trace.reranker_ranking == ["shared"]
    assert len(candidates) == 1
    assert len(candidates[0].branch_ranks) == 8
    reset_retrieval_backend()


@pytest.mark.asyncio
async def test_expansion_failure_uses_only_original_dense_and_bm25(monkeypatch) -> None:
    monkeypatch.setattr(config, "retrieval_backend", "milvus")
    reset_retrieval_backend()
    service = HybridRetrievalService()
    expansion = QueryExpansion(
        original_query="原始问题",
        rewritten_query="原始问题",
        degraded=True,
        error="TimeoutError",
    )

    async def fake_expand(query: str):
        return expansion

    dense_branches: list[str] = []
    bm25_branches: list[str] = []

    def fake_dense(vector, top_k, branch: str):
        dense_branches.append(branch)
        return [_candidate("dense", branch, 1, "dense.md")]

    def fake_bm25(query: str, top_k, branch: str):
        bm25_branches.append(branch)
        return [_candidate("bm25", branch, 1, "bm25.md")]

    monkeypatch.setattr(
        "app.services.hybrid_retrieval_service.query_expansion_service.expand",
        fake_expand,
    )
    monkeypatch.setattr(service, "_embed_queries_sync", lambda values: [[0.1] * 4])
    monkeypatch.setattr(service.backend, "dense_search", fake_dense)
    monkeypatch.setattr(service.backend, "bm25_search", fake_bm25)
    candidates, trace = await service.search(
        "原始问题",
        options=RetrievalOptions(reranker=False),
    )
    assert dense_branches == ["dense_original"]
    assert bm25_branches == ["bm25_original"]
    assert "query_expansion" in trace.degradations
    assert len(candidates) == 2
    reset_retrieval_backend()


@pytest.mark.asyncio
async def test_bm25_and_reranker_failures_keep_dense_results(monkeypatch) -> None:
    monkeypatch.setattr(config, "retrieval_backend", "milvus")
    reset_retrieval_backend()
    service = HybridRetrievalService()

    def fake_dense(vector, top_k, branch: str):
        return [_candidate(branch, branch, 1, f"{branch}.md")]

    def broken_bm25(query: str, top_k, branch: str):
        raise ConnectionError("fixture bm25 unavailable")

    async def broken_rerank(query: str, candidates):
        raise RuntimeError("fixture out of memory")

    monkeypatch.setattr(
        service,
        "_embed_queries_sync",
        lambda values: [[0.1] * 4 for _ in values],
    )
    monkeypatch.setattr(service.backend, "dense_search", fake_dense)
    monkeypatch.setattr(service.backend, "bm25_search", broken_bm25)
    monkeypatch.setattr(
        "app.services.hybrid_retrieval_service.bge_reranker_service.rerank",
        broken_rerank,
    )
    candidates, trace = await service.search(
        "原始问题",
        options=RetrievalOptions(rewrite=False, multi_query=False, hyde=False),
    )
    assert [candidate.chunk_id for candidate in candidates] == ["dense_original"]
    assert {"bm25", "reranker", "gpu_oom"}.issubset(trace.degradations)
    reset_retrieval_backend()


@pytest.mark.asyncio
async def test_dense_failure_keeps_bm25_evidence(monkeypatch) -> None:
    monkeypatch.setattr(config, "retrieval_backend", "milvus")
    reset_retrieval_backend()
    service = HybridRetrievalService()

    def broken_embeddings(values):
        raise RuntimeError("fixture embedding unavailable")

    def fake_bm25(query: str, top_k, branch: str):
        return [_candidate("bm25-evidence", branch, 1, "runbook.md")]

    monkeypatch.setattr(service, "_embed_queries_sync", broken_embeddings)
    monkeypatch.setattr(service.backend, "bm25_search", fake_bm25)
    candidates, trace = await service.search(
        "原始问题",
        options=RetrievalOptions(
            rewrite=False,
            multi_query=False,
            hyde=False,
            reranker=False,
        ),
    )
    assert [candidate.chunk_id for candidate in candidates] == ["bm25-evidence"]
    assert "dense_all" in trace.degradations
    reset_retrieval_backend()


def test_tool_router_exposes_at_most_eight_relevant_tools() -> None:
    tools = [
        _Tool(name)
        for name in (
            "retrieve_knowledge",
            "get_current_time",
            "k8s_list_pods",
            "k8s_get_events",
            "k8s_get_logs",
            "k8s_describe_workload",
            "k8s_rollout_status",
            "web_search",
            "mysql_read_query",
            "query_cpu_metrics",
        )
    ]
    selected = select_relevant_tools("请检查 Kubernetes Pod 的事件和日志", tools)
    assert len(selected) <= 8
    assert {"k8s_list_pods", "k8s_get_events", "k8s_get_logs"}.issubset(
        {tool.name for tool in selected}
    )
    # 33 个厂商无关工具；WINDOS 的 6 个工具已下沉为可选集成，不计入核心目录。
    assert len(TOOL_METADATA) == 33
    assert all(item["read_only"] is True for item in TOOL_METADATA.values())


def test_v2_dataset_is_stable_grouped_and_contains_no_answer_cases() -> None:
    rows = build_dataset()
    assert len(rows) == 440
    assert len({row["id"] for row in rows}) == 440
    assert sum(not row["expected_sources"] for row in rows) == 40
    assert {row["split"] for row in rows} == {"train", "dev", "test"}
    by_doc: dict[str, set[str]] = {}
    for row in rows:
        assert "expected_evidence_spans" in row
        for doc_id in row["expected_doc_ids"]:
            by_doc.setdefault(doc_id, set()).add(row["split"])
    assert all(len(splits) == 1 for splits in by_doc.values())


def test_retrieval_metrics_include_no_answer_accuracy() -> None:
    rows = [
        {"id": "hit", "expected_sources": ["a.md"], "category": "x"},
        {"id": "negative", "expected_sources": [], "category": "out_of_scope"},
    ]
    metrics, errors = evaluate(rows, [["a.md"], []])
    assert metrics["recall_at_5"] == 1.0
    assert metrics["mrr_at_10"] == 1.0
    assert metrics["ndcg_at_10"] == 1.0
    assert metrics["no_answer_accuracy"] == 1.0
    assert errors == []


def test_generation_scores_validate_citations_and_grounding() -> None:
    row = {
        "query": "CPU 过高怎么排查",
        "key_facts": ["检查进程 CPU 使用率和慢查询"],
    }
    scores = deterministic_scores(
        row,
        "先检查进程 CPU 使用率和慢查询。[1]",
        ["检查进程 CPU 使用率和慢查询。"],
    )
    assert scores["citation_presence"] is True
    assert scores["citation_validity"] == 1.0
    assert scores["key_fact_coverage"] == 1.0
    assert parse_judge(
        '```json\n{"faithfulness": 0.9, "answer_relevance": 0.8}\n```'
    ) == {"faithfulness": 0.9, "answer_relevance": 0.8}


def test_corpus_manifest_tracks_all_documents() -> None:
    manifest = json.loads(
        Path("aiops-docs/CORPUS_MANIFEST.json").read_text(encoding="utf-8")
    )
    assert manifest["document_count"] == 40
    assert len(manifest["documents"]) == 40
    assert manifest["embedding"]["dimensions"] == 1024


@pytest.mark.asyncio
async def test_rag_search_api_returns_public_trace(monkeypatch) -> None:
    candidate = _candidate("chunk-1", "dense_original", 1, "runbook.md")
    candidate.final_rank = 1
    trace = RetrievalTrace(
        expansion=QueryExpansion(
            original_query="Pod OOM",
            rewritten_query="Kubernetes Pod OOMKilled 排查",
            alternative_queries=["容器内存限制排查"],
            hypothetical_document="Pod 因超过内存限制被终止。",
        ),
        branch_counts={"dense_original": 1},
        final_candidates=1,
    )

    async def fake_search(query: str, top_k: int):
        assert query == "Pod OOM"
        assert top_k == 5
        return [candidate], trace

    monkeypatch.setattr(
        "app.api.rag.hybrid_retrieval_service.search",
        fake_search,
    )
    response = await search_rag(RagSearchRequest(query="Pod OOM"))
    assert response.candidates[0].final_rank == 1
    assert response.trace.expansion.rewritten_query == "Kubernetes Pod OOMKilled 排查"
    assert not hasattr(response.trace, "reasoning")
