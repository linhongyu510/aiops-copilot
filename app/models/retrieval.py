"""Public, reasoning-safe models for the RAG 2.0 retrieval pipeline."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class QueryExpansion(BaseModel):
    original_query: str
    rewritten_query: str
    alternative_queries: list[str] = Field(default_factory=list)
    hypothetical_document: str = ""
    degraded: bool = False
    error: str | None = None


class RetrievalCandidate(BaseModel):
    chunk_id: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    branch_ranks: dict[str, int] = Field(default_factory=dict)
    raw_scores: dict[str, float] = Field(default_factory=dict)
    rrf_score: float = 0.0
    reranker_score: float | None = None
    final_rank: int | None = None


class RetrievalTrace(BaseModel):
    expansion: QueryExpansion
    branch_counts: dict[str, int] = Field(default_factory=dict)
    branch_rankings: dict[str, list[str]] = Field(default_factory=dict)
    stage_latencies_ms: dict[str, float] = Field(default_factory=dict)
    degradations: list[str] = Field(default_factory=list)
    rrf_candidates: int = 0
    rrf_ranking: list[str] = Field(default_factory=list)
    reranker_ranking: list[str] = Field(default_factory=list)
    final_candidates: int = 0


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("query 不能为空")
        return normalized


class RagSearchResponse(BaseModel):
    query: str
    candidates: list[RetrievalCandidate]
    trace: RetrievalTrace
