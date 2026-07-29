"""可切换的向量嵌入服务，支持本地开源模型与 DashScope。"""

from __future__ import annotations

import os
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
    ) -> None:
        self.model_name = model_name
        self.dimensions = dimensions
        self.requested_device = device
        self.batch_size = batch_size
        self.cache_dir = cache_dir or None
        self.source = source.strip().lower()
        self.modelscope_model_id = modelscope_model_id or model_name
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
        method_name = "encode_query" if query else "encode_document"
        encode = getattr(model, method_name, model.encode)
        vectors = encode(
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


def create_embedding_service() -> Embeddings:
    """按配置创建 Embedding 后端。"""
    provider = config.embedding_provider.strip().lower()
    if provider == "local":
        return LocalSentenceTransformerEmbeddings(
            model_name=config.local_embedding_model,
            dimensions=config.embedding_dimensions,
            device=config.local_embedding_device,
            batch_size=config.local_embedding_batch_size,
            cache_dir=config.local_embedding_cache_dir,
            source=config.local_embedding_source,
            modelscope_model_id=config.modelscope_embedding_model,
        )
    if provider == "dashscope":
        return DashScopeEmbeddings(
            api_key=config.dashscope_api_key,
            model=config.dashscope_embedding_model,
            dimensions=config.embedding_dimensions,
        )
    raise ValueError("EMBEDDING_PROVIDER 仅支持 local 或 dashscope")


vector_embedding_service = create_embedding_service()
