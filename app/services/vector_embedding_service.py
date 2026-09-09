"""可切换的向量嵌入服务，支持本地开源模型与 DashScope。"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Any

from langchain_core.embeddings import Embeddings
from loguru import logger
from openai import OpenAI

from app.config import config


class DashScopeEmbeddings(Embeddings):
    """DashScope Text Embedding（OpenAI 兼容接口）。"""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-v4",
        dimensions: int = 1024,
    ) -> None:
        if not api_key or api_key == "your-api-key-here":
            raise ValueError("请设置环境变量 DASHSCOPE_API_KEY")

        self.client = OpenAI(
            api_key=api_key,
            base_url=config.dashscope_api_base,
            timeout=30.0,
            max_retries=0,
        )
        self.model = model
        self.dimensions = dimensions
        logger.info("DashScope Embeddings 已配置: model={}, dim={}", model, dimensions)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self.client.embeddings.create(
                model=self.model,
                input=texts,
                dimensions=self.dimensions,
                encoding_format="float",
            )
            return [item.embedding for item in response.data]
        except Exception as exc:
            logger.error("DashScope 批量嵌入失败: {}", exc)
            raise RuntimeError(f"DashScope 批量嵌入失败: {exc}") from exc

    def embed_query(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise ValueError("查询文本不能为空")
        try:
            response = self.client.embeddings.create(
                model=self.model,
                input=text,
                dimensions=self.dimensions,
                encoding_format="float",
            )
            return response.data[0].embedding
        except Exception as exc:
            logger.error("DashScope 查询嵌入失败: {}", exc)
            raise RuntimeError(f"DashScope 查询嵌入失败: {exc}") from exc


class LocalSentenceTransformerEmbeddings(Embeddings):
    """基于 Sentence Transformers 的本地嵌入服务。

    模型采用懒加载，启动 Web 服务时不会立刻下载或占用显存。实际向量维度由
    EMBEDDING_DIMENSIONS 校验，并同步用于 Milvus schema。
    """

    def __init__(
        self,
        model_name: str,
        dimensions: int = 1024,
        device: str = "auto",
        batch_size: int = 8,
        cache_dir: str = "",
        source: str = "huggingface",
        modelscope_model_id: str = "",
        revision: str = "",
    ) -> None:
        self.model_name = model_name
        self.dimensions = dimensions
        self.requested_device = device
        self.batch_size = batch_size
        self.cache_dir = cache_dir or None
        self.source = source.strip().lower()
        self.modelscope_model_id = modelscope_model_id or model_name
        self.revision = revision or None
        self._model: Any | None = None
        self._device: str | None = None
        logger.info("本地 Embeddings 已配置: model={}, dim={}", model_name, dimensions)

    def _resolve_device(self) -> str:
        if self.requested_device != "auto":
            return self.requested_device
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model

        if config.huggingface_endpoint:
            os.environ.setdefault("HF_ENDPOINT", config.huggingface_endpoint)
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "本地 Embedding 依赖未安装，请执行: uv sync --extra local-embeddings"
            ) from exc

        model_path = self.model_name
        if self.source == "modelscope":
            try:
                from modelscope import snapshot_download
            except ImportError as exc:
                raise RuntimeError(
                    "ModelScope 依赖未安装，请执行: uv sync --extra local-embeddings"
                ) from exc
            model_path = snapshot_download(
                self.modelscope_model_id,
                cache_dir=self.cache_dir,
            )
        elif self.source != "huggingface":
            raise ValueError("LOCAL_EMBEDDING_SOURCE 仅支持 modelscope 或 huggingface")

        self._device = self._resolve_device()
        logger.info("正在加载本地 Embedding 模型 {}，device={}", model_path, self._device)
        self._model = SentenceTransformer(
            model_path,
            device=self._device,
            cache_folder=self.cache_dir,
            revision=self.revision,
        )
        actual_dimension = self._model.get_sentence_embedding_dimension()
        if actual_dimension != self.dimensions:
            self._model = None
            raise ValueError(
                f"Embedding 维度不匹配: model={actual_dimension}, config={self.dimensions}"
            )
        return self._model

    def _encode(self, values: list[str], *, query: bool) -> list[list[float]]:
        if not values:
            return []
        model = self._get_model()
        if query and config.embedding_query_instruction:
            prefix = config.embedding_query_instruction.strip()
            values = [
                value if value.startswith(prefix) else f"{prefix}{value}"
                for value in values
            ]
        # Use the same encoder explicitly: the query instruction above is part of
        # the versioned retrieval contract, so SentenceTransformer prompts must not
        # inject a second implicit prefix.
        vectors = model.encode(
            values,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vectors.tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts, query=False)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """批量编码查询，供离线评测复用同一模型实例。"""
        return self._encode(texts, query=True)

    def embed_query(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise ValueError("查询文本不能为空")
        return self._encode([text], query=True)[0]


class QueryEmbeddingCache(Embeddings):
    """Wrap an embedding backend with a bounded LRU cache for query vectors.

    Query embeddings are deterministic for a fixed model and instruction prefix,
    so repeated queries (a retried alert, the same runbook phrasing, an ablation
    rerun) can reuse the cached vector. Document embeddings are not cached: they
    are seen once per ingest and would only evict useful query entries.

    Vectors are copied on read and write so a caller mutating a returned list
    cannot corrupt the cached entry.
    """

    def __init__(self, inner: Embeddings, max_size: int) -> None:
        self._inner = inner
        self._max_size = max(0, max_size)
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._hits = 0
        self._misses = 0

    @property
    def inner(self) -> Embeddings:
        return self._inner

    @property
    def stats(self) -> dict[str, int]:
        return {"hits": self._hits, "misses": self._misses, "size": len(self._cache)}

    def _get(self, text: str) -> list[float] | None:
        if self._max_size == 0:
            return None
        cached = self._cache.get(text)
        if cached is None:
            return None
        self._cache.move_to_end(text)
        return list(cached)

    def _put(self, text: str, vector: list[float]) -> None:
        if self._max_size == 0:
            return
        self._cache[text] = list(vector)
        self._cache.move_to_end(text)
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        cached = self._get(text)
        if cached is not None:
            self._hits += 1
            return cached
        self._misses += 1
        vector = self._inner.embed_query(text)
        self._put(text, vector)
        return vector

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Batch query encoding that only sends cache misses to the backend."""
        if not texts:
            return []

        results: list[list[float] | None] = [None] * len(texts)
        pending: list[str] = []
        pending_positions: list[int] = []
        for index, text in enumerate(texts):
            cached = self._get(text)
            if cached is not None:
                self._hits += 1
                results[index] = cached
            else:
                self._misses += 1
                pending.append(text)
                pending_positions.append(index)

        if pending:
            batch_method = getattr(self._inner, "embed_queries", None)
            if batch_method is not None:
                vectors = batch_method(pending)
            else:
                vectors = [self._inner.embed_query(value) for value in pending]
            for position, text, vector in zip(
                pending_positions, pending, vectors, strict=True
            ):
                results[position] = vector
                self._put(text, vector)

        return [vector for vector in results if vector is not None]

    def __getattr__(self, item: str) -> Any:
        # Preserve backend-specific attributes (model_name, dimensions, ...).
        return getattr(self._inner, item)


def create_embedding_service() -> Embeddings:
    """按配置创建 Embedding 后端。"""
    provider = config.embedding_provider.strip().lower()
    if provider == "local":
        backend: Embeddings = LocalSentenceTransformerEmbeddings(
            model_name=config.local_embedding_model,
            dimensions=config.embedding_dimensions,
            device=config.local_embedding_device,
            batch_size=config.local_embedding_batch_size,
            cache_dir=config.local_embedding_cache_dir,
            source=config.local_embedding_source,
            modelscope_model_id=config.modelscope_embedding_model,
            revision=config.local_embedding_revision,
        )
    elif provider == "dashscope":
        backend = DashScopeEmbeddings(
            api_key=config.dashscope_api_key,
            model=config.dashscope_embedding_model,
            dimensions=config.embedding_dimensions,
        )
    else:
        raise ValueError("EMBEDDING_PROVIDER 仅支持 local 或 dashscope")

    if config.embedding_query_cache_size > 0:
        return QueryEmbeddingCache(backend, config.embedding_query_cache_size)
    return backend


vector_embedding_service = create_embedding_service()
