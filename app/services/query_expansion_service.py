"""Single-call Query Rewrite, Multi-Query and HyDE generation."""

from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from app.config import config
from app.core.llm_factory import llm_factory
from app.models.retrieval import QueryExpansion
from app.observability import retrieval_metrics


class QueryExpansionService:
    def __init__(self) -> None:
        self._model: Any | None = None
        self._semaphore = asyncio.Semaphore(max(1, config.rag_model_max_concurrency))
        self._cache: OrderedDict[str, QueryExpansion] = OrderedDict()

    def _get_model(self) -> Any:
        if self._model is None:
            model = (
                config.rag_expansion_model
                if config.llm_provider.strip().lower() == "deepseek"
                else config.rag_model
            )
            self._model = llm_factory.create_chat_model(
                model=model,
                temperature=0.2,
                streaming=False,
                max_tokens=900,
            )
        return self._model

    @staticmethod
    def fallback(query: str, error: Exception | None = None) -> QueryExpansion:
        return QueryExpansion(
            original_query=query,
            rewritten_query=query,
            alternative_queries=[],
            hypothetical_document="",
            degraded=True,
            error=type(error).__name__ if error is not None else None,
        )

    async def expand(self, query: str) -> QueryExpansion:
        normalized = " ".join(query.split())
        if not config.rag_expansion_enabled:
            return self.fallback(normalized)
        cached = self._cache.get(normalized)
        if cached is not None:
            self._cache.move_to_end(normalized)
            retrieval_metrics.record_cache(True)
            return cached.model_copy(deep=True)
        retrieval_metrics.record_cache(False)
        try:
            async with self._semaphore:
                response = await self._get_model().ainvoke(
                    [
                        SystemMessage(
                            content=(
                                "你是中文 AIOps 检索查询扩展器。只输出一个 JSON 对象，不输出解释。"
                                "不要虚构主机名、IP、账号或真实故障结论。"
                            )
                        ),
                        HumanMessage(
                            content=(
                                f"原始问题：{normalized}\n"
                                "返回字段：rewritten_query（消除指代并保留所有技术实体）、"
                                f"alternative_queries（恰好 {config.rag_multi_query_count} 条，"
                                "分别从症状、原因、处置角度检索）、"
                                "hypothetical_document（100-180字的假设性 Runbook 片段，"
                                "包含可能出现的专业术语，但明确作为检索文本而非事实）。"
                            )
                        ),
                    ]
                )
            payload = self._parse_json(response.content)
            rewritten = self._clean(payload.get("rewritten_query")) or normalized
            alternatives = self._normalize_alternatives(
                payload.get("alternative_queries"),
                excluded={normalized, rewritten},
            )
            hypothetical = self._clean(payload.get("hypothetical_document"))
            if not hypothetical:
                raise ValueError("HyDE 文档为空")
            expansion = QueryExpansion(
                original_query=normalized,
                rewritten_query=rewritten,
                alternative_queries=alternatives,
                hypothetical_document=hypothetical,
            )
            self._cache[normalized] = expansion.model_copy(deep=True)
            self._cache.move_to_end(normalized)
            while len(self._cache) > max(1, config.rag_expansion_cache_size):
                self._cache.popitem(last=False)
            return expansion
        except Exception as exc:
            logger.warning("Query 扩展失败，降级为原始查询: {}", type(exc).__name__)
            return self.fallback(normalized, exc)

    @staticmethod
    def _clean(value: Any) -> str:
        return " ".join(str(value or "").split()).strip()

    def _normalize_alternatives(self, values: Any, excluded: set[str]) -> list[str]:
        if not isinstance(values, list):
            raise ValueError("alternative_queries 必须是数组")
        result: list[str] = []
        seen = {item.casefold() for item in excluded}
        for value in values:
            cleaned = self._clean(value)
            if cleaned and cleaned.casefold() not in seen:
                seen.add(cleaned.casefold())
                result.append(cleaned)
        if len(result) < config.rag_multi_query_count:
            raise ValueError("Multi-Query 数量不足")
        return result[: config.rag_multi_query_count]

    @staticmethod
    def _parse_json(content: Any) -> dict[str, Any]:
        if isinstance(content, dict):
            return content
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        text = str(content).strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        else:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("扩展结果不是 JSON 对象")
        return parsed


query_expansion_service = QueryExpansionService()
