"""RAG 2.0: eight-way recall, application-level RRF and BGE reranking."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass

from loguru import logger

from app.config import config
from app.models.retrieval import (
    QueryExpansion,
    RetrievalCandidate,
    RetrievalTrace,
)
from app.observability import retrieval_metrics
from app.services.query_expansion_service import query_expansion_service
from app.services.reranker_service import bge_reranker_service
from app.services.retrieval_backend import get_retrieval_backend
from app.services.vector_embedding_service import vector_embedding_service


@dataclass(frozen=True)
class RetrievalOptions:
    rewrite: bool = True
    multi_query: bool = True
    hyde: bool = True
    bm25: bool = True
    rrf: bool = True
    reranker: bool = True


class HybridRetrievalService:
    def __init__(self) -> None:
        self.backend = get_retrieval_backend()

    @staticmethod
    def _embed_queries_sync(values: list[str]) -> list[list[float]]:
        batch_method = getattr(vector_embedding_service, "embed_queries", None)
        if batch_method is not None:
            return batch_method(values)
        return [vector_embedding_service.embed_query(value) for value in values]

    def _record_stage(
        self,
        trace: RetrievalTrace,
        stage: str,
        started: float,
    ) -> None:
        elapsed = (time.perf_counter() - started) * 1000
        trace.stage_latencies_ms[stage] = round(elapsed, 3)
        retrieval_metrics.record_stage(stage, elapsed)

    @staticmethod
    def reciprocal_rank_fusion(
        ranked_lists: Iterable[list[RetrievalCandidate]],
        rrf_k: int = 60,
        limit: int = 30,
    ) -> list[RetrievalCandidate]:
        fused: dict[str, RetrievalCandidate] = {}
        for ranked in ranked_lists:
            for rank, candidate in enumerate(ranked, start=1):
                if not candidate.chunk_id:
                    continue
                existing = fused.get(candidate.chunk_id)
                if existing is None:
                    existing = candidate.model_copy(deep=True)
                    existing.branch_ranks = {}
                    existing.raw_scores = {}
                    existing.rrf_score = 0.0
                    fused[candidate.chunk_id] = existing
                existing.rrf_score += 1.0 / (rrf_k + rank)
                existing.branch_ranks.update(candidate.branch_ranks)
                existing.raw_scores.update(candidate.raw_scores)
        return sorted(
            fused.values(),
            key=lambda item: (item.rrf_score, len(item.branch_ranks), item.chunk_id),
            reverse=True,
        )[:limit]

    @staticmethod
    def round_robin_fusion(
        ranked_lists: Iterable[list[RetrievalCandidate]],
        limit: int,
    ) -> list[RetrievalCandidate]:
        """A deterministic pre-RRF ablation baseline."""
        lists = list(ranked_lists)
        selected: list[RetrievalCandidate] = []
        seen: set[str] = set()
        cursor = 0
        while len(selected) < limit and any(cursor < len(values) for values in lists):
            for values in lists:
                if cursor >= len(values):
                    continue
                candidate = values[cursor]
                if candidate.chunk_id and candidate.chunk_id not in seen:
                    seen.add(candidate.chunk_id)
                    selected.append(candidate.model_copy(deep=True))
                    if len(selected) >= limit:
                        break
            cursor += 1
        return selected

    @staticmethod
    def _near_duplicate(left: str, right: str) -> bool:
        if left == right:
            return True
        if not left or not right:
            return False
        left_grams = {left[index : index + 3] for index in range(max(1, len(left) - 2))}
        right_grams = {right[index : index + 3] for index in range(max(1, len(right) - 2))}
        union = left_grams | right_grams
        return bool(union) and len(left_grams & right_grams) / len(union) >= 0.9

    @classmethod
    def diversify(
        cls,
        candidates: list[RetrievalCandidate],
        limit: int,
    ) -> list[RetrievalCandidate]:
        selected: list[RetrievalCandidate] = []
        source_counts: dict[str, int] = {}
        for candidate in candidates:
            source = str(
                candidate.metadata.get("_file_name")
                or candidate.metadata.get("source")
                or candidate.metadata.get("_source")
                or ""
            )
            if source_counts.get(source, 0) >= config.rag_max_chunks_per_source:
                continue
            if any(
                cls._near_duplicate(candidate.content, existing.content) for existing in selected
            ):
                continue
            candidate.final_rank = len(selected) + 1
            selected.append(candidate)
            source_counts[source] = source_counts.get(source, 0) + 1
            if len(selected) >= limit:
                break
        return selected

    def _record_branch(
        self,
        trace: RetrievalTrace,
        result: list[RetrievalCandidate],
    ) -> None:
        """Record per-branch recall counts and rankings for one branch result."""
        if not result:
            return
        branch = next(iter(result[0].branch_ranks))
        trace.branch_counts[branch] = len(result)
        trace.branch_rankings[branch] = [candidate.chunk_id for candidate in result]

    async def _dense_stage(
        self,
        expansion: QueryExpansion,
        options: RetrievalOptions,
    ) -> tuple[list[list[RetrievalCandidate]], list[str]]:
        """Run every dense branch concurrently.

        A single failing branch degrades only itself; a failure before any branch
        is dispatched (embedding backend down) degrades the whole dense stage and
        leaves BM25 evidence untouched.
        """
        if not self.backend.supports_dense:
            # 后端不支持稠密检索（如 local_wiki）：预期行为，不记录 degradation。
            return [], []

        expansion_available = not expansion.degraded
        dense_queries = [("dense_original", expansion.original_query)]
        if options.rewrite and expansion_available:
            dense_queries.append(("dense_rewrite", expansion.rewritten_query))
        if options.multi_query and expansion_available:
            dense_queries.extend(
                [
                    (f"dense_multi_{index + 1}", value)
                    for index, value in enumerate(expansion.alternative_queries)
                ]
            )

        ranked_lists: list[list[RetrievalCandidate]] = []
        degradations: list[str] = []
        try:
            vectors = await asyncio.to_thread(
                self._embed_queries_sync,
                [value for _, value in dense_queries],
            )
            dense_tasks = [
                asyncio.to_thread(
                    self.backend.dense_search,
                    vector,
                    config.rag_branch_top_k,
                    branch,
                )
                for (branch, _), vector in zip(dense_queries, vectors, strict=True)
            ]
            if options.hyde and expansion_available and expansion.hypothetical_document:
                hyde_vector = (
                    await asyncio.to_thread(
                        vector_embedding_service.embed_documents,
                        [expansion.hypothetical_document],
                    )
                )[0]
                dense_tasks.append(
                    asyncio.to_thread(
                        self.backend.dense_search,
                        hyde_vector,
                        config.rag_branch_top_k,
                        "dense_hyde",
                    )
                )
            dense_results = await asyncio.gather(*dense_tasks, return_exceptions=True)
            for result in dense_results:
                if isinstance(result, Exception):
                    degradations.append("dense_branch")
                    logger.warning("Dense 召回分支失败: {}", type(result).__name__)
                else:
                    ranked_lists.append(result)
        except Exception as exc:
            degradations.append("dense_all")
            logger.warning("Dense 召回整体失败: {}", type(exc).__name__)
        return ranked_lists, degradations

    async def _bm25_stage(
        self,
        expansion: QueryExpansion,
        options: RetrievalOptions,
    ) -> tuple[list[list[RetrievalCandidate]], list[str]]:
        """Run BM25 branches concurrently, keeping dense evidence on failure."""
        if not options.bm25:
            return [], []

        expansion_available = not expansion.degraded
        bm25_tasks = [
            asyncio.to_thread(
                self.backend.bm25_search,
                expansion.original_query,
                config.rag_branch_top_k,
                "bm25_original",
            )
        ]
        if (
            options.rewrite
            and expansion_available
            and expansion.rewritten_query != expansion.original_query
        ):
            bm25_tasks.append(
                asyncio.to_thread(
                    self.backend.bm25_search,
                    expansion.rewritten_query,
                    config.rag_branch_top_k,
                    "bm25_rewrite",
                )
            )

        ranked_lists: list[list[RetrievalCandidate]] = []
        degradations: list[str] = []
        bm25_results = await asyncio.gather(*bm25_tasks, return_exceptions=True)
        for result in bm25_results:
            if isinstance(result, Exception):
                if "bm25" not in degradations:
                    degradations.append("bm25")
                logger.warning("BM25 召回失败，保留 Dense 结果: {}", type(result).__name__)
            else:
                ranked_lists.append(result)
        return ranked_lists, degradations

    async def search(
        self,
        query: str,
        top_k: int | None = None,
        options: RetrievalOptions | None = None,
    ) -> tuple[list[RetrievalCandidate], RetrievalTrace]:
        options = options or RetrievalOptions(
            rewrite=config.rag_expansion_enabled,
            multi_query=config.rag_expansion_enabled,
            hyde=config.rag_expansion_enabled,
            bm25=config.rag_bm25_enabled,
            rrf=True,
            reranker=config.rag_reranker_enabled,
        )
        top_k = max(1, min(top_k or config.rag_top_k, 20))
        total_started = time.perf_counter()

        expansion_started = time.perf_counter()
        if options.rewrite or options.multi_query or options.hyde:
            expansion = await query_expansion_service.expand(query)
        else:
            expansion = QueryExpansion(
                original_query=" ".join(query.split()),
                rewritten_query=" ".join(query.split()),
            )
        trace = RetrievalTrace(expansion=expansion)
        self._record_stage(trace, "query_expansion", expansion_started)
        if expansion.degraded:
            trace.degradations.append("query_expansion")

        # Dense and BM25 depend only on the expansion result, never on each other,
        # so they run concurrently instead of one after the other. Results are
        # merged in a fixed dense-then-BM25 order to keep fusion deterministic.
        retrieval_started = time.perf_counter()
        (dense_lists, dense_degradations), (bm25_lists, bm25_degradations) = await asyncio.gather(
            self._dense_stage(expansion, options),
            self._bm25_stage(expansion, options),
        )
        retrieval_elapsed = (time.perf_counter() - retrieval_started) * 1000
        for stage in ("dense_retrieval", "bm25_retrieval"):
            trace.stage_latencies_ms[stage] = round(retrieval_elapsed, 3)
            retrieval_metrics.record_stage(stage, retrieval_elapsed)

        ranked_lists: list[list[RetrievalCandidate]] = []
        for result in dense_lists:
            ranked_lists.append(result)
            self._record_branch(trace, result)
        trace.degradations.extend(dense_degradations)
        for result in bm25_lists:
            ranked_lists.append(result)
            self._record_branch(trace, result)
        trace.degradations.extend(bm25_degradations)

        rrf_started = time.perf_counter()
        if options.rrf:
            fused = self.reciprocal_rank_fusion(
                ranked_lists,
                rrf_k=config.rag_rrf_k,
                limit=config.rag_rrf_top_k,
            )
        else:
            fused = self.round_robin_fusion(ranked_lists, config.rag_rrf_top_k)
        trace.rrf_candidates = len(fused)
        trace.rrf_ranking = [candidate.chunk_id for candidate in fused]
        self._record_stage(trace, "rrf", rrf_started)

        rerank_started = time.perf_counter()
        reranked = fused
        try:
            if options.reranker:
                reranked = await bge_reranker_service.rerank(query, fused)
        except Exception as exc:
            trace.degradations.append("reranker")
            if "out of memory" in str(exc).lower():
                trace.degradations.append("gpu_oom")
            logger.warning("BGE Reranker 失败，使用 RRF 排名: {}", type(exc).__name__)
        self._record_stage(trace, "reranker", rerank_started)
        trace.reranker_ranking = [candidate.chunk_id for candidate in reranked]

        thresholded = [
            candidate
            for candidate in reranked
            if not options.reranker
            or candidate.reranker_score is None
            or candidate.reranker_score >= config.rag_min_reranker_score
        ]
        final = self.diversify(thresholded, top_k)
        trace.final_candidates = len(final)
        self._record_stage(trace, "total", total_started)
        trace.degradations = list(dict.fromkeys(trace.degradations))
        retrieval_metrics.record_request(len(final), trace.degradations)
        return final, trace


hybrid_retrieval_service = HybridRetrievalService()
