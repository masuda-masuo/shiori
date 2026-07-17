from __future__ import annotations

import logging

import psycopg

from .db import bulk_insert_chunks
from .embedding import Embedder

log = logging.getLogger(__name__)


class ChunkBuffer:
    """Accumulate chunks, batch-embed, bulk-insert, coarse-commit for high throughput.
    Incremental path unused; initial/rebuild only (detailed design/01, 02, 10).
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        embedder: Embedder,
        batch_size: int = 500,
    ):
        self._conn = conn
        self._embedder = embedder
        self._batch_size = batch_size
        self._items: list[dict] = []
        self._texts: list[str] = []

    def add(self, *, chunk_key: str, chunk_index: int, source_type: str,
            repo: str, content: str,
            path: str | None = None, issue_no: int | None = None,
            comment_id: int | None = None, kind: str | None = None,
            language: str | None = None, heading_path: str | None = None,
            state: str | None = None, author: str | None = None,
            line: int | None = None,
            end_line: int | None = None, commit_sha: str | None = None,
            prog_lang: str | None = None, symbols: str | None = None,
            created_at=None, updated_at=None, url: str | None = None,
    ) -> None:
        """Add chunk to buffer. Auto-flushes at batch_size."""
        self._items.append({
            "chunk_key": chunk_key, "chunk_index": chunk_index,
            "source_type": source_type, "repo": repo, "content": content,
            "path": path, "issue_no": issue_no, "comment_id": comment_id,
            "kind": kind,
            "language": language, "heading_path": heading_path,
            "state": state, "author": author, "line": line,
            "end_line": end_line, "commit_sha": commit_sha,
            "prog_lang": prog_lang, "symbols": symbols,
            "created_at": created_at, "updated_at": updated_at,
            "url": url,
            # Embedding placeholder (filled at flush time)
            "embedding": None,
        })
        self._texts.append(content)
        if len(self._items) >= self._batch_size:
            self.flush()

    def flush(self) -> int:
        """Flush buffer: batch embed → bulk insert → commit. Returns insert count."""
        if not self._items:
            return 0
        n = len(self._items)
        vectors = self._embedder.embed_passages(
            self._texts, batch_size=min(len(self._texts), 256),
        )
        for item, vec in zip(self._items, vectors):
            item["embedding"] = vec
        bulk_insert_chunks(self._conn, self._items)
        self._conn.commit()
        log.info("ChunkBuffer flushed: %d chunks", n)
        self._items.clear()
        self._texts.clear()
        return n
