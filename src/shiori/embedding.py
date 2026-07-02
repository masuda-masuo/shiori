"""Embeddings (detailed design/03).

Decisions:
- Default model is intfloat/multilingual-e5-small (384 dims). Lightweight and cross-lingual
  (Japanese query -> English document). Can be changed via EMBEDDING_MODEL, but re-indexing
  (`shiori ingest --rebuild`) is required since dimensions change.
- e5 models require "query: " / "passage: " prefixes, handled here.
- Model weights are cached in HF_HOME (named volume).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class Embedder:
    def __init__(self, model_name: str, expected_dim: int | None = None):
        # 重い import は遅延（CLI のヘルプ等を速くする）
        from sentence_transformers import SentenceTransformer

        log.info("loading embedding model: %s", model_name)
        self.model_name = model_name
        self.model = SentenceTransformer(model_name)
        self.dim = self.model.get_sentence_embedding_dimension()
        if expected_dim is not None and self.dim != expected_dim:
            raise ValueError(
                f"EMBEDDING_DIM={expected_dim} but model '{model_name}' produces "
                f"{self.dim} dims. Set EMBEDDING_DIM={self.dim} and re-ingest."
            )
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
