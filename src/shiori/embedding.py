"""Embedding (detailed design/03).

Single provider architecture: model baked into the image (docker/app/Dockerfile).
Dimension is auto-detected from the model.

API: embed(texts: list[str]) -> np.ndarray (batch), embed_one(text: str) -> np.ndarray, embed_many(texts: list[str]) -> list[np.ndarray]."""

from __future__ import annotations

import logging
import os

from shiori.config import DEFAULT_EMBEDDING_MODEL

log = logging.getLogger(__name__)


class Embedder:
    def __init__(self, model_name: str | None = None):
        from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]

        model_name = model_name or DEFAULT_EMBEDDING_MODEL
        log.info("loading embedding model: %s (pid=%d)", model_name, os.getpid())
        self.model_name = model_name
        self.model = SentenceTransformer(model_name)
        self.dim = self.model.get_sentence_embedding_dimension()
        self._is_e5 = "e5" in model_name.lower()
