"""Indexer pipeline: OpenMetadata → embed → Qdrant.

Two paths in:
    1. CLI:   `python -m src.indexer`  (uses __main__.py wrapper)
    2. HTTP:  POST /v1/index/refresh   (calls run_indexing directly)

By default the pipeline performs upserts (idempotent — stable point IDs).
Pass reset=True to wipe and recreate the collection before upsert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..config import Settings, get_settings
from ..embedding import get_encoder
from ..store import get_vector_store
from .doc_synthesis import synthesize
from .om_source import OpenMetadataSource

logger = logging.getLogger(__name__)


@dataclass
class IndexResult:
    fetched: int   # tables pulled from OM
    indexed: int   # points upserted to Qdrant
    collection: str
    reset: bool


def run_indexing(
    settings: Optional[Settings] = None,
    *,
    reset: bool = False,
) -> IndexResult:
    """End-to-end indexer run. Returns counts for status reporting."""
    s = settings or get_settings()

    # 1. Pull
    source = OpenMetadataSource(s)
    tables = source.fetch_tables()
    logger.info("Indexer pulled %d tables from OpenMetadata", len(tables))

    # 2. Synthesize docs
    payloads: List[Dict[str, Any]] = []
    doc_texts: List[str] = []
    for t in tables:
        if not t.get("name"):
            continue
        payload, text = synthesize(t)
        payloads.append(payload)
        doc_texts.append(text)
    logger.info("Indexer synthesized %d docs", len(payloads))

    # 3. Embed
    encoder = get_encoder(s)
    vectors = encoder.encode(doc_texts) if doc_texts else []
    logger.info(
        "Indexer embedded %d docs (dim=%d)",
        len(vectors), encoder.dim if vectors else 0,
    )

    # 4. Store
    store = get_vector_store(s)
    if reset:
        store.reset_collection()
    else:
        store.ensure_collection()

    upserted = store.upsert(payloads, vectors) if payloads else 0
    return IndexResult(
        fetched=len(tables),
        indexed=upserted,
        collection=s.qdrant.collection,
        reset=reset,
    )
