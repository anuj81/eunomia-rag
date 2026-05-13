"""HTTP routes for eunomia-rag."""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..config import get_settings
from ..embedding import get_encoder
from ..indexer.pipeline import run_indexing
from ..store import get_vector_store
from .auth import verify_token

logger = logging.getLogger(__name__)
router = APIRouter()

_DEFAULT_TOP_K = 8


# --------------------------------------------------------------------------- #
# Request / response schemas                                                  #
# --------------------------------------------------------------------------- #


class RetrieveRequest(BaseModel):
    query: str
    allowed_views: List[str]
    k: Optional[int] = None  # falls back to _DEFAULT_TOP_K


class RetrievedColumn(BaseModel):
    name: str
    description: str = ""


class RetrievedView(BaseModel):
    name: str
    domain: Optional[str] = None
    description: str = ""
    columns: List[RetrievedColumn] = Field(default_factory=list)
    owner_team: Optional[str] = None
    score: float


class RetrieveResponse(BaseModel):
    results: List[RetrievedView]


class RefreshRequest(BaseModel):
    reset: bool = False


class RefreshResponse(BaseModel):
    fetched: int
    indexed: int
    collection: str
    reset: bool


class HealthResponse(BaseModel):
    status: str
    qdrant: str
    collection: str


# --------------------------------------------------------------------------- #
# /v1/healthz                                                                 #
# --------------------------------------------------------------------------- #


@router.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    settings = get_settings()
    store = get_vector_store(settings)
    qdrant_ok = store.ping()
    return HealthResponse(
        status="ok",
        qdrant="reachable" if qdrant_ok else "unreachable",
        collection=settings.qdrant.collection,
    )


# --------------------------------------------------------------------------- #
# /v1/retrieve                                                                #
# --------------------------------------------------------------------------- #


@router.post(
    "/retrieve",
    response_model=RetrieveResponse,
    dependencies=[Depends(verify_token)],
)
def retrieve(req: RetrieveRequest) -> RetrieveResponse:
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="query must be non-empty")
    if not isinstance(req.allowed_views, list):
        raise HTTPException(status_code=400, detail="allowed_views must be a list")

    # An empty allow-list returns an empty response by design — the middleware
    # MAY pass [] when a user has no authorized views.
    if not req.allowed_views:
        logger.info(
            "retrieve: empty allowed_views — returning [] (query=%r)",
            req.query[:80],
        )
        return RetrieveResponse(results=[])

    k = req.k or _DEFAULT_TOP_K
    if k <= 0:
        raise HTTPException(status_code=400, detail="k must be > 0")

    settings = get_settings()
    encoder = get_encoder(settings)
    store = get_vector_store(settings)

    try:
        qv = encoder.encode([req.query])[0]
    except Exception:
        logger.exception("Embedding failed")
        raise HTTPException(status_code=500, detail="embedding failed")

    try:
        raw_hits = store.search(
            query_vector=qv,
            allowed_view_names=req.allowed_views,
            k=k,
        )
    except Exception:
        logger.exception("Qdrant search failed")
        raise HTTPException(status_code=502, detail="vector store unavailable")

    results = [
        RetrievedView(
            name=h["name"],
            domain=h.get("domain"),
            description=h.get("description") or "",
            columns=[
                RetrievedColumn(name=c.get("name") or "", description=c.get("description") or "")
                for c in (h.get("columns") or [])
            ],
            owner_team=h.get("owner_team"),
            score=h["score"],
        )
        for h in raw_hits
    ]
    logger.info(
        "retrieve: query=%r allowed=%d k=%d → %d hits",
        req.query[:80], len(req.allowed_views), k, len(results),
    )
    return RetrieveResponse(results=results)


# --------------------------------------------------------------------------- #
# /v1/index/refresh  (admin)                                                  #
# --------------------------------------------------------------------------- #


@router.post(
    "/index/refresh",
    response_model=RefreshResponse,
    dependencies=[Depends(verify_token)],
)
def refresh(req: Optional[RefreshRequest] = None) -> RefreshResponse:
    reset = bool(req and req.reset)
    logger.info("admin refresh triggered (reset=%s)", reset)
    try:
        result = run_indexing(reset=reset)
    except Exception:
        logger.exception("Index refresh failed")
        raise HTTPException(status_code=500, detail="refresh failed")
    return RefreshResponse(
        fetched=result.fetched,
        indexed=result.indexed,
        collection=result.collection,
        reset=result.reset,
    )
