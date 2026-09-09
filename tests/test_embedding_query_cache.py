"""Query embedding cache behaviour.

The cache sits on the retrieval hot path, so these tests pin the properties that
make it safe: identical queries must not re-enter the backend, batch calls must
only forward misses while preserving order, documents must never be cached, and a
caller mutating a returned vector must not corrupt the cached entry.
"""

from langchain_core.embeddings import Embeddings

from app.services.vector_embedding_service import QueryEmbeddingCache


class RecordingEmbeddings(Embeddings):
    """Backend that records every call and returns a deterministic vector."""

    def __init__(self) -> None:
        self.query_calls: list[list[str]] = []
        self.document_calls: list[list[str]] = []

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [float(len(text)), 0.5]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        return [self._vector(value) for value in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls.append([text])
        return self._vector(text)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self.query_calls.append(list(texts))
        return [self._vector(value) for value in texts]


def test_repeated_query_is_served_from_cache() -> None:
    backend = RecordingEmbeddings()
    cache = QueryEmbeddingCache(backend, max_size=8)

    first = cache.embed_query("磁盘使用率过高怎么排查")
    second = cache.embed_query("磁盘使用率过高怎么排查")

    assert first == second
    assert backend.query_calls == [["磁盘使用率过高怎么排查"]]
    assert cache.stats == {"hits": 1, "misses": 1, "size": 1}


def test_batch_only_forwards_cache_misses_and_keeps_order() -> None:
    backend = RecordingEmbeddings()
    cache = QueryEmbeddingCache(backend, max_size=8)
    cache.embed_query("aaa")
    backend.query_calls.clear()

    vectors = cache.embed_queries(["aaa", "bb", "cccc"])

    # Only the two misses reach the backend, but the caller still gets three
    # vectors in the requested order.
    assert backend.query_calls == [["bb", "cccc"]]
    assert vectors == [[3.0, 0.5], [2.0, 0.5], [4.0, 0.5]]


def test_documents_are_never_cached() -> None:
    backend = RecordingEmbeddings()
    cache = QueryEmbeddingCache(backend, max_size=8)

    cache.embed_documents(["runbook chunk"])
    cache.embed_documents(["runbook chunk"])

    assert backend.document_calls == [["runbook chunk"], ["runbook chunk"]]
    assert cache.stats["size"] == 0


def test_mutating_a_returned_vector_does_not_corrupt_the_cache() -> None:
    backend = RecordingEmbeddings()
    cache = QueryEmbeddingCache(backend, max_size=8)

    returned = cache.embed_query("disk")
    returned[0] = 999.0

    assert cache.embed_query("disk") == [4.0, 0.5]


def test_cache_evicts_least_recently_used_entries() -> None:
    backend = RecordingEmbeddings()
    cache = QueryEmbeddingCache(backend, max_size=2)

    cache.embed_query("one")
    cache.embed_query("two")
    cache.embed_query("one")  # refresh recency of "one"
    cache.embed_query("three")  # evicts "two"

    assert cache.stats["size"] == 2
    backend.query_calls.clear()
    cache.embed_query("two")
    assert backend.query_calls == [["two"]]


def test_zero_size_disables_caching() -> None:
    backend = RecordingEmbeddings()
    cache = QueryEmbeddingCache(backend, max_size=0)

    cache.embed_query("x")
    cache.embed_query("x")

    assert backend.query_calls == [["x"], ["x"]]
    assert cache.stats["size"] == 0


def test_backend_attributes_remain_reachable() -> None:
    backend = RecordingEmbeddings()
    backend.dimensions = 1024  # type: ignore[attr-defined]
    cache = QueryEmbeddingCache(backend, max_size=4)

    assert cache.dimensions == 1024
    assert cache.inner is backend
