"""Sync pipeline: Phase 1 (clone refresh) + Phase 2 (re-index) orchestration.

Extracted from mcp_server.py (issue #281).
Shared between MCP server and CLI/compose ingest (ingest.py).

Process mutual exclusion (issue #6): threading + PostgreSQL advisory lock prevents
concurrent execution with serve auto-sync or MCP ingest.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import psycopg

from . import db
from .config import Settings, load_settings
from .embedding import Embedder
from .github_auth import build_token_provider
from .github_sync import ChunkBuffer, sync_code, sync_docs, sync_issues
from .ingest import SYNC_LOCK_KEY, _BULK_BUFFER_SIZE

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


def _is_bulk_path(conn, rebuild: bool) -> bool:
    """Determine bulk path: rebuild=True or chunks table empty/missing (issue #72)."""
    if rebuild:
        return True
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('chunks')")
        if cur.fetchone()[0] is None:
            return True
        cur.execute("SELECT count(*) FROM chunks")
        return cur.fetchone()[0] == 0


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
    exception still propagates unchanged (that case is instead covered by
    the module-level _auto_sync_last_error state set by _auto_sync_loop).
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

# Per-repo Phase 2 in-flight set: tracks repos currently being synced.
# Duplicate requests for the same repo are no-ops (single-flight, #236).
_phase2_in_flight: set[str] = set()
_phase2_pending: set[str] = set()  # repos waiting for a semaphore slot (#246)
_phase2_lock = threading.Lock()

# Phase 2 concurrency cap: at most N concurrent background sync threads (#236 self-review).
_phase2_semaphore = threading.BoundedSemaphore(2)


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


def _trigger_phase2(repo: str) -> None:
    """Trigger Phase 2 (re-index) for *repo* in the background.

    Single-flight: if Phase 2 is already running for this repo, this is a no-op.
    Concurrency-capped: at most _phase2_semaphore's value concurrent threads (#236).
    Pending queue: when the semaphore is saturated, the repo is enqueued and
    will be picked up when a running Phase 2 finishes (#246).
    """
    with _phase2_lock:
        if repo in _phase2_in_flight or repo in _phase2_pending:
            return
        if not _phase2_semaphore.acquire(blocking=False):
            _phase2_pending.add(repo)
            return
        _phase2_in_flight.add(repo)

    def _run():
        try:
            _do_sync(repos=[repo], route="pull")
        except Exception:
            log.exception("Phase 2 sync failed for %s", repo)
        finally:
            with _phase2_lock:
                _phase2_in_flight.discard(repo)
            _drain_pending()

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _drain_pending() -> None:
    """Pick the next pending repo and start its Phase 2 sync (#246).

    Called from a finished thread's finally block, after discarding the
    finished repo from _phase2_in_flight.  Must be called outside
    _phase2_lock to avoid deadlock with _trigger_phase2.
    """
    with _phase2_lock:
        if not _phase2_pending:
            _phase2_semaphore.release()
            return
        next_repo = _phase2_pending.pop()
        _phase2_in_flight.add(next_repo)

    def _next_run():
        try:
            _do_sync(repos=[next_repo], route="pull")
        except Exception:
            log.exception("Phase 2 sync failed for %s", next_repo)
        finally:
            with _phase2_lock:
                _phase2_in_flight.discard(next_repo)
            _drain_pending()

    t = threading.Thread(target=_next_run, daemon=True)
    t.start()


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
                is_bulk = _is_bulk_path(conn, rebuild)

                if is_bulk:
                    log.info("bulk path detected (rebuild=%s), using light schema + deferred indexes", rebuild)
                    db.migrate_light(conn, settings)
                else:
                    db.migrate(conn, settings)

                with conn.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_KEY,))
                    row = cur.fetchone()
                    acquired = row[0] if row is not None else False
            except Exception as exc:
                try:
                    conn.rollback()
                except psycopg.OperationalError:
                    pass
                for repo in targets:
                    db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                raise
            if not acquired:
                conn.close()
                return {"status": "skipped", "reason": "sync already running in another process"}
            try:
                if is_bulk:
                    if rebuild:
                        log.warning("rebuild: discarding existing index and sync cursors")
                        db.truncate_all_repos(conn)
                        conn.commit()
                    db.drop_heavy_indexes(conn)

                buffer: ChunkBuffer | None = None
                if is_bulk:
                    buffer = ChunkBuffer(conn, embedder, batch_size=_BULK_BUFFER_SIZE)

                failed_repos: dict[str, str] = {}
                for repo in targets:
                    try:
                        n_docs = sync_docs(
                            settings, conn, embedder, repo, provider,
                            buffer=buffer if is_bulk else None,
                        )
                        if is_bulk:
                            assert buffer is not None
                            buffer.flush()
                            conn.commit()
                        n_items = sync_issues(
                            settings, conn, embedder, repo, provider,
                            buffer=buffer if is_bulk else None,
                        )
                        if is_bulk:
                            assert buffer is not None
                            buffer.flush()
                            conn.commit()
                        n_code = sync_code(
                            settings, conn, embedder, repo, provider,
                            buffer=buffer if is_bulk else None,
                        )
                        if is_bulk:
                            assert buffer is not None
                            buffer.flush()
                            conn.commit()
                        finished_at = db.record_sync_run(
                            conn, repo, route, n_docs, n_items, n_code
                        )
                        db.record_sync_attempt(conn, repo, success=True)
                        indexed_head = db.get_cursor(conn, repo, "docs")
                        if indexed_head:
                            db.upsert_indexed_head(conn, repo, indexed_head)
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
                    except Exception as exc:
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
                            except Exception:
                                pass
                            conn = _conn()
                            log.warning(
                                "reconnected DB connection after OperationalError "
                                "during sync of %s", repo,
                            )

                if is_bulk:
                    db.create_heavy_indexes(conn)

                if failed_repos:
                    detail = "; ".join(f"{r}: {e}" for r, e in failed_repos.items())
                    raise RuntimeError(
                        f"sync failed for {len(failed_repos)}/{len(targets)} "
                        f"repo(s): {detail}"
                    )

            finally:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
                except psycopg.OperationalError:
                    pass
        finally:
            conn.close()
        return result
    finally:
        _sync_lock.release()
