"""Ingest job (detailed design/01, 07).
On-demand: docker compose run --rm app python -m shiori ingest.
Auth via build_token_provider shared across all repos (detailed design/09).

Process mutual exclusion (issue #6): PostgreSQL advisory lock prevents concurrent execution with serve auto-sync or MCP ingest.

Freshness tracking (issue #22 / #33): Records completion to sync_runs per repo. Route via SHIORI_INGEST_ROUTE (default 'cli').

Security (issue #63): Validates repo against SHIORI_REPOS allowlist.

Subcommand split (issue #306):
- run_fetch: API fetch + git pull only, populates issue_items/doc_files on disk
- run_index: read issue_items / doc_files, chunk + embed, write to chunks
- run_ingest (alias for run): fetch + index, backward compatible
"""

from __future__ import annotations

import logging
import os
import shutil
import time

from . import db, schema
from .config import Settings, load_settings
from .embedding import Embedder
from .github_auth import build_token_provider
from .github_sync import (
    ChunkBuffer,
    fetch_docs,
    fetch_issues,
    index_code,
    index_docs,
    index_issues,
)

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
            deleted = schema.forget_repo(conn, repo)
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


# ── Common helpers for fetch/index/run ───────────────────────────────────


def _acquire_lock(conn) -> bool:
    """Acquire PostgreSQL advisory lock. Returns True if acquired."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_KEY,))
        row = cur.fetchone()
        return row[0] if row is not None else False


def _release_lock(conn) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
    except Exception:
        pass


def _validate_repos(repos: list[str] | None, settings: Settings) -> list[str]:
    """Validate repos against SHIORI_REPOS allowlist (issue #63)."""
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
    return targets


def _route() -> str:
    return os.environ.get("SHIORI_INGEST_ROUTE", "cli")


# ── run_fetch: API/git only, no chunk/embed ──────────────────────────────


def run_fetch(
    settings: Settings | None = None,
    repos: list[str] | None = None,
) -> None:
    """Fetch phase: API fetch + git pull only.

    Populates issue_items from GitHub API and ensures git clones are up to
    date.  Does NOT write to chunks.
    """
    settings = settings or load_settings()
    targets = _validate_repos(repos, settings)
    provider = build_token_provider(settings)

    conn = db.connect(settings)
    schema.migrate(conn, settings)

    lock_acquired = _acquire_lock(conn)
    if not lock_acquired:
        log.info("skipped: sync already running in another process")
        conn.close()
        return

    t_total = time.monotonic()
    try:
        for repo in targets:
            log.info("=== fetch %s ===", repo)
            t0 = time.monotonic()

            # Fetch docs (git pull)
            try:
                head = fetch_docs(settings, conn, repo, provider)
                if head:
                    log.info("fetch docs: clone refreshed at %s (%.1fs)",
                             head[:8], time.monotonic() - t0)
                else:
                    log.warning("fetch docs: clone refresh failed for %s", repo)
            except Exception as exc:
                conn.rollback()
                db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                log.exception("fetch docs failed for %s", repo)
                raise

            # Fetch issues/PRs/comments/reviews (API only)
            try:
                t0 = time.monotonic()
                n_fetched = fetch_issues(settings, conn, repo, provider)
                log.info("fetch issues: %d items fetched (%.1fs)", n_fetched, time.monotonic() - t0)
            except Exception as exc:
                conn.rollback()
                db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                log.exception("fetch issues failed for %s", repo)
                raise

        t_total_elapsed = time.monotonic() - t_total
        log.info("total fetch time: %.1fs", t_total_elapsed)
    finally:
        if lock_acquired:
            _release_lock(conn)
        conn.close()


# ── run_index: read issue_items/doc_files, chunk + embed ─────────────────


def run_index(
    settings: Settings | None = None,
    repos: list[str] | None = None,
    rebuild: bool = False,
) -> None:
    """Index phase: read from issue_items / doc_files, chunk + embed, write to chunks.

    Idempotent: running this multiple times against the same data produces
    the same chunks.
    """
    settings = settings or load_settings()
    targets = _validate_repos(repos, settings)
    route = _route()

    conn = db.connect(settings)
    is_bulk = _is_bulk_path(conn, rebuild)

    if is_bulk:
        log.info("bulk path detected (rebuild=%s), using light schema + deferred indexes", rebuild)
        schema.migrate_light(conn, settings)
    else:
        schema.migrate(conn, settings)

    lock_acquired = _acquire_lock(conn)
    if not lock_acquired:
        log.info("skipped: sync already running in another process")
        conn.close()
        return

    t_total = time.monotonic()
    try:
        if is_bulk:
            if rebuild:
                log.warning("rebuild: discarding existing index and sync cursors")
                schema.truncate_all_repos(conn)
                conn.commit()
            schema.drop_heavy_indexes(conn)

        embedder = Embedder()
        buffer: ChunkBuffer | None = None
        if is_bulk:
            buffer = ChunkBuffer(conn, embedder, batch_size=_BULK_BUFFER_SIZE)

        failed_repos: dict[str, str] = {}

        for repo in targets:
            log.info("=== index %s ===", repo)
            try:
                # Index docs
                t0 = time.monotonic()
                n_docs = index_docs(
                    settings, conn, embedder, repo,
                    buffer=buffer if is_bulk else None,
                )
                if is_bulk and buffer is not None:
                    n_flushed = buffer.flush()
                    conn.commit()
                    log.info("docs flushed: %d chunks", n_flushed)
                t_docs = time.monotonic() - t0
                log.info("index docs: %d files updated (%.1fs)", n_docs, t_docs)

                # Index issues/PRs
                t0 = time.monotonic()
                n_items = index_issues(
                    settings, conn, embedder, repo,
                    buffer=buffer if is_bulk else None,
                )
                if is_bulk and buffer is not None:
                    n_flushed = buffer.flush()
                    conn.commit()
                    log.info("issues flushed: %d chunks", n_flushed)
                t_issues = time.monotonic() - t0
                log.info("index issues: %d items indexed (%.1fs)", n_items, t_issues)

                # Index code
                t0 = time.monotonic()
                n_code = index_code(
                    settings, conn, embedder, repo,
                    buffer=buffer if is_bulk else None,
                )
                if is_bulk and buffer is not None:
                    n_flushed = buffer.flush()
                    conn.commit()
                    log.info("code flushed: %d chunks", n_flushed)
                t_code = time.monotonic() - t0
                log.info("index code: %d files updated (%.1fs)", n_code, t_code)

                # Record success
                finished_at = db.record_sync_run(
                    conn, repo, route, n_docs, n_items, n_code
                )
                db.record_sync_attempt(conn, repo, success=True)
                synced_ts = finished_at.isoformat() if finished_at is not None else "?"
                log.info("indexed at %s (route=%s)", synced_ts, route)
            except Exception as exc:
                conn.rollback()
                db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                if is_bulk:
                    raise
                failed_repos[repo] = str(exc)
                log.exception(
                    "index failed for %s (route=%s), continuing with remaining repos",
                    repo, route,
                )

        # --- Bulk path: create heavy indexes in batch ---
        if is_bulk:
            t0 = time.monotonic()
            schema.create_heavy_indexes(conn)
            t_idx = time.monotonic() - t0
            log.info("heavy indexes created (%.1fs)", t_idx)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_type, count(*) FROM chunks GROUP BY 1 ORDER BY 1"
            )
            for st, n in cur.fetchall():
                log.info("chunks[%s] = %d", st, n)

        t_total_elapsed = time.monotonic() - t_total
        log.info("total index time: %.1fs", t_total_elapsed)

        if failed_repos:
            detail = "; ".join(f"{r}: {e}" for r, e in failed_repos.items())
            raise RuntimeError(
                f"index failed for {len(failed_repos)}/{len(targets)} repo(s): {detail}"
            )

    finally:
        if lock_acquired:
            _release_lock(conn)
        conn.close()


# ── run_ingest (combined fetch + index) — backward compatible ────────────


def run_ingest(
    settings: Settings | None = None,
    repos: list[str] | None = None,
    rebuild: bool = False,
) -> None:
    """Combined fetch + index (legacy ingest behavior).

    Equivalent to calling run_fetch then run_index sequentially.
    Backward-compatible: ``shiori ingest`` (no subcommand) and
    ``shiori ingest run`` both call this function.
    """
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
        schema.migrate_light(conn, settings)
    else:
        schema.migrate(conn, settings)

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
                schema.truncate_all_repos(conn)
                conn.commit()
            schema.drop_heavy_indexes(conn)

        embedder = Embedder()

        buffer: ChunkBuffer | None = None
        if is_bulk:
            buffer = ChunkBuffer(conn, embedder, batch_size=_BULK_BUFFER_SIZE)

        failed_repos: dict[str, str] = {}
        for repo in targets:
            log.info("=== %s === (fetch + index)", repo)

            try:
                # --- Fetch phase ---
                # docs: git pull
                t0 = time.monotonic()
                fetch_docs(settings, conn, repo, provider)
                t_fetch_docs = time.monotonic() - t0
                log.info("fetch docs: %.1fs", t_fetch_docs)

                # issues: API
                t0 = time.monotonic()
                fetch_issues(settings, conn, repo, provider)
                t_fetch_issues = time.monotonic() - t0
                log.info("fetch issues: %.1fs", t_fetch_issues)

                # --- Index phase ---
                # docs: walk + chunk + embed
                t0 = time.monotonic()
                n_docs = index_docs(
                    settings, conn, embedder, repo,
                    buffer=buffer if is_bulk else None,
                )
                if is_bulk:
                    assert buffer is not None
                    n_flushed = buffer.flush()
                    conn.commit()
                    log.info("docs flushed: %d chunks", n_flushed)
                t_docs = time.monotonic() - t0
                log.info("index docs: %d files updated (%.1fs)", n_docs, t_docs)

                # issues: read issue_items + chunk + embed
                t0 = time.monotonic()
                n_items = index_issues(
                    settings, conn, embedder, repo,
                    buffer=buffer if is_bulk else None,
                )
                if is_bulk:
                    assert buffer is not None
                    n_flushed = buffer.flush()
                    conn.commit()
                    log.info("issues flushed: %d chunks", n_flushed)
                t_issues = time.monotonic() - t0
                log.info("index issues: %d items indexed (%.1fs)", n_items, t_issues)

                # code: walk + chunk + embed
                t0 = time.monotonic()
                n_code = index_code(
                    settings, conn, embedder, repo,
                    buffer=buffer if is_bulk else None,
                )
                if is_bulk:
                    assert buffer is not None
                    n_flushed = buffer.flush()
                    conn.commit()
                    log.info("code flushed: %d chunks", n_flushed)
                t_code = time.monotonic() - t0
                log.info("index code: %d files updated (%.1fs)", n_code, t_code)

                finished_at = db.record_sync_run(
                    conn, repo, route, n_docs, n_items, n_code
                )
                db.record_sync_attempt(conn, repo, success=True)
                synced_ts = finished_at.isoformat() if finished_at is not None else "?"
                log.info("synced at %s (route=%s)", synced_ts, route)
            except Exception as exc:
                # Record the failed attempt (issue #194)
                conn.rollback()
                db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                if is_bulk:
                    raise
                failed_repos[repo] = str(exc)
                log.exception(
                    "sync failed for %s (route=%s), continuing with remaining repos",
                    repo, route,
                )

        # --- Bulk path: create heavy indexes in batch ---
        if is_bulk:
            t0 = time.monotonic()
            schema.create_heavy_indexes(conn)
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
