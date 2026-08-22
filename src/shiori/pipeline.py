"""Sync pipeline: Phase 1 (clone refresh) + Phase 2 (re-index) orchestration.

Extracted from mcp_server.py (issue #281).
Provides the sync orchestration layer consumed by mcp_server.py; designed
for eventual sharing with ingest.py.

Process mutual exclusion (issue #6): threading + PostgreSQL advisory lock prevents
concurrent execution with serve auto-sync or MCP ingest.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import psycopg

from . import db, schema
from .config import Settings, is_docs_only, load_settings
from .embedding import Embedder
from .github_auth import build_token_provider
from .github_sync import ChunkBuffer, sync_code, sync_docs, sync_issues
from .ingest import (
    _BULK_BUFFER_SIZE,
    _acquire_repo_lock,
    _bulk_covers_all_repos,
    _bulk_run_completed_all_repos,
    _is_bulk_path,
    _release_repo_lock,
    repo_lock,
)

log = logging.getLogger(__name__)

settings: Settings = load_settings()
_sync_lock = threading.Lock()
_embedder: Embedder | None = None
_embedder_lock = threading.Lock()


def _get_embedder() -> Embedder:
    global _embedder
    with _embedder_lock:
        if _embedder is None:
            _embedder = Embedder()
    return _embedder


def _conn():
    return db.connect(settings)


def _record_pre_loop_sync_failure(targets: list[str], error: str) -> None:
    """Best-effort record of a sync failure that happened before any
    repo-scoped work started -- token provider construction, embedder
    creation (issue #196; #195 is the concrete case that exposed this: a
    missing embedding dependency raises out of _get_embedder() before the
    per-repo loop's own except ever runs, so sync_runs stays silent even
    though the sync is, in effect, failing for every configured repo).

    Opens its own short-lived connection since the caller may not have one
    yet at this point in _do_sync(). Swallows its own errors -- if the DB
    itself is unreachable, this is a no-op and the caller's original
    exception still propagates unchanged (the unreachable-case is instead
    covered by _auto_sync_last_error in mcp_server.py).
    """
    try:
        with _conn() as conn:
            for repo in targets:
                db.record_sync_attempt(conn, repo, success=False, error=error)
    except Exception:
        log.exception("failed to record pre-loop sync failure for %s", targets)


# Per-repo Phase 1 single-flight: one concurrent git fetch per repo.
# Key: repo name. Value: threading.Lock for that repo.
# _phase1_locks guard protects the dict itself.
_phase1_locks: dict[str, threading.Lock] = {}
_phase1_locks_guard = threading.Lock()

# Per-repo Phase 1 debounce: last fetch timestamp for each repo.
# Updated atomically within the per-repo lock after fetch completes.
_phase1_last_fetch: dict[str, float] = {}

# Minimum debounce interval (seconds) when sync_interval_seconds=0.
# Prevents sequential re-fetches within the per-repo lock even when
# debounce is disabled by configuration (#236 self-review).
_PHASE1_MIN_DEBOUNCE = 1.0


def _ensure_phase1(repo: str) -> str | None:
    """Ensure clone is fresh for *repo*. Returns HEAD SHA or None on failure.

    Inline (blocking) call for Phase 1. Per-repo single-flight with debounce:
    at most one git fetch per repo at any time; subsequent callers within the
    debounce window (or while a fetch is in-flight) wait behind the per-repo
    lock and see the result immediately without re-fetching (#236 review fix).
    """
    with _phase1_locks_guard:
        lock = _phase1_locks.get(repo)
        if lock is None:
            lock = threading.Lock()
            _phase1_locks[repo] = lock

    with lock:
        now = time.monotonic()
        last = _phase1_last_fetch.get(repo, 0.0)
        try:
            interval = int(settings.sync_interval_seconds)
        except (TypeError, ValueError):
            interval = 0
        debounce = max(interval, _PHASE1_MIN_DEBOUNCE)
        if last > 0 and (now - last) < debounce:
            return None  # debounced: skip

        try:
            provider = build_token_provider(settings)
            from .refresh import refresh_clone
            head = refresh_clone(repo, provider, settings)
            _phase1_last_fetch[repo] = time.monotonic()
            try:
                with _conn() as conn:
                    db.upsert_clone_head(conn, repo, head)
            except Exception:
                log.exception("upsert_clone_head failed for %s", repo)
            return head
        except Exception:
            log.exception("Phase 1 clone refresh failed for %s", repo)
            return None


def _do_sync(
    repos: list[str] | None = None,
    rebuild: bool = False,
    route: str = "mcp",
) -> dict[str, Any]:
    """Incremental sync body. Called by both ingest tool and auto-sync loop.
    Process-level exclusion via _sync_lock (threading.Lock)."""
    # Allowlist validation: ensure specified repo is in settings.repos (issue #63)
    if repos is not None:
        allowed = set(settings.repos)
        invalid = sorted(set(repos) - allowed)
        if invalid:
            raise ValueError(
                f"Specified repos not in SHIORI_REPOS: "
                f"{', '.join(invalid)}"
            )

    if not _sync_lock.acquire(blocking=False):
        return {"status": "skipped", "reason": "sync already running"}
    try:
        targets = repos or settings.repos
        if not targets:
            return {"status": "error", "reason": "SHIORI_REPOS not set"}
        try:
            provider = build_token_provider(settings)
            embedder = _get_embedder()
        except Exception as exc:
            _record_pre_loop_sync_failure(targets, str(exc))
            raise
        result: dict[str, Any] = {"status": "ok", "repos": {}}
        conn = _conn()
        try:
            try:
                # Deliberately no targets/settings: the volume trigger
                # (SHIORI_BULK_PENDING_THRESHOLD, issue #376) was measured
                # on the CLI path only, so an MCP ingest() stays on the
                # item-at-a-time path no matter how large the backlog.
                # Whether the MCP path should get the volume check is an
                # open question waiting on a separate measurement (issue
                # #382); this call site is the one visible difference from
                # ingest's callers, not a forked copy that can drift again.
                is_bulk = _is_bulk_path(conn, rebuild)

                if is_bulk:
                    log.info("bulk path detected (rebuild=%s), using light schema + deferred indexes", rebuild)
                    schema.migrate_light(conn, settings)
                else:
                    schema.migrate(conn, settings)
            except Exception as exc:
                try:
                    conn.rollback()
                except psycopg.OperationalError:
                    pass
                for repo in targets:
                    db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                raise

            try:
                if is_bulk:
                    if rebuild:
                        log.warning("rebuild: discarding existing index and sync cursors")
                        schema.truncate_all_repos(conn)
                        conn.commit()
                    # Issue #364: only rebuild or a sync that intends to
                    # cover every configured repo may drop the heavy
                    # indexes (shared helper ingest._bulk_covers_all_repos).
                    # A scoped sync neither drops nor creates them: during
                    # a genuine drain they're already absent, and if they
                    # exist the drop is exactly the #364 accident.
                    if rebuild or _bulk_covers_all_repos(targets, settings):
                        schema.drop_heavy_indexes(conn)
                    else:
                        log.info(
                            "heavy indexes drop skipped: scoped bulk sync (%d/%d repos)",
                            len(targets), len(settings.repos),
                        )

                buffer: ChunkBuffer | None = None
                if is_bulk:
                    buffer = ChunkBuffer(conn, embedder, batch_size=_BULK_BUFFER_SIZE)

                failed_repos: dict[str, str] = {}
                completed: list[str] = []
                for repo in targets:
                    # Per-repo PG advisory lock (issue #307): acquire,
                    # skip recording and release all live in
                    # repo_lock() (issue #374). Only an *acquire*
                    # failure (DB-level problem) is handled here --
                    # recorded as a failed attempt, never as a skip.
                    # recorded_exc distinguishes that case from the
                    # bulk-mode re-raise below, so a work failure is
                    # recorded exactly once.
                    recorded_exc: BaseException | None = None
                    try:
                        with repo_lock(
                            conn, repo, phase="sync",
                            acquire=_acquire_repo_lock,
                            release=_release_repo_lock,
                        ) as lock_ok:
                            if not lock_ok:
                                continue

                            try:
                                n_docs = sync_docs(
                                    settings, conn, embedder, repo, provider,
                                    buffer=buffer if is_bulk else None,
                                )
                                if is_bulk:
                                    assert buffer is not None  # noqa: S101 - type-narrowing assert (guarded by is_bulk)
                                    buffer.flush()
                                    conn.commit()
                                if is_docs_only(settings, repo):
                                    log.info("sync issues: skipping %s (docs-only repo)", repo)
                                    n_items = 0
                                else:
                                    n_items = sync_issues(
                                        settings, conn, embedder, repo, provider,
                                        buffer=buffer if is_bulk else None,
                                    )
                                    if is_bulk:
                                        assert buffer is not None  # noqa: S101 - type-narrowing assert (guarded by is_bulk)
                                        buffer.flush()
                                        conn.commit()
                                n_code = sync_code(
                                    settings, conn, embedder, repo, provider,
                                    buffer=buffer if is_bulk else None,
                                )
                                if is_bulk:
                                    assert buffer is not None  # noqa: S101 - type-narrowing assert (guarded by is_bulk)
                                    buffer.flush()
                                    conn.commit()
                                finished_at = db.record_sync_run(
                                    conn, repo, route, n_docs, n_items, n_code
                                )
                                db.record_sync_attempt(conn, repo, success=True)
                                db.advance_indexed_head(conn, repo)
                                db.refresh_chunk_counts(conn, repo)
                                result["repos"][repo] = {
                                    "docs_updated": n_docs,
                                    "issues_indexed": n_items,
                                    "code_added": n_code,
                                    "synced_at": finished_at.isoformat() if finished_at is not None else None,
                                }
                                log.info(
                                    "synced %s: docs=%d issues=%d code=%d (route=%s)",
                                    repo, n_docs, n_items, n_code, route,
                                )
                                completed.append(repo)
                            except Exception as exc:
                                recorded_exc = exc
                                need_reconnect = False
                                try:
                                    conn.rollback()
                                except psycopg.OperationalError:
                                    need_reconnect = True
                                try:
                                    db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                                except psycopg.OperationalError:
                                    need_reconnect = True
                                    with _conn() as tmp_conn:
                                        db.record_sync_attempt(tmp_conn, repo, success=False, error=str(exc))
                                try:
                                    db.record_repo_sync_error(conn, repo, str(exc))
                                except psycopg.OperationalError:
                                    with _conn() as tmp_conn:
                                        db.record_repo_sync_error(tmp_conn, repo, str(exc))
                                if is_bulk:
                                    raise
                                failed_repos[repo] = str(exc)
                                log.exception(
                                    "sync failed for %s (route=%s), continuing with "
                                    "remaining repos", repo, route,
                                )
                                if need_reconnect:
                                    try:
                                        conn.close()
                                    except Exception:  # noqa: BLE001, S110 - close failure non-fatal; reconnecting anyway
                                        pass
                                    conn = _conn()
                                    log.warning(
                                        "reconnected DB connection after OperationalError "
                                        "during sync of %s", repo,
                                    )
                    except Exception as exc:
                        if exc is not recorded_exc:
                            db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                        raise


                if is_bulk:
                    # Gate on repos that actually completed, not the
                    # intended target list (shared helper
                    # ingest._bulk_run_completed_all_repos, issue #365): a
                    # per-repo advisory-lock skip can leave the coverage
                    # check passing on intention while a repo was never
                    # re-indexed. A scoped bulk sync during a reindex drain
                    # must also not rebuild the heavy indexes early
                    # (issue #352).
                    if _bulk_run_completed_all_repos(completed, settings):
                        schema.create_heavy_indexes(conn, settings)
                    elif _bulk_covers_all_repos(targets, settings):
                        skipped = sorted(set(targets) - set(completed))
                        log.info(
                            "heavy indexes deferred: %d repo(s) skipped via "
                            "advisory lock during a sync that intended to "
                            "cover all repos (%s); rerun once they are free",
                            len(skipped), ", ".join(skipped),
                        )
                    else:
                        log.info(
                            "heavy indexes deferred: scoped bulk sync (%d/%d repos)",
                            len(targets), len(settings.repos),
                        )

                if failed_repos:
                    detail = "; ".join(f"{r}: {e}" for r, e in failed_repos.items())
                    raise RuntimeError(
                        f"sync failed for {len(failed_repos)}/{len(targets)} "
                        f"repo(s): {detail}"
                    )

            finally:
                pass  # per-repo locks already released in finally blocks above
        finally:
            conn.close()
        return result
    finally:
        _sync_lock.release()
