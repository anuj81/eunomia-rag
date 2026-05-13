"""Qdrant wrapper.

Thin abstraction so the indexer and the /v1/retrieve route don't need to
touch the raw Qdrant SDK details. Stores per-view documents with payload
shape:

    {
        "name":        "finance_daily_revenue_view",
        "domain":      "Finance",
        "description": "Aggregated daily revenue.",
        "columns": [
            {"name": "day",            "description": "..."},
            {"name": "gross_revenue",  "description": "..."},
            ...
        ],
        "owner_team":  "Finance"
    }
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from functools import lru_cache
from typing import Any, Dict, List, Optional

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)


def _stable_point_id(view_name: str) -> str:
    """Deterministic UUID for a view name — lets us upsert idempotently."""
    digest = hashlib.sha1(view_name.encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16]))


class VectorStore:
    """Wrap qdrant-client with the operations we care about."""

    def __init__(self, settings: Settings):
        self._url = settings.qdrant.url
        self._collection = settings.qdrant.collection
        self._vector_size = settings.qdrant.vector_size
        self._distance_name = settings.qdrant.distance
        self._client = None  # lazy

    # ---------------------------------------------------------------- client #

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        # Lazy import so module load doesn't pay for qdrant-client startup.
        from qdrant_client import QdrantClient

        self._client = QdrantClient(url=self._url, timeout=30.0)
        return self._client

    def ping(self) -> bool:
        try:
            self._ensure_client().get_collections()
            return True
        except Exception:
            logger.exception("Qdrant unreachable at %s", self._url)
            return False

    # ----------------------------------------------------------- collections #

    def ensure_collection(self) -> None:
        from qdrant_client import models as qm

        client = self._ensure_client()
        existing = {c.name for c in client.get_collections().collections}
        if self._collection in existing:
            return
        distance_map = {
            "Cosine": qm.Distance.COSINE,
            "Dot":    qm.Distance.DOT,
            "Euclid": qm.Distance.EUCLID,
        }
        client.create_collection(
            collection_name=self._collection,
            vectors_config=qm.VectorParams(
                size=self._vector_size,
                distance=distance_map[self._distance_name],
            ),
        )
        # Payload index on 'name' so allowed_views filtering is fast.
        client.create_payload_index(
            collection_name=self._collection,
            field_name="name",
            field_schema=qm.PayloadSchemaType.KEYWORD,
        )
        logger.info(
            "Created Qdrant collection %s (size=%d, distance=%s)",
            self._collection, self._vector_size, self._distance_name,
        )

    def reset_collection(self) -> None:
        """Drop and recreate — used by the indexer for a full re-index."""
        client = self._ensure_client()
        try:
            client.delete_collection(collection_name=self._collection)
            logger.info("Dropped Qdrant collection %s", self._collection)
        except Exception:
            logger.debug("Collection %s did not exist", self._collection)
        self.ensure_collection()

    # --------------------------------------------------------------- upsert #

    def upsert(
        self,
        docs: List[Dict[str, Any]],
        vectors: List[List[float]],
    ) -> int:
        """Upsert `docs` with their pre-computed `vectors`. Returns # points."""
        from qdrant_client import models as qm

        if len(docs) != len(vectors):
            raise ValueError(
                f"docs/vectors length mismatch: {len(docs)} vs {len(vectors)}"
            )
        if not docs:
            return 0
        client = self._ensure_client()
        points = [
            qm.PointStruct(
                id=_stable_point_id(d["name"]),
                vector=v,
                payload=d,
            )
            for d, v in zip(docs, vectors)
        ]
        client.upsert(collection_name=self._collection, points=points, wait=True)
        logger.info("Upserted %d points to %s", len(points), self._collection)
        return len(points)

    # ----------------------------------------------------------------- read #

    def count(self) -> int:
        return int(
            self._ensure_client().count(collection_name=self._collection).count
        )

    def search(
        self,
        query_vector: List[float],
        allowed_view_names: List[str],
        k: int,
    ) -> List[Dict[str, Any]]:
        """Top-K nearest, filtered to payload.name ∈ allowed_view_names.

        Filtering happens server-side, so we never pull a view that wasn't
        in the allow-list even if it scores higher.

        Uses ``query_points`` (qdrant-client ≥1.10 API). The deprecated
        ``search`` method was removed in qdrant-client 1.15+.
        """
        from qdrant_client import models as qm

        if not allowed_view_names:
            return []
        client = self._ensure_client()
        q_filter = qm.Filter(
            must=[qm.FieldCondition(
                key="name",
                match=qm.MatchAny(any=list(allowed_view_names)),
            )]
        )
        resp = client.query_points(
            collection_name=self._collection,
            query=query_vector,
            query_filter=q_filter,
            limit=k,
            with_payload=True,
        )
        # query_points returns a QueryResponse wrapping .points
        points = getattr(resp, "points", resp)
        return [
            {
                "name": p.payload.get("name"),
                "domain": p.payload.get("domain"),
                "description": p.payload.get("description"),
                "columns": p.payload.get("columns") or [],
                "owner_team": p.payload.get("owner_team"),
                "score": float(p.score),
            }
            for p in points
        ]


# --------------------------------------------------------------------------- #
# Module-level cache                                                          #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _build_store(url: str, collection: str, vector_size: int, distance: str) -> VectorStore:
    # Cache by primitive fields, but reconstruct from settings to keep the
    # constructor signature simple.
    return VectorStore(get_settings())


def get_vector_store(settings: Optional[Settings] = None) -> VectorStore:
    s = settings or get_settings()
    return _build_store(
        s.qdrant.url, s.qdrant.collection, s.qdrant.vector_size, s.qdrant.distance,
    )


def reset_vector_store_cache() -> None:
    _build_store.cache_clear()
