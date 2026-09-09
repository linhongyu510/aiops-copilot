from types import SimpleNamespace

import pytest

import app.services.vector_embedding_service as module
from app.config import config
from app.services.vector_embedding_service import (
    DashScopeEmbeddings,
    LocalSentenceTransformerEmbeddings,
    QueryEmbeddingCache,
    create_embedding_service,
)


class FakeEmbeddingsApi:
    def create(self, **kwargs):
        values = kwargs["input"]
        if values == "bad":
            raise TimeoutError("remote timeout")
        count = len(values) if isinstance(values, list) else 1
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[float(index), 1.0]) for index in range(count)]
        )


class FakeOpenAI:
    def __init__(self, **_kwargs):
        self.embeddings = FakeEmbeddingsApi()


def test_dashscope_embedding_validation_and_calls(monkeypatch) -> None:
    monkeypatch.setattr(module, "OpenAI", FakeOpenAI)
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        DashScopeEmbeddings("")
    service = DashScopeEmbeddings("fixture", dimensions=2)
    assert service.embed_documents([]) == []
    assert service.embed_documents(["a", "b"]) == [[0.0, 1.0], [1.0, 1.0]]
    assert service.embed_query("query") == [0.0, 1.0]
    with pytest.raises(ValueError, match="不能为空"):
        service.embed_query(" ")
    with pytest.raises(RuntimeError, match="查询嵌入失败"):
        service.embed_query("bad")


def test_local_embedding_encode_and_validation() -> None:
    class Matrix:
        def tolist(self):
            return [[1.0, 0.0], [0.0, 1.0]]

    class Model:
        def encode(self, values, **kwargs):
            assert kwargs["normalize_embeddings"] is True
            return Matrix()

    service = LocalSentenceTransformerEmbeddings(
        "fixture", dimensions=2, device="cpu", batch_size=2
    )
    service._model = Model()
    assert service.embed_documents([]) == []
    assert service.embed_documents(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert service.embed_queries(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    with pytest.raises(ValueError, match="不能为空"):
        service.embed_query("")


def test_embedding_factory_routes_provider(monkeypatch) -> None:
    # The factory wraps the selected backend in the query cache, so routing is
    # asserted on the backend behind the wrapper.
    monkeypatch.setattr(config, "embedding_query_cache_size", 128)
    monkeypatch.setattr(config, "embedding_provider", "local")
    local_service = create_embedding_service()
    assert isinstance(local_service, QueryEmbeddingCache)
    assert isinstance(local_service.inner, LocalSentenceTransformerEmbeddings)

    monkeypatch.setattr(config, "embedding_provider", "dashscope")
    monkeypatch.setattr(config, "dashscope_api_key", "fixture")
    monkeypatch.setattr(module, "OpenAI", FakeOpenAI)
    dashscope_service = create_embedding_service()
    assert isinstance(dashscope_service, QueryEmbeddingCache)
    assert isinstance(dashscope_service.inner, DashScopeEmbeddings)

    monkeypatch.setattr(config, "embedding_provider", "unknown")
    with pytest.raises(ValueError, match="EMBEDDING_PROVIDER"):
        create_embedding_service()


def test_embedding_factory_returns_bare_backend_when_cache_disabled(monkeypatch) -> None:
    monkeypatch.setattr(config, "embedding_query_cache_size", 0)
    monkeypatch.setattr(config, "embedding_provider", "local")
    assert isinstance(create_embedding_service(), LocalSentenceTransformerEmbeddings)
