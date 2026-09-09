"""Lazy local BGE cross-encoder reranker with bounded GPU concurrency."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from app.config import config
from app.models.retrieval import RetrievalCandidate


class BgeRerankerService:
    def __init__(self) -> None:
        self._model: Any | None = None
        self._tokenizer: Any | None = None
        self._device = "cpu"
        self._semaphore = asyncio.Semaphore(max(1, config.rag_model_max_concurrency))

    def _load(self) -> tuple[Any, Any]:
        if self._model is not None and self._tokenizer is not None:
            return self._tokenizer, self._model
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "Reranker 依赖未安装，请执行: uv sync --extra local-embeddings"
            ) from exc
        requested = config.rag_reranker_device.strip().lower()
        self._device = (
            "cuda"
            if requested == "auto" and torch.cuda.is_available()
            else ("cpu" if requested == "auto" else requested)
        )
        self._tokenizer = AutoTokenizer.from_pretrained(
            config.rag_reranker_model,
            cache_dir=config.rag_reranker_cache_dir or None,
            revision=config.rag_reranker_revision,
            trust_remote_code=False,
        )
        self._model = AutoModelForSequenceClassification.from_pretrained(
            config.rag_reranker_model,
            cache_dir=config.rag_reranker_cache_dir or None,
            revision=config.rag_reranker_revision,
            trust_remote_code=False,
        )
        self._model.to(self._device)
        if (
            self._device.startswith("cuda")
            and config.rag_reranker_use_fp16
        ):
            self._model.half()
        self._model.eval()
        logger.info(
            "BGE Reranker 已加载: model={}, device={}",
            config.rag_reranker_model,
            self._device,
        )
        return self._tokenizer, self._model

    def _score_sync(self, query: str, passages: list[str]) -> list[float]:
        import torch

        tokenizer, model = self._load()
        scores: list[float] = []
        batch_size = max(1, config.rag_reranker_batch_size)
        for start in range(0, len(passages), batch_size):
            pairs = [[query, passage] for passage in passages[start : start + batch_size]]
            inputs = tokenizer(
                pairs,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            inputs = {key: value.to(self._device) for key, value in inputs.items()}
            with torch.inference_mode():
                logits = model(**inputs).logits.view(-1).float()
                scores.extend(torch.sigmoid(logits).cpu().tolist())
        return scores

    async def rerank(
        self,
        query: str,
        candidates: list[RetrievalCandidate],
    ) -> list[RetrievalCandidate]:
        if not candidates or not config.rag_reranker_enabled:
            return candidates
        async with self._semaphore:
            scores = await asyncio.to_thread(
                self._score_sync,
                query,
                [candidate.content for candidate in candidates],
            )
        for candidate, score in zip(candidates, scores, strict=True):
            candidate.reranker_score = float(score)
        return sorted(
            candidates,
            key=lambda item: (
                item.reranker_score if item.reranker_score is not None else -1.0,
                item.rrf_score,
            ),
            reverse=True,
        )


bge_reranker_service = BgeRerankerService()
