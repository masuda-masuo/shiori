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
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import db, schema
from .config import IndexBudget, Settings, load_settings
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


def _index_budget(settings: Settings) -> IndexBudget:
    """Construct the run's time budget once, from settings (issue #377).

    ``None``/unset means unbounded (the default).  Defensive against test
    settings objects (MagicMock): anything that is not a positive number
    yields an unbounded budget.
    """
    return IndexBudget(getattr(settings, "ingest_time_budget", None))


def _budget_desc(budget: IndexBudget) -> str:
    """One-line description of the budget in force, for the run-start log."""
    seconds = budget.budget_seconds
    if isinstance(seconds, (int, float)) and seconds > 0:
        return f"{seconds:.0f}s (working time; suspend is not charged)"
    return "unbounded (SHIORI_INGEST_TIME_BUDGET unset or not a positive number)"


def _is_bulk_path(
    conn,
    rebuild: bool,
    targets: list[str] | None = None,
    settings: Settings | None = None,
) -> bool:
    """Determine if bulk path: rebuild=True, chunks table empty/missing, the
    HNSW index is absent (issue #352), or the pending indexing volume for the
    targeted repos reaches the configured threshold (issue #376).

    Heavy-index absence is the persistent, DB-derived marker of a drain in
    progress (e.g. a ``reindex`` that dropped the heavy indexes and is
    working through the bulk chunk/embed pass, possibly across several
    invocations). While it is absent, every ``index``/``run`` invocation
    stays on the bulk path (deferred heavy indexes); only a run that
    completes successfully rebuilds them once, at the end. A volume-triggered
    bulk run reuses this same marker as its resume mechanism -- nothing new
    is built for it (issue #376): if a covering run is interrupted after
    dropping the heavy indexes, later invocations stay on the bulk path via
    the absent-index check; a scoped run (which never drops them) stays on
    it via the volume check itself as long as the backlog remains.

    The volume check is a single COUNT over ``issue_items`` scoped to the
    targeted repos, using the same pending predicate ``index_issues`` applies
    (``db.PENDING_ISSUE_ITEMS_WHERE`` -- the one shared definition of
    "pending", issue #377) -- issue_items is the only SQL-side proxy for
    pending chunk/embed work (doc/code re-chunking is sha-driven off the
    on-disk clone and not countable in SQL). No rows are fetched, no
    per-repo loop runs, and nothing is truncated or reset: the bulk path
    only batches; discarding is exclusive to ``rebuild=True``.

    ``targets``/``settings`` are optional for backward compatibility with
    callers that predate the volume check (e.g. tests, and the duplicated
    ``shiori.pipeline._is_bulk_path``): when either is missing the volume
    check is skipped and the pre-#376 behaviour is exactly preserved.
    """
    if rebuild:
        return True
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('chunks')")
        row = cur.fetchone()
        if row is not None and row[0] is None:
            return True
        cur.execute("SELECT count(*) FROM chunks")
        row = cur.fetchone()
        if row is not None and row[0] == 0:
            return True
        cur.execute("SELECT to_regclass('chunks_embedding_hnsw')")
        row = cur.fetchone()
        if row is not None and row[0] is None:
            return True
        # Issue #376: pending volume across the targeted repos. Defensive
        # settings access (MagicMock in tests must not crash the check,
        # mirroring _should_skip_repo).
        if targets and settings is not None:
            threshold = getattr(settings, "bulk_pending_threshold", 0)
            if isinstance(threshold, (int, float)) and threshold > 0:
                cur.execute(
                    f"SELECT count(*) FROM issue_items WHERE repo = ANY(%s) "
                    f"AND {db.PENDING_ISSUE_ITEMS_WHERE}",
                    (list(targets),),
                )
                row = cur.fetchone()
                if row is not None and row[0] >= threshold:
                    return True
    return False


def _bulk_covers_all_repos(targets: list[str], settings: Settings) -> bool:
    """True when a bulk run *intended* to cover every configured repo (issue #352).

    Checked before the per-repo loop runs, so it only knows the target
    list -- not whether every repo actually completed. Used to gate the
    DROP side (``drop_heavy_indexes`` happens before the loop, so
    completion isn't known yet) and, on its own, is NOT sufficient to gate
    the CREATE side -- see ``_bulk_run_completed_all_repos`` (issue #365).
    """
    return bool(settings.repos) and set(targets) >= set(settings.repos)


def _bulk_run_completed_all_repos(completed: list[str], settings: Settings) -> bool:
    """True when every configured repo actually finished successfully during
    a bulk run (issue #365).

    Only such a run may rebuild the heavy indexes at its end. Checking the
    *intended* target list (``_bulk_covers_all_repos``) is not enough: a
    per-repo advisory-lock skip (``continue`` when ``_acquire_repo_lock``
    fails), or in ``run_ingest`` a circuit-breaker pre-skip, can leave a
    repo unprocessed while the intended-coverage check still passes.
    Rebuilding on that partial data triggers an hours-long index build and
    flips every later invocation off the fast bulk path with a backlog
    remaining -- the same failure mode #352 fixed for the scoped-by-design
    case, but reached via a skip instead of an explicit scope.
    """
    return bool(settings.repos) and set(completed) >= set(settings.repos)


def run_forget(
    repos: list[str],
    settings: Settings | None = None,
    keep_clone: bool = False,
) -> dict[str, dict[str, int]]:
    """Drop *repos* from the index. Returns rows deleted per repo per table.

    Exists because the only way to remove a stale repo used to be ``--rebuild``,
    which discards *every* repo and re-indexes from scratch.

    Uses per-repo advisory lock (issue #307): deleting rows underneath a running
    sync of the same repo is prevented, but different repos can proceed
    concurrently.
    """
    settings = settings or load_settings()
    conn = db.connect(settings)
    try:
        result: dict[str, dict[str, int]] = {}
        for repo in repos:
            # Per-repo advisory lock (issue #307): acquire, skip recording
            # and release all live in repo_lock(). The skip is recorded
            # durably even though this site raises SystemExit instead of
            # continuing (issue #374).
            with repo_lock(conn, repo, phase="forget") as held:
                if not held:
                    raise SystemExit(
                        f"sync is running for {repo} in another process; try again later"
                    )
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
        conn.close()


# ── Common helpers for fetch/index/run ───────────────────────────────────


def _acquire_repo_lock(conn, repo: str) -> bool:
    """Acquire per-repo PostgreSQL advisory lock (2-argument form).

    Uses hashtext(repo) as the second key so different repos can be
    synced concurrently across processes while the same repo remains
    mutually exclusive (issue #307).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(%s, hashtext(%s))",
            (SYNC_LOCK_KEY, repo),
        )
        row = cur.fetchone()
        return row[0] if row is not None else False


def _release_repo_lock(conn, repo: str) -> bool:
    """Release the per-repo advisory lock.

    Returns True when the lock was released, False when this session did not
    hold it (the lock was lost mid-run -- see ``repo_lock``). Raises when the
    release statement itself failed.

    The return value and the exception are both signals and are no longer
    discarded (issue #374): ``pg_advisory_unlock`` returning false (with a
    server warning) means the session lost the lock while work was still
    running -- the connection died (issue #370/#373) -- so another process
    could have entered the same repo. Callers that must not let a release
    error mask their own outcome catch it here; this function never swallows.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_unlock(%s, hashtext(%s))",
            (SYNC_LOCK_KEY, repo),
        )
        row = cur.fetchone()
        return bool(row[0]) if row is not None else False


@contextmanager
def repo_lock(
    conn, repo: str, phase: str = "index", acquire=None, release=None,
):
    """Acquire the per-repo advisory lock and own the whole lifecycle.

    The single place where acquire / skip recording / release live, so a new
    phase that takes the lock gets the recording for free (issue #374). The
    lock granularity, key and session-level semantics are unchanged (issue
    #307): two-argument ``pg_try_advisory_lock`` keyed on the repo, held for
    the session so the batch loop's commits do not drop it.

    Yields True when the lock was acquired -- the caller's own
    ``try/finally`` release disappears, this helper releases on exit. Yields
    False when it was not: the skip is already recorded durably
    (``db.record_sync_skip`` -> sync_runs.last_skipped_at/skip_count) and
    logged, and the caller keeps its own control flow (``continue`` /
    ``return`` / ``raise`` -- e.g. the ``SystemExit`` for an explicitly
    targeted repo). A raise from ``_acquire_repo_lock`` itself (DB-level
    problem) propagates unchanged and records nothing: it is not a skip.

    Release outcomes are distinguishable, never silent: released (debug),
    did not hold the lock -- lost mid-run (warning), release statement
    failed (error with traceback, never masking the block's own outcome).

    The lock is taken on *conn* -- the caller's working connection -- and
    no connection is kept alive by this helper beyond the block (issue #373).

    ``acquire``/``release`` default to this module's ``_acquire_repo_lock`` /
    ``_release_repo_lock``. Callers may pass their own (imported) copies so a
    patch on the *caller's* module attribute takes effect -- the pipeline
    passes its module globals, keeping one CM for all call sites (issue #374).
    """
    acquire = acquire or _acquire_repo_lock
    release = release or _release_repo_lock
    held = acquire(conn, repo)
    if not held:
        db.record_sync_skip(conn, repo)
        log.info("%s %s: skipped (sync already running for this repo)", phase, repo)
    try:
        yield held
    finally:
        if held:
            try:
                released = release(conn, repo)
            except Exception:
                log.exception(
                    "%s %s: advisory-lock release statement failed -- the "
                    "lock may still be held by this session",
                    phase, repo,
                )
            else:
                if released:
                    log.debug("%s %s: advisory lock released", phase, repo)
                else:
                    log.warning(
                        "%s %s: advisory lock was NOT held at release time -- "
                        "the lock was lost mid-run (session died?); another "
                        "process could have entered this repo",
                        phase, repo,
                    )


# --- Circuit breaker helpers (issue #345) ---


def _compute_backoff(failures: int, base: float, cap: float) -> float:
    """Exponential backoff: min(cap, base * 2^(failures - 1))."""
    return min(cap, base * (2 ** max(0, failures - 1)))


def _should_skip_repo(
    conn,
    repo: str,
    settings: Settings,
    explicit: bool,
) -> bool:
    """Return True when *repo* should be skipped by the circuit breaker.

    Reads ``consecutive_failures`` and ``last_attempt_at`` from the DB.
    When *explicit* is True (caller passed ``repos=[...]``), a skip raises
    ``ValueError`` instead of silently returning True.

    The backoff cap is chosen per lane from ``settings.dev_repos``
    (issue #371): reference repos get a day-scale cap so the breaker can
    fire on the daily cadence; dev repos keep the one-hour cap exactly.
    """
    # Defensive: settings may be a MagicMock (tests); MagicMock.__int__()
    # returns 1, so we must use isinstance to detect non-real settings.
    # When settings fields are not real numbers, disable the breaker.
    cb_threshold = getattr(settings, "cb_threshold", 0)
    if not isinstance(cb_threshold, (int, float)):
        return False
    if cb_threshold <= 0:
        return False

    base_backoff = getattr(settings, "cb_base_backoff", 60.0)
    if not isinstance(base_backoff, (int, float)):
        return False
    # The backoff cap is chosen per lane (issue #371): dev repos (in
    # settings.dev_repos, ~15-minute cadence) keep the one-hour cap
    # exactly; reference repos (daily cadence) get a day-scale cap so the
    # breaker can actually fire on that lane.
    if repo in (getattr(settings, "dev_repos", ()) or ()):
        max_backoff = getattr(settings, "cb_max_backoff", 3600.0)
    else:
        # Reference lane. Absent/non-numeric reference cap (partial test
        # settings) falls back to the dev cap rather than raising.
        max_backoff = getattr(settings, "cb_ref_max_backoff", None)
        if not isinstance(max_backoff, (int, float)):
            max_backoff = getattr(settings, "cb_max_backoff", 3600.0)
    if not isinstance(max_backoff, (int, float)):
        return False

    threshold = int(cb_threshold)

    failures, last_attempt_at = db.get_sync_attempt(conn, repo)
    if failures < threshold:
        return False

    if last_attempt_at is None:
        return False

    backoff = _compute_backoff(failures, base_backoff, max_backoff)
    now = datetime.now(timezone.utc)
    elapsed = (now - last_attempt_at).total_seconds()

    if elapsed < backoff:
        retry_at = last_attempt_at + timedelta(seconds=backoff)
        if explicit:
            raise ValueError(
                f"Repo {repo} is circuit-broken: {failures} consecutive "
                f"failures, retry after {retry_at.isoformat()}"
            )
        log.warning(
            "Circuit breaker: skipping %s (%d consecutive failures, "
            "retry after %s)",
            repo, failures, retry_at.isoformat(),
        )
        return True

    return False


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


def _order_repos_dev_first(targets: list[str], dev_repos: set[str]) -> list[str]:
    """Stable-sort ``targets``: dev repos first, then ref repos.

    Within each group, original config order is preserved.
    """
    devs = [r for r in targets if r in dev_repos]
    refs = [r for r in targets if r not in dev_repos]
    return devs + refs


def _resolve_backfill_since(
    cli_backfill_since: str | None,
    settings: Settings,
    repo: str,
) -> str | None:
    """Resolve ``backfill_since`` for a single repo.

    CLI flag takes precedence over env default.
    Env default (``settings.ref_backfill_since``) applies to ref repos only.
    """
    if cli_backfill_since is not None:
        return cli_backfill_since
    if repo not in settings.dev_repos:
        return settings.ref_backfill_since
    return None


# ── run_fetch: API/git only, no chunk/embed ──────────────────────────────


def run_fetch(
    settings: Settings | None = None,
    repos: list[str] | None = None,
    backfill_since: str | None = None,
) -> None:
    """Fetch phase: API fetch + git pull only.

    Populates issue_items from GitHub API and ensures git clones are up to
    date.  Does NOT write to chunks.

    Uses ThreadPoolExecutor to fetch multiple repos in parallel (issue #307).
    Per-repo PG advisory lock ensures mutual exclusion for the same repo
    across processes.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    settings = settings or load_settings()
    targets = _validate_repos(repos, settings)
    targets = _order_repos_dev_first(targets, settings.dev_repos)
    provider = build_token_provider(settings)

    # Circuit breaker pre-check: skip repos with too many consecutive
    # failures BEFORE opening a DB connection inside a thread.  This
    # eliminates the connection leak that the old per-thread check had
    # (issue #345).
    explicit_repos = repos is not None
    # Pre-flight (migrate_light + circuit-breaker pre-check) runs on its own
    # short-lived connection.  db.connect() opens with autocommit=False, so
    # any SELECT would leave this connection idle in an open transaction for
    # the whole fetch phase; PostgreSQL would then kill it with
    # idle_in_transaction_session_timeout (issue #373).  connect_scope
    # commits and closes it before the executor starts.
    with db.connect_scope(settings) as _cb_conn:
        # fetch only writes issue_items/sync_state -- never needs heavy
        # indexes, and a full migrate() here would resurrect them mid-drain
        # if a reindex (#352) had dropped them (issue #352).
        schema.migrate_light(_cb_conn, settings)
        _cb_skipped: list[str] = []
        _active_targets: list[str] = []
        for repo in targets:
            if _should_skip_repo(_cb_conn, repo, settings, explicit_repos):
                _cb_skipped.append(repo)
            else:
                _active_targets.append(repo)

    if _cb_skipped:
        log.info(
            "Circuit breaker skipped %d repo(s): %s",
            len(_cb_skipped), ", ".join(_cb_skipped),
        )

    t_total = time.monotonic()
    failed: list[str] = []

    def _fetch_one(repo: str) -> None:
        """Fetch a single repo (docs + issues) in its own DB connection."""
        conn = db.connect(settings)
        schema.migrate_light(conn, settings)
        try:
            # Per-repo PG advisory lock: acquire, skip recording and release
            # all live in repo_lock() (issue #374).
            with repo_lock(conn, repo, phase="fetch") as held:
                if not held:
                    return
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
                    resolved_since = _resolve_backfill_since(backfill_since, settings, repo)
                    n_fetched = fetch_issues(settings, conn, repo, provider, backfill_since=resolved_since)
                    log.info("fetch issues: %d items fetched (%.1fs)",
                             n_fetched, time.monotonic() - t0)
                except Exception as exc:
                    conn.rollback()
                    db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                    log.exception("fetch issues failed for %s", repo)
                    raise

                # Record the success.  The circuit breaker gates *fetch*, so
                # fetch is what has to be able to clear it: run_fetch used to
                # only ever record failures, leaving consecutive_failures and
                # last_attempt_at frozen at the last failure for fetch-only
                # runs (run_ingest resets in its index phase instead).
                db.record_sync_attempt(conn, repo, success=True)
        finally:
            conn.close()

    n_workers = max(1, min(len(_active_targets), settings.fetch_concurrency))
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_fetch_one, repo): repo for repo in _active_targets}
        for future in as_completed(futures):
            repo = futures[future]
            try:
                future.result()
            except Exception:
                failed.append(repo)

    if failed:
        raise RuntimeError(
            f"fetch failed for {len(failed)}/{len(_active_targets)} repo(s): "
            f"{', '.join(failed)}"
        )

    t_total_elapsed = time.monotonic() - t_total
    log.info("total fetch time: %.1fs", t_total_elapsed)


# ── run_index: read issue_items/doc_files, chunk + embed ─────────────────


def run_index(
    settings: Settings | None = None,
    repos: list[str] | None = None,
    rebuild: bool = False,
) -> None:
    """Index phase: read from issue_items / doc_files, chunk + embed, write to chunks.

    Idempotent: running this multiple times against the same data produces
    the same chunks.

    Sequential execution across repos with per-repo PG advisory lock (issue #307).
    """
    settings = settings or load_settings()
    targets = _validate_repos(repos, settings)
    targets = _order_repos_dev_first(targets, settings.dev_repos)
    route = _route()

    # Pre-flight (bulk-path detection, schema prep, destructive bulk ops)
    # runs on its own short-lived connection.  db.connect() opens with
    # autocommit=False, so any SELECT would leave this connection idle in an
    # open transaction while the index loop embeds; PostgreSQL would then
    # kill it with idle_in_transaction_session_timeout (issue #373).
    # connect_scope commits and closes it before the loop starts, and the
    # loop opens a fresh connection below.
    with db.connect_scope(settings) as pre_conn:
        # Issue #376: the pending-volume threshold is scoped to the targeted
        # repos, so targets/settings are passed into the bulk-path decision.
        is_bulk = _is_bulk_path(pre_conn, rebuild, targets, settings)

        if is_bulk:
            log.info("bulk path detected (rebuild=%s), using light schema + deferred indexes", rebuild)
            schema.migrate_light(pre_conn, settings)
        else:
            schema.migrate(pre_conn, settings)

        # Bulk path: destructive operations (no per-repo lock needed at schema level)
        if is_bulk:
            if rebuild:
                log.warning("rebuild: discarding existing index and sync cursors")
                schema.truncate_all_repos(pre_conn)
                pre_conn.commit()
            # Issue #364: only rebuild or a run that intends to cover every
            # configured repo may drop the heavy indexes. A scoped bulk run
            # (e.g. refreshing one dev repo) neither drops nor creates them --
            # during a genuine drain they're already absent, and if they exist
            # the drop is exactly the #364 accident (a scoped run destroying
            # another lane's in-progress or just-finished HNSW build).
            if rebuild or _bulk_covers_all_repos(targets, settings):
                schema.drop_heavy_indexes(pre_conn)
            else:
                log.info(
                    "heavy indexes drop skipped: scoped bulk run (%d/%d repos); "
                    "indexes are left as-is for another lane's drain",
                    len(targets), len(settings.repos),
                )

    conn = db.connect(settings)

    embedder = Embedder()
    buffer: ChunkBuffer | None = None
    if is_bulk:
        buffer = ChunkBuffer(conn, embedder, batch_size=_BULK_BUFFER_SIZE)

    t_total = time.monotonic()
    budget = _index_budget(settings)
    # Report the budget that is actually in force. An unparseable
    # SHIORI_INGEST_TIME_BUDGET degrades to unbounded, which is silent and
    # looks exactly like the unbounded runs this issue exists to end -- so
    # say which of the two this run is, once, up front.
    log.info("index run: time budget = %s", _budget_desc(budget))
    truncated = False
    failed_repos: dict[str, str] = {}
    completed: list[str] = []

    for repo in targets:
        # Time budget (issue #377): between repositories.  An exhausted
        # budget is a normal outcome -- stop, log why, exit 0; the next run
        # resumes via indexed_at.  ``budget.exhausted()`` uses
        # time.monotonic(), so machine-suspend time does not consume it.
        if budget.exhausted():
            log.info(
                "index run: budget exhausted (budget_s=%s), stopping "
                "before repo=%s",
                budget.budget_seconds, repo,
            )
            truncated = True
            break

        log.info("=== index %s ===", repo)

        # Per-repo PG advisory lock: acquire, skip recording and
        # release all live in repo_lock() (issue #374).
        with repo_lock(conn, repo, phase="index") as held:
            if not held:
                continue

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
                    budget=budget,
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

                # Completion is decided from the data (issue #377), not from
                # a flag: count what is still pending for this repo with the
                # same predicate index_issues selects rows with.  Zero
                # pending => fully indexed => record the successful run with
                # finished_at.  Non-zero => the pass was cut short by the
                # budget => record progress only, NO finished_at (the only
                # trustworthy completion signal in the system) -- and keep
                # the repo out of `completed`, so the bulk path's heavy-index
                # gate cannot rebuild HNSW/pgroonga mid-drain.
                pending = db.count_pending_issue_items(conn, repo)
                if pending == 0:
                    finished_at = db.record_sync_run(
                        conn, repo, route, n_docs, n_items, n_code
                    )
                    db.record_sync_attempt(conn, repo, success=True)
                    synced_ts = (
                        finished_at.isoformat() if finished_at is not None else "?"
                    )
                    log.info("indexed at %s (route=%s)", synced_ts, route)
                    completed.append(repo)
                else:
                    db.record_sync_progress(
                        conn, repo, route, pending_count=pending
                    )
                    truncated = True
                    log.info(
                        "index %s: budget-truncated -- %d items still "
                        "pending, progress recorded, no finished_at",
                        repo, pending,
                    )
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
    # Issue #365: gate on repos that actually completed, not the intended
    # target list -- a per-repo advisory-lock skip can leave the coverage
    # check passing on intention while a repo was never re-indexed.  A
    # budget-truncated repo never lands in `completed` (issue #377), so a
    # partially-indexed repo can never trigger the HNSW/pgroonga rebuild
    # mid-drain.
    if is_bulk:
        if _bulk_run_completed_all_repos(completed, settings):
            t0 = time.monotonic()
            schema.create_heavy_indexes(conn, settings)
            t_idx = time.monotonic() - t0
            log.info("heavy indexes created (%.1fs)", t_idx)
        elif _bulk_covers_all_repos(targets, settings):
            skipped = sorted(set(targets) - set(completed))
            log.info(
                "heavy indexes deferred: %d repo(s) not completed during a "
                "run that intended to cover all repos (advisory lock skip, "
                "circuit breaker, or index time budget; %s); rerun once "
                "they are free",
                len(skipped), ", ".join(skipped),
            )
        else:
            log.info(
                "heavy indexes deferred: scoped bulk run (%d/%d repos); "
                "an unscoped `ingest index --all` rebuilds them once the "
                "drain completes",
                len(targets), len(settings.repos),
            )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_type, count(*) FROM chunks GROUP BY 1 ORDER BY 1"
        )
        for st, n in cur.fetchall():
            log.info("chunks[%s] = %d", st, n)

    t_total_elapsed = time.monotonic() - t_total
    log.info("total index time: %.1fs", t_total_elapsed)

    # Run summary (issue #377): fixed key=value shape, greppable.  An
    # exhausted budget is a normal outcome: truncated=1 with a non-zero
    # exit only when a repo actually FAILED below.
    remaining = db.count_pending_issue_items_for_repos(conn, targets)
    log.info(
        "index run summary: route=%s targets=%d completed_repos=%d "
        "truncated=%d remaining_pending=%d elapsed_s=%.1f",
        route, len(targets), len(completed), int(truncated),
        remaining, t_total_elapsed,
    )

    if failed_repos:
        detail = "; ".join(f"{r}: {e}" for r, e in failed_repos.items())
        raise RuntimeError(
            f"index failed for {len(failed_repos)}/{len(targets)} repo(s): {detail}"
        )

    conn.close()


# ── run_reindex: rebuild chunks while preserving fetched raw data (#352) ───


def run_reindex(
    settings: Settings | None = None,
    repos: list[str] | None = None,
) -> None:
    """Rebuild the ``chunks`` table (re-chunk + re-embed) while preserving
    fetched raw data (issue #352).

    Unlike ``--rebuild`` (which truncates ``issue_items``, ``sync_state``,
    ``sync_runs``, and ``repo_index_state`` -- destroying cursors and forcing
    a full, rate-limited re-fetch), ``reindex`` only clears the derived
    ``chunks`` and ``doc_files`` tables:

    - ``chunks`` is truncated/deleted outright -- it holds nothing but
      derived embeddings.
    - ``doc_files`` (a path+sha cache, not the content itself) is deleted so
      every doc/code file re-chunks from the on-disk clone with no network
      fetch.
    - ``issue_items`` rows are kept; only ``indexed_at`` is reset to NULL so
      ``index_issues`` re-embeds them (issue #318's incremental logic
      otherwise re-indexes nothing once chunks are gone).
    - ``sync_state`` (fetch cursors), ``sync_runs``, and ``repo_index_state``
      are untouched -- reindex never re-fetches from GitHub.

    ``repos=None`` reindexes every configured repo (unscoped ``TRUNCATE``).
    ``repos=[...]`` scopes the clear to those repos only.

    After clearing, the heavy indexes (HNSW/pgroonga) are dropped and the
    existing ``index`` phase (``run_index``) runs for the same scope: it
    detects the bulk path automatically via the now-absent HNSW index
    (``_is_bulk_path``) and rebuilds the heavy indexes once, on success.

    A reindex killed mid-drain is resumed with ``shiori ingest index --all``
    -- there is no separate resume mechanism. While the heavy indexes stay
    absent, every ``index``/``run`` invocation (CLI or MCP) keeps deferring
    them, so nothing needs to remember that a reindex was in progress; only
    an invocation that covers every configured repo rebuilds them at its end
    (``_bulk_covers_all_repos``).
    """
    settings = settings or load_settings()
    # Validates --repo against SHIORI_REPOS and requires SHIORI_REPOS to be
    # set when unscoped; the *original* repos (None vs list) is what decides
    # scoping below, so it is not overwritten with the resolved target list.
    _validate_repos(repos, settings)

    conn = db.connect(settings)
    try:
        schema.migrate_light(conn, settings)
        schema.reindex_prepare(conn, repos)
        conn.commit()
        schema.drop_heavy_indexes(conn)
    finally:
        conn.close()

    run_index(settings=settings, repos=repos)


# ── run_ingest (combined fetch + index) — backward compatible ────────────


def run_ingest(
    settings: Settings | None = None,
    repos: list[str] | None = None,
    rebuild: bool = False,
    backfill_since: str | None = None,
) -> None:
    """Combined fetch + index (legacy ingest behavior).

    Fetch phase runs in parallel across repos via ThreadPoolExecutor.
    Index phase runs sequentially (with batch embedding in bulk path).

    Backward-compatible: ``shiori ingest`` (no subcommand) and
    ``shiori ingest run`` both call this function.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

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
    targets = _order_repos_dev_first(targets, settings.dev_repos)

    provider = build_token_provider(settings)
    route = os.environ.get("SHIORI_INGEST_ROUTE", "cli")

    # --- Pre-flight: bulk-path detection, schema prep, destructive bulk
    # ops, and the circuit-breaker pre-check all run on their own
    # short-lived connection (issue #373).  db.connect() opens with
    # autocommit=False, so any SELECT would leave this connection idle in
    # an open transaction for the whole parallel fetch phase; PostgreSQL
    # would then kill it with idle_in_transaction_session_timeout and
    # Phase 2 would fail with IdleInTransactionSessionTimeout.
    # connect_scope commits and closes it before Phase 1, and Phase 2
    # opens a fresh connection.
    with db.connect_scope(settings) as pre_conn:
        # --- Bulk path detection (detect before lock. Handles fresh DB. Issue #72) ---
        # Issue #376: the pending-volume threshold is scoped to the targeted
        # repos, so targets/settings are passed into the bulk-path decision.
        is_bulk = _is_bulk_path(pre_conn, rebuild, targets, settings)

        # --- Schema prep: migrate_light is idempotent, safe outside lock ---
        if is_bulk:
            log.info("bulk path detected (rebuild=%s), using light schema + deferred indexes", rebuild)
            schema.migrate_light(pre_conn, settings)
        else:
            schema.migrate(pre_conn, settings)

        # --- Bulk path: destructive operations (no per-repo lock needed at schema level) ---
        if is_bulk:
            if rebuild:
                log.warning("rebuild: discarding existing index and sync cursors")
                schema.truncate_all_repos(pre_conn)
                pre_conn.commit()
            # Issue #364: only rebuild or a run that intends to cover every
            # configured repo may drop the heavy indexes -- see run_index for
            # the full rationale (same gate, same predicate).
            if rebuild or _bulk_covers_all_repos(targets, settings):
                schema.drop_heavy_indexes(pre_conn)
            else:
                log.info(
                    "heavy indexes drop skipped: scoped bulk run (%d/%d repos); "
                    "indexes are left as-is for another lane's drain",
                    len(targets), len(settings.repos),
                )

        # ========================================================================
        # Phase 1: Parallel fetch (ThreadPoolExecutor)
        # Each repo gets its own DB connection with per-repo PG advisory lock.
        # Failures are recorded per-repo; the process continues for other repos.
        #
        # Circuit breaker pre-check: repos with too many consecutive failures
        # are skipped BEFORE a DB connection is opened inside a thread (issue #345).
        # ========================================================================
        explicit_repos = repos is not None
        _cb_skipped: list[str] = []
        _active_targets: list[str] = []
        for repo in targets:
            if _should_skip_repo(pre_conn, repo, settings, explicit_repos):
                _cb_skipped.append(repo)
            else:
                _active_targets.append(repo)
    if _cb_skipped:
        log.info(
            "Circuit breaker skipped %d repo(s): %s",
            len(_cb_skipped), ", ".join(_cb_skipped),
        )

    fetch_failed: dict[str, str] = {}
    _fetch_failed_lock = threading.Lock()

    def _fetch_one(repo: str) -> None:
        """Fetch a single repo: docs + issues. Runs in a thread.

        On failure, records via record_sync_attempt and adds to
        fetch_failed (thread-safe via _fetch_failed_lock). Never re-raises
        so that other repos' fetches continue.
        """
        conn2 = db.connect(settings)
        try:
            # Per-repo PG advisory lock: acquire, skip recording and release
            # all live in repo_lock() (issue #374).
            with repo_lock(conn2, repo, phase="fetch") as held:
                if not held:
                    return
                log.info("=== fetch %s === (parallel)", repo)

                t0 = time.monotonic()
                try:
                    head = fetch_docs(settings, conn2, repo, provider)
                    if head:
                        log.info("fetch docs: clone refreshed at %s (%.1fs)",
                                 head[:8], time.monotonic() - t0)
                    else:
                        log.warning("fetch docs: clone refresh failed for %s", repo)
                except Exception as exc:
                    conn2.rollback()
                    db.record_sync_attempt(conn2, repo, success=False, error=str(exc))
                    log.exception("fetch docs failed for %s", repo)
                    with _fetch_failed_lock:
                        fetch_failed[repo] = str(exc)
                    return  # swallow: don't abort other repos

                t0 = time.monotonic()
                try:
                    resolved_since = _resolve_backfill_since(backfill_since, settings, repo)
                    n_fetched = fetch_issues(settings, conn2, repo, provider, backfill_since=resolved_since)
                    log.info("fetch issues: %d items fetched (%.1fs)",
                             n_fetched, time.monotonic() - t0)
                except Exception as exc:
                    conn2.rollback()
                    db.record_sync_attempt(conn2, repo, success=False, error=str(exc))
                    log.exception("fetch issues failed for %s", repo)
                    with _fetch_failed_lock:
                        fetch_failed[repo] = str(exc)
                    return  # swallow: don't abort other repos
        finally:
            conn2.close()

    n_workers = max(1, min(len(_active_targets), settings.fetch_concurrency))
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_fetch_one, repo): repo for repo in _active_targets}
        for future in as_completed(futures):
            future.result()  # _fetch_one never re-raises

    if fetch_failed:
        log.warning(
            "fetch failed for %d/%d repo(s): %s",
            len(fetch_failed), len(_active_targets),
            "; ".join(f"{r}: {e}" for r, e in fetch_failed.items()),
        )
        # Bulk path: abort immediately on first fetch failure (backward compat)
        if is_bulk:
            first = next(iter(fetch_failed.items()))
            raise RuntimeError(
                f"sync failed for 1/{len(_active_targets)} repo(s): "
                f"{first[0]}: {first[1]}"
            )

    # ========================================================================
    # Phase 2: Sequential index (with batch embedding via ChunkBuffer)
    # Repos that failed during fetch OR were skipped by CB are excluded.
    # ========================================================================
    # Fresh connection for this phase (issue #373): the pre-flight
    # connection was closed before Phase 1 and nothing may be carried
    # across a phase boundary.
    conn = db.connect(settings)

    embedder = Embedder()

    buffer: ChunkBuffer | None = None
    if is_bulk:
        buffer = ChunkBuffer(conn, embedder, batch_size=_BULK_BUFFER_SIZE)

    t_total = time.monotonic()
    budget = _index_budget(settings)
    log.info("ingest run: index time budget = %s", _budget_desc(budget))
    truncated = False
    index_failed: dict[str, str] = {}
    completed: list[str] = []

    for repo in _active_targets:
        if repo in fetch_failed:
            log.info("index %s: skipped (fetch failed earlier)", repo)
            continue

        # Time budget (issue #377): between repositories.  An exhausted
        # budget is a normal outcome -- stop, log why, exit 0; the next run
        # resumes via indexed_at (see run_index for the full rationale).
        if budget.exhausted():
            log.info(
                "ingest run: budget exhausted (budget_s=%s), stopping "
                "before repo=%s",
                budget.budget_seconds, repo,
            )
            truncated = True
            break

        log.info("=== index %s === (sequential)", repo)

        # Per-repo PG advisory lock for index phase: acquire, skip
        # recording and release all live in repo_lock() (issue #374).
        with repo_lock(conn, repo, phase="index") as held:
            if not held:
                continue

            try:
                # docs: walk + chunk + embed
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

                # issues: read issue_items + chunk + embed
                t0 = time.monotonic()
                n_items = index_issues(
                    settings, conn, embedder, repo,
                    buffer=buffer if is_bulk else None,
                    budget=budget,
                )
                if is_bulk and buffer is not None:
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
                if is_bulk and buffer is not None:
                    n_flushed = buffer.flush()
                    conn.commit()
                    log.info("code flushed: %d chunks", n_flushed)
                t_code = time.monotonic() - t0
                log.info("index code: %d files updated (%.1fs)", n_code, t_code)

                # Completion is decided from the data (issue #377): count
                # what is still pending with the same predicate index_issues
                # selects rows with.  Zero pending => fully indexed =>
                # record the successful run (finished_at).  Non-zero => the
                # pass was cut short by the budget => record progress only,
                # NO finished_at, and keep the repo out of `completed` so
                # the bulk path's heavy-index gate cannot rebuild mid-drain.
                pending = db.count_pending_issue_items(conn, repo)
                if pending == 0:
                    finished_at = db.record_sync_run(
                        conn, repo, route, n_docs, n_items, n_code
                    )
                    db.record_sync_attempt(conn, repo, success=True)
                    synced_ts = (
                        finished_at.isoformat() if finished_at is not None else "?"
                    )
                    log.info("synced at %s (route=%s)", synced_ts, route)
                    completed.append(repo)
                else:
                    db.record_sync_progress(
                        conn, repo, route, pending_count=pending
                    )
                    truncated = True
                    log.info(
                        "index %s: budget-truncated -- %d items still "
                        "pending, progress recorded, no finished_at",
                        repo, pending,
                    )
            except Exception as exc:
                conn.rollback()
                db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                if is_bulk:
                    raise
                index_failed[repo] = str(exc)
                log.exception(
                    "index failed for %s (route=%s), continuing with remaining repos",
                    repo, route,
                )


    # --- Bulk path: create heavy indexes in batch ---
    # Issue #365: gate on repos that actually completed, not the intended
    # target list. A per-repo advisory-lock skip, a circuit-breaker
    # pre-skip (a cb-skipped repo is never in _active_targets, so it can
    # never land in `completed` either), or a budget truncation (a
    # partially-indexed repo is never appended to `completed`, issue #377)
    # can leave the coverage check passing on intention while a repo was
    # never fully re-indexed.
    if is_bulk:
        if _bulk_run_completed_all_repos(completed, settings):
            t0 = time.monotonic()
            schema.create_heavy_indexes(conn, settings)
            t_idx = time.monotonic() - t0
            log.info("heavy indexes created (%.1fs)", t_idx)
        elif _bulk_covers_all_repos(targets, settings):
            skipped = sorted(set(targets) - set(completed))
            log.info(
                "heavy indexes deferred: %d repo(s) not completed during a "
                "run that intended to cover all repos (advisory lock skip, "
                "circuit breaker, or index time budget; %s); rerun once "
                "they are free",
                len(skipped), ", ".join(skipped),
            )
        else:
            log.info(
                "heavy indexes deferred: scoped bulk run (%d/%d repos); "
                "an unscoped `ingest index --all` rebuilds them once the "
                "drain completes",
                len(targets), len(settings.repos),
            )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_type, count(*) FROM chunks GROUP BY 1 ORDER BY 1"
        )
        for st, n in cur.fetchall():
            log.info("chunks[%s] = %d", st, n)

    t_total_elapsed = time.monotonic() - t_total
    log.info("total ingest time: %.1fs", t_total_elapsed)

    # Run summary (issue #377): fixed key=value shape, greppable.  An
    # exhausted budget is a normal outcome: truncated=1 with a non-zero
    # exit only when a repo actually FAILED below.
    remaining = db.count_pending_issue_items_for_repos(conn, targets)
    log.info(
        "ingest run summary: route=%s targets=%d completed_repos=%d "
        "truncated=%d remaining_pending=%d elapsed_s=%.1f",
        route, len(targets), len(completed), int(truncated),
        remaining, t_total_elapsed,
    )

    # Aggregate all failures (fetch + index)
    all_failed: dict[str, str] = {}
    all_failed.update(fetch_failed)
    all_failed.update(index_failed)
    if all_failed:
        detail = "; ".join(f"{r}: {e}" for r, e in all_failed.items())
        raise RuntimeError(
            f"sync failed for {len(all_failed)}/{len(targets)} repo(s): {detail}"
        )

    conn.close()
