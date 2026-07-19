"""Embedding (detailed design/03).

Single provider architecture: model baked into the image (docker/app/Dockerfile).
Dimension is auto-detected from the model.

API: embed_passages(texts, batch_size) -> np.ndarray, embed_query(text) -> np.ndarray."""

from __future__ import annotations

import logging
import os

from shiori.config import DEFAULT_EMBEDDING_MODEL

log = logging.getLogger(__name__)


def resolve_batch_size() -> int:
    """Resolve batch_size for embed_passages.

    Priority:
    1. Env var ``SHIORI_EMBED_BATCH_SIZE`` (positive integer).
    2. Auto-detect: 512 if ``torch.cuda.is_available()``, else 32.
    """
    env_val = os.environ.get("SHIORI_EMBED_BATCH_SIZE")
    if env_val is not None:
        try:
            bs = int(env_val)
            if bs > 0:
                return bs
        except ValueError:
            pass
    try:
        import torch  # type: ignore[import-untyped]

        if torch.cuda.is_available():
            return 512
    except ImportError:
        pass
    return 32


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

    def embed_passages(self, texts: list[str], batch_size: int | None = None):
        if batch_size is None:
            batch_size = resolve_batch_size()
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
