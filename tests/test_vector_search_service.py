from types import SimpleNamespace

import pytest

import app.services.vector_search_service as module
from app.services.vector_search_service import SearchResult, VectorSearchService


def test_search_result_to_dict() -> None:
    result = SearchResult("id", "content", 0.2, {"source": "runbook"})
    assert result.to_dict() == {
        "id": "id",
        "content": "content",
        "score": 0.2,
        "metadata": {"source": "runbook"},
    }


def test_vector_search_parses_milvus_hits(monkeypatch) -> None:
    hit = SimpleNamespace(
        entity={"id": "doc-1", "content": "evidence", "metadata": {"source": "a"}},
        distance=0.12,
    )

    class Collection:
        def search(self, **kwargs):
            assert kwargs["limit"] == 2
            assert kwargs["anns_field"] == "vector"
            return [[hit]]

    monkeypatch.setattr(
        module.vector_embedding_service, "embed_query", lambda _query: [0.1, 0.2]
    )
    monkeypatch.setattr(module.milvus_manager, "get_collection", lambda: Collection())
    results = VectorSearchService().search_similar_documents("cpu", top_k=2)
    assert [result.to_dict() for result in results] == [
        {
            "id": "doc-1",
            "content": "evidence",
            "score": 0.12,
            "metadata": {"source": "a"},
        }
    ]


def test_vector_search_wraps_dependency_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        module.vector_embedding_service,
        "embed_query",
        lambda _query: (_ for _ in ()).throw(ConnectionError("embedding down")),
    )
    with pytest.raises(RuntimeError, match="搜索失败"):
        VectorSearchService().search_similar_documents("cpu")
