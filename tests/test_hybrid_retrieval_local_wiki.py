"""hybrid_retrieval_service 接入 local_wiki 后端的集成测试。

通过 RetrievalOptions 关闭 query rewrite / multi_query / hyde / reranker，
避免触发 LLM 调用，仅保留 BM25 + RRF 的确定性路径。
"""

from __future__ import annotations

import pytest

from app.services.hybrid_retrieval_service import (
    RetrievalOptions,
    hybrid_retrieval_service,
)
from app.services.local_wiki import LocalWikiBackend


@pytest.fixture(scope="module", autouse=True)
def _local_wiki_backend() -> None:
    """确保 hybrid 服务绑定并初始化一个 local_wiki 后端。"""
    if not isinstance(hybrid_retrieval_service.backend, LocalWikiBackend):
        hybrid_retrieval_service.backend = LocalWikiBackend()
    hybrid_retrieval_service.backend.initialize()
    yield
    hybrid_retrieval_service.backend.close()


_OPTIONS = RetrievalOptions(
    rewrite=False,
    multi_query=False,
    hyde=False,
    bm25=True,
    rrf=True,
    reranker=False,
)


@pytest.mark.asyncio
async def test_search_returns_candidates() -> None:
    candidates, _ = await hybrid_retrieval_service.search(
        "CPU 使用率过高", top_k=3, options=_OPTIONS
    )
    assert isinstance(candidates, list)
    assert len(candidates) > 0


@pytest.mark.asyncio
async def test_search_cpu_high_usage_hit() -> None:
    candidates, _ = await hybrid_retrieval_service.search(
        "CPU 使用率过高", top_k=5, options=_OPTIONS
    )
    sources = [str(c.metadata.get("source", "")) for c in candidates]
    assert any("cpu_high_usage" in s for s in sources)


@pytest.mark.asyncio
async def test_search_trace_has_bm25_branch() -> None:
    _, trace = await hybrid_retrieval_service.search("CPU 使用率过高", top_k=3, options=_OPTIONS)
    assert any("bm25" in branch for branch in trace.branch_counts)


@pytest.mark.asyncio
async def test_search_no_dense_branch_when_local_wiki() -> None:
    _, trace = await hybrid_retrieval_service.search("CPU 使用率过高", top_k=3, options=_OPTIONS)
    assert not any(branch.startswith("dense_") for branch in trace.branch_counts)


@pytest.mark.asyncio
async def test_search_candidates_have_source() -> None:
    candidates, _ = await hybrid_retrieval_service.search(
        "CPU 使用率过高", top_k=3, options=_OPTIONS
    )
    assert candidates
    for candidate in candidates:
        assert "source" in candidate.metadata
        assert candidate.metadata["source"]
