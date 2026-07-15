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
import shutil
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
        row = cur.fetchone()
        if row is not None and row[0] is None:
            return True
        cur.execute("SELECT count(*) FROM chunks")
        row = cur.fetchone()
        return row is not None and row[0] == 0


def run_forget(
    repos: list[str],
    settings: Settings | None = None,
    keep_clone: bool = False,
) -> dict[str, dict[str, int]]:
    """Drop *repos* from the index. Returns rows deleted per repo per table.

    Exists because the only way to remove a stale repo used to be ``--rebuild``,
    which discards *every* repo and re-indexes from scratch.

    Takes the same advisory lock as ingest: deleting rows underneath a running
    sync would let that sync re-insert what we just deleted.
    """
    settings = settings or load_settings()
    conn = db.connect(settings)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_KEY,))
            row = cur.fetchone()
            assert row is not None  # a scalar SELECT always yields one row
            if not row[0]:
                raise SystemExit("sync is running in another process; try again later")

        result: dict[str, dict[str, int]] = {}
        for repo in repos:
            deleted = db.forget_repo(conn, repo)
            conn.commit()
            result[repo] = deleted
            log.info(
                "forget %s: %d rows deleted (%s)",
                repo,
                sum(deleted.values()),
                ", ".join(f"{t}={n}" for t, n in deleted.items() if n),
            )

            if keep_clone:
                continue
            repo_dir = settings.repo_dir(repo)
            if os.path.isdir(repo_dir):
                shutil.rmtree(repo_dir)
                log.info("forget %s: removed clone %s", repo, repo_dir)

            if repo in settings.repos:
                log.warning(
                    "forget %s: still listed in SHIORI_REPOS -- the next sync "
                    "will index it again",
                    repo,
                )
        return result
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
        conn.close()


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
        row = cur.fetchone()
        acquired = row[0] if row is not None else False
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
                db.truncate_all_repos(conn)
                conn.commit()
            db.drop_heavy_indexes(conn)

        embedder = Embedder()

        buffer: ChunkBuffer | None = None
        if is_bulk:
            buffer = ChunkBuffer(conn, embedder, batch_size=_BULK_BUFFER_SIZE)

        failed_repos: dict[str, str] = {}
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
                    assert buffer is not None
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
                    assert buffer is not None
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
                    assert buffer is not None
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
                synced_ts = finished_at.isoformat() if finished_at is not None else "?"
                log.info("synced at %s (route=%s)", synced_ts, route)
            except Exception as exc:
                # Record the failed attempt so shiori_status can surface it
                # (issue #194, same as the _do_sync path in mcp_server.py) --
                # without this, a repo whose CLI/compose ingest fails every time
                # leaves no trace at all and the "consecutive failures" warning
                # never fires for this route. record_sync_run / record_sync_attempt
                # each commit on their own (db.py), so this rollback only discards
                # *this* repo's own uncommitted work -- an earlier repo in this
                # same loop already landed via its own commit (issue #199
                # rollback-scope question).
                conn.rollback()
                db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                if is_bulk:
                    # Bulk (initial full ingest) has no per-repo resume story, so
                    # a partial failure still aborts the whole run immediately,
                    # same as before (issue #199).
                    raise
                # Diff sync (normal operation, mirrors _do_sync in
                # mcp_server.py): one repo's failure must not block the rest
                # (issue #199) -- record and move on, then raise an aggregate
                # error (and thus a non-zero CLI exit) once every repo has had
                # a chance to run.
                failed_repos[repo] = str(exc)
                log.exception(
                    "sync failed for %s (route=%s), continuing with remaining repos",
                    repo, route,
                )

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

        if failed_repos:
            detail = "; ".join(f"{r}: {e}" for r, e in failed_repos.items())
            raise RuntimeError(
                f"sync failed for {len(failed_repos)}/{len(targets)} repo(s): {detail}"
            )

    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
        conn.close()
