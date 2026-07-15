"""Embedding (detailed design/03).

Single provider architecture: model baked into the image (docker/app/Dockerfile).
Dimension is auto-detected from the model.

API: embed_passages(texts, batch_size) -> np.ndarray, embed_query(text) -> np.ndarray."""

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

    def _prep(self, texts: list[str], kind: str) -> list[str]:
        if not self._is_e5:
            return texts
        prefix = "query: " if kind == "query" else "passage: "
        return [prefix + t for t in texts]

    def embed_passages(self, texts: list[str], batch_size: int = 32):
        return self.model.encode(
            self._prep(texts, "passage"),
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )

    def embed_query(self, text: str):
        return self.model.encode(
            self._prep([text], "query")[0], normalize_embeddings=True
        )
