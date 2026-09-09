"""Reasoning-safe RAG retrieval inspector API."""

from fastapi import APIRouter, HTTPException

from app.models.retrieval import RagSearchRequest, RagSearchResponse
from app.services.hybrid_retrieval_service import hybrid_retrieval_service

router = APIRouter()


@router.post("/rag/search", response_model=RagSearchResponse)
async def search_rag(request: RagSearchRequest) -> RagSearchResponse:
    """Return public retrieval evidence and stage scores, never hidden reasoning."""
    try:
        candidates, trace = await hybrid_retrieval_service.search(
            request.query,
            request.top_k,
        )
        return RagSearchResponse(
            query=request.query,
            candidates=candidates,
            trace=trace,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"rag_search_unavailable:{type(exc).__name__}",
        ) from exc
