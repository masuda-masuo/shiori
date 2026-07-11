"""Ingest job (detailed design/01, 07).
On-demand: docker compose run --rm app python -m shiori ingest.
Auth via build_token_provider shared across all repos (detailed design/09).

Process mutual exclusion (issue #6): PostgreSQL advisory lock prevents concurrent execution with serve auto-sync or MCP ingest.

Freshness tracking (issue #22 / #33): Records completion to sync_runs per repo. Route via SHIORI_INGEST_ROUTE (default 'cli').

Security (issue #63): Validates repo against SHIORI_REPOS allowlist.
"""

from __future__ import annotations

import logging
import os
import time

from . import db
from .config import Settings, load_settings
from .embedding import Embedder
from .github_auth import build_token_provider
from .github_sync import ChunkBuffer, sync_code, sync_docs, sync_issues

log = logging.getLogger(__name__)

# PostgreSQL advisory lock key (shared with mcp_server.py. ASCII for 'SHIO')
SYNC_LOCK_KEY = 0x5348494F

# ChunkBuffer flush threshold for bulk path (issue #72)
_BULK_BUFFER_SIZE = 500


def _is_bulk_path(conn, rebuild: bool) -> bool:
    """Determine if bulk path: rebuild=True or chunks table empty/missing."""
    if rebuild:
        return True
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('chunks')")
        if cur.fetchone()[0] is None:
            return True
        cur.execute("SELECT count(*) FROM chunks")
        return cur.fetchone()[0] == 0


def run_ingest(
    settings: Settings | None = None,
    repos: list[str] | None = None,
    rebuild: bool = False,
) -> None:
    settings = settings or load_settings()

    # Allowlist validation: ensure specified repo is in settings.repos (issue #63)
    if repos is not None:
        allowed = set(settings.repos)
        invalid = sorted(set(repos) - allowed)
        if invalid:
            raise SystemExit(
                f"Specified repos not in SHIORI_REPOS: "
                f"{', '.join(invalid)}"
            )

    targets = repos or settings.repos
    if not targets:
        raise SystemExit("SHIORI_REPOS not set (e.g. SHIORI_REPOS=owner/name)")

    provider = build_token_provider(settings)
    route = os.environ.get("SHIORI_INGEST_ROUTE", "cli")

    conn = db.connect(settings)

    # --- Bulk path detection (detect before lock. Handles fresh DB. Issue #72) ---
    is_bulk = _is_bulk_path(conn, rebuild)

    # --- Schema prep: migrate_light is idempotent, safe outside lock ---
    if is_bulk:
        log.info("bulk path detected (rebuild=%s), using light schema + deferred indexes", rebuild)
        db.migrate_light(conn, settings)
    else:
        db.migrate(conn, settings)

    # --- Cross-process mutex: advisory lock ---
    # Prevent concurrent execution with serve auto-sync and MCP ingest at DB level.
    # Advisory lock is session-bound; acquire and release on the same connection.
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_KEY,))
        acquired = cur.fetchone()[0]
    if not acquired:
        log.info("skipped: sync already running in another process")
        conn.close()
        return

    t_total = time.monotonic()

    try:
        # --- Bulk path: destructive operations inside the lock (issue #72) ---
        if is_bulk:
            if rebuild:
                log.warning("rebuild: discarding existing index and sync cursors")
                with conn.cursor() as cur:
                    cur.execute("TRUNCATE chunks, doc_files, issue_items, sync_state")
                conn.commit()
            db.drop_heavy_indexes(conn)

        embedder = Embedder(settings.embedding_model, settings.embedding_dim)

        if is_bulk:
            buffer = ChunkBuffer(conn, embedder, batch_size=_BULK_BUFFER_SIZE)

        for repo in targets:
            log.info("=== %s ===", repo)

            try:
                # docs phase
                t0 = time.monotonic()
                n_docs = sync_docs(
                    settings, conn, embedder, repo, provider,
                    buffer=buffer if is_bulk else None,
                )
                if is_bulk:
                    n_flushed = buffer.flush()
                    conn.commit()  # Commit metadata (doc_files, set_cursor)
                    log.info("docs flushed: %d chunks", n_flushed)
                t_docs = time.monotonic() - t0
                log.info("docs: %d files updated (%.1fs)", n_docs, t_docs)

                # issues phase
                t0 = time.monotonic()
                n_items = sync_issues(
                    settings, conn, embedder, repo, provider,
                    buffer=buffer if is_bulk else None,
                )
                if is_bulk:
                    n_flushed = buffer.flush()
                    conn.commit()  # Commit metadata (issue_items, set_cursor)
                    log.info("issues flushed: %d chunks", n_flushed)
                t_issues = time.monotonic() - t0
                log.info("issues/PR: %d items indexed (%.1fs)", n_items, t_issues)

                # code phase
                t0 = time.monotonic()
                n_code = sync_code(
                    settings, conn, embedder, repo, provider,
                    buffer=buffer if is_bulk else None,
                )
                if is_bulk:
                    n_flushed = buffer.flush()
                    conn.commit()  # Commit metadata (doc_files, set_cursor)
                    log.info("code flushed: %d chunks", n_flushed)
                t_code = time.monotonic() - t0
                log.info("code: %d files updated (%.1fs)", n_code, t_code)

                finished_at = db.record_sync_run(
                    conn, repo, route, n_docs, n_items, n_code
                )
                # Record the successful attempt so shiori_status can report it and
                # so a failure streak from a prior CLI/compose ingest run is
                # cleared (issue #194 -- record_sync_run alone does not reset
                # consecutive_failures, only record_sync_attempt(success=True)
                # does; mirrors _do_sync in mcp_server.py, the MCP-tool ingest
                # path this CLI/compose path duplicates).
                db.record_sync_attempt(conn, repo, success=True)
                log.info("synced at %s (route=%s)", finished_at.isoformat(), route)
            except Exception as exc:
                # Record the failed attempt so shiori_status can surface it
                # (issue #194, same as the _do_sync path in mcp_server.py) --
                # without this, a repo whose CLI/compose ingest fails every time
                # leaves no trace at all and the "consecutive failures" warning
                # never fires for this route.
                conn.rollback()
                db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                raise

        # --- Bulk path: create heavy indexes in batch ---
        if is_bulk:
            t0 = time.monotonic()
            db.create_heavy_indexes(conn)
            t_idx = time.monotonic() - t0
            log.info("heavy indexes created (%.1fs)", t_idx)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_type, count(*) FROM chunks GROUP BY 1 ORDER BY 1"
            )
            for st, n in cur.fetchall():
                log.info("chunks[%s] = %d", st, n)

        t_total_elapsed = time.monotonic() - t_total
        log.info("total ingest time: %.1fs", t_total_elapsed)

    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
        conn.close()
