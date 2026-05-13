"""Sentence-transformers wrapper.

Loaded lazily by default (first call to .encode() triggers the model download
on first run). Set ``embedding.preload = true`` in YAML to load eagerly at
startup — adds a few seconds to boot, eliminates first-request latency.
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache
from typing import List, Optional

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)


class Encoder:
    """Thin wrapper around sentence-transformers.SentenceTransformer.

    Thread-safe lazy loading. The underlying model is loaded once per process.
    """

    def __init__(self, model_name: str):
        self._model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            logger.info("Loading embedding model: %s", self._model_name)
            # Imported lazily so module load doesn't pay the import cost.
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
            logger.info(
                "Embedding model loaded: dim=%d", self.dim,
            )

    @property
    def dim(self) -> int:
        self._ensure_loaded()
        # `get_embedding_dimension` is the new name in sentence-transformers ≥5;
        # the older method still works but emits a FutureWarning.
        if hasattr(self._model, "get_embedding_dimension"):
            return int(self._model.get_embedding_dimension())  # type: ignore[union-attr]
        return int(self._model.get_sentence_embedding_dimension())  # type: ignore[union-attr]

    @property
    def model_name(self) -> str:
        return self._model_name

    def encode(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of texts → list of vectors (lists of floats)."""
        if not texts:
            return []
        self._ensure_loaded()
        vectors = self._model.encode(  # type: ignore[union-attr]
            texts,
            convert_to_numpy=True,
            show_progress_bar=False,
            normalize_embeddings=True,  # cosine-friendly
        )
        return [v.tolist() for v in vectors]


# --------------------------------------------------------------------------- #
# Module-level cache                                                          #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _build_encoder(model_name: str) -> Encoder:
    return Encoder(model_name)


def get_encoder(settings: Optional[Settings] = None) -> Encoder:
    s = settings or get_settings()
    enc = _build_encoder(s.embedding.model)
    if s.embedding.preload:
        enc._ensure_loaded()
    return enc


def reset_encoder_cache() -> None:
    _build_encoder.cache_clear()
