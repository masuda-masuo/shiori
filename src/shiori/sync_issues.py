from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
import psycopg

from .api_utils import API, _GitHubAuth, _api_pages, _api_pages_gen
from .chunk_buffer import ChunkBuffer
from .chunking import detect_language, split_issue_text
from .config import IndexBudget, Settings
from .db import (
    PENDING_ISSUE_ITEMS_WHERE,
    delete_chunks_by_key,
    get_cursor,
    insert_chunk,
    set_cursor,
)
from . import db
from .embedding import Embedder
from .github_auth import TokenProvider
from .sync_utils import _clean_text, _is_bot, _should_index

log = logging.getLogger(__name__)

MAX_PR_REVIEW_WORKERS = 10

#: Global ceiling on the PR-review inner level (issue #375): at most this
#: many PR-review workers may hold a DB connection at once, ACROSS ALL repo
#: threads of one ingest process.
#:
#: Each ``_worker`` opens exactly one connection for its whole lifetime, so
#: bounding the workers bounds the inner-level connections.  The worst case
#: for the whole process becomes ``1 (pre-flight / phase 2) +
#: fetch_concurrency (repo threads) + PR_REVIEW_CONNECTION_LIMIT`` = 15 with
#: the defaults -- a sum, instead of the old
#: ``fetch_concurrency * (1 + MAX_PR_REVIEW_WORKERS) + 1`` = 45 product that
#: grew with ``SHIORI_FETCH_CONCURRENCY``.  A single repo's pool is at most
#: ``MAX_PR_REVIEW_WORKERS`` anyway, so a lone repo is unaffected; the cap
#: only binds when several repos nest PR-review workers at once.
PR_REVIEW_CONNECTION_LIMIT = MAX_PR_REVIEW_WORKERS

#: Permits for the ceiling above.  A worker acquires one before opening its
#: connection and returns it after closing, so a worker waiting for a slot
#: holds no connection and no advisory lock -- the level that waits is never
#: the level that holds (issue #375).
_pr_review_connection_slots = threading.BoundedSemaphore(PR_REVIEW_CONNECTION_LIMIT)


@contextmanager
def _pr_review_connection_scope() -> Iterator[None]:
    """Hold one global PR-review connection slot for the duration of the block.

    Structural release (issue #375): the slot is returned by ``__exit__`` no
    matter how the block ends -- success, exception, or ``BaseException`` --
    so a work-path failure can never leak a permit (a leaked permit would
    silently degrade every later run of the unattended daily lane).  The
    block body is the whole worker lifetime: ``db.connect`` opens inside the
    block and ``conn2.close()`` runs before it exits.
    """
    _pr_review_connection_slots.acquire()
    try:
        yield
    finally:
        _pr_review_connection_slots.release()


def _upsert_issue_item(conn: psycopg.Connection, row: dict) -> None:
    # Only issue/PR body rows carry labels; comment/review rows omit the key,
    # but %(labels)s requires it to be present (#165)
    row.setdefault("labels", None)
    with conn.cursor() as cur:
        cur.execute(
            """ INSERT INTO issue_items (
                repo, issue_no, comment_id, kind, title, author, is_bot,
                state, path, line, body, url, created_at, updated_at,
                labels
            ) VALUES (
                %(repo)s, %(issue_no)s, %(comment_id)s, %(kind)s, %(title)s,
                %(author)s, %(is_bot)s, %(state)s, %(path)s, %(line)s,
                %(body)s, %(url)s, %(created_at)s, %(updated_at)s,
                %(labels)s
            )
            ON CONFLICT (repo, issue_no, comment_id) DO UPDATE SET
                kind = EXCLUDED.kind, title = EXCLUDED.title,
                author = EXCLUDED.author, is_bot = EXCLUDED.is_bot,
                state = EXCLUDED.state, path = EXCLUDED.path,
                line = EXCLUDED.line, body = EXCLUDED.body,
                url = EXCLUDED.url, created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                labels = EXCLUDED.labels
            """,
            row,
        )


def _issue_title_state_kind(
    conn: psycopg.Connection, repo: str, issue_no: int
) -> tuple[str | None, str | None, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT title, state, kind FROM issue_items "
            "WHERE repo = %s AND issue_no = %s AND comment_id = 0",
            (repo, issue_no),
        )
        row = cur.fetchone()
    return (row[0], row[1], row[2]) if row else (None, None, None)


def _propagate_issue_states(conn: psycopg.Connection, repo: str) -> None:
    """Propagate issue_items body-row states to chunks (issue #56).

    Single set-based UPDATE joining chunks to the comment_id = 0 body
    rows of issue_items, so the number of round-trips does not scale
    with the number of issues (issue #378).  Uses IS DISTINCT FROM (not
    ``<>``: state is nullable) so a chunk whose state is unchanged is
    not rewritten -- PostgreSQL would otherwise create a new row version
    plus index entries for every chunk on every ingest run.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE chunks AS c
            SET state = b.state
            FROM issue_items AS b
            WHERE b.repo = %s
              AND b.comment_id = 0
              AND c.repo = b.repo
              AND c.issue_no = b.issue_no
              AND c.state IS DISTINCT FROM b.state
            """,
            (repo,),
        )
        if cur.rowcount:
            log.debug(
                "propagated states to %d chunks (repo=%s)",
                cur.rowcount, repo,
            )


def _index_item(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    *,
    chunk_key: str,
    source_type: str,
    repo: str,
    issue_no: int,
    comment_id: int | None,
    kind: str | None,
    title: str | None,
    body: str,
    state: str | None,
    author: str | None,
    path: str | None,
    line: int | None,
    created_at,
    updated_at,
    url: str | None,
    buffer: ChunkBuffer | None = None,
) -> None:
    chunks = split_issue_text(title, body, settings.chunk_max_chars)
    delete_chunks_by_key(conn, chunk_key)
    if not chunks:
        return
    language = detect_language((title or "") + "\n" + (body or ""))
    if buffer is not None:
        for c in chunks:
            buffer.add(
                chunk_key=chunk_key,
                chunk_index=c.chunk_index,
                source_type=source_type,
                repo=repo,
                path=path,
                issue_no=issue_no,
                comment_id=comment_id,
                kind=kind,
                language=language,
                content=c.content,
                state=state,
                author=author,
                line=line,
                created_at=created_at,
                updated_at=updated_at,
                url=url,
            )
    else:
        vectors = embedder.embed_passages([c.content for c in chunks])
        for c, v in zip(chunks, vectors):
            insert_chunk(
                conn,
                chunk_key=chunk_key,
                chunk_index=c.chunk_index,
                source_type=source_type,
                repo=repo,
                path=path,
                issue_no=issue_no,
                comment_id=comment_id,
                kind=kind,
                language=language,
                content=c.content,
                embedding=v,
                state=state,
                author=author,
                line=line,
                created_at=created_at,
                updated_at=updated_at,
                url=url,
            )


def _sync_pr_reviews(
    client: httpx.Client,
    conn: psycopg.Connection,
    embedder: Embedder | None,
    settings: Settings,
    repo: str,
    issue_no: int,
    *,
    do_index: bool = True,
    buffer: ChunkBuffer | None = None,
) -> None:
    """Sync PR review submissions from pulls/{issue_no}/reviews (issue #103).

    Review submissions (body + state like COMMENTED/APPROVED/CHANGES_REQUESTED)
    are not returned by pulls/comments (which only has inline review comments).
    Store with comment_id = -(review_id) to avoid collision with inline reviews.

    When do_index=False (fetch-only mode), only upserts into issue_items
    without chunking/embedding.
    """
    try:
        reviews = _api_pages(
            client,
            f"{API}/repos/{repo}/pulls/{issue_no}/reviews",
            {"per_page": 100},
        )
    except httpx.HTTPError as exc:
        log.info("PR #%d: could not fetch reviews: %s", issue_no, exc)
        return

    if not reviews:
        return

    pr_state = _issue_title_state_kind(conn, repo, issue_no)[1]

    for r in reviews:
        rid = r["id"]
        author = (r.get("user") or {}).get("login")
        is_bot = _is_bot(r.get("user"))
        state = r.get("state")
        body = _clean_text(r.get("body") or "")
        submitted_at = r.get("submitted_at")

        _upsert_issue_item(conn, {
            "repo": repo,
            "issue_no": issue_no,
            "comment_id": -rid,
            "kind": "pr_review",
            "title": None,
            "author": author,
            "is_bot": is_bot,
            "state": state,
            "path": None,
            "line": None,
            "body": body,
            "url": r.get("html_url"),
            "created_at": submitted_at,
            "updated_at": submitted_at,
        })

        if do_index and body and _should_index(is_bot, author, settings):
            assert embedder is not None  # guaranteed by do_index=True
            _index_item(
                settings, conn, embedder,
                chunk_key=f"pr_review_submission:{repo}:{issue_no}:r{rid}",
                source_type="pr_review",
                repo=repo, issue_no=issue_no, comment_id=-rid,
                kind="pr",
                title=None,
                body=f"[{state}] {body}" if state else body,
                state=pr_state, author=author,
                path=None, line=None,
                created_at=submitted_at, updated_at=submitted_at,
                url=r.get("html_url"),
                buffer=buffer,
            )


def _split_into_chunks(items: list[int], n: int) -> list[list[int]]:
    """Split *items* into *n* roughly equal chunks."""
    chunk_size = max(1, len(items) // max(1, n))
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def _fetch_pr_reviews_parallel(
    settings: Settings,
    repo: str,
    provider: TokenProvider,
    pr_numbers: list[int],
) -> int:
    """Fetch PR review submissions for multiple PRs in parallel.

    Uses ThreadPoolExecutor to parallelize per-PR API calls (issue #308).
    PRs are split into chunks (one per worker); each worker shares one
    httpx.Client and one DB connection across its chunk to avoid per-PR
    connection churn.

    Returns the number of PRs for which reviews were successfully fetched.
    Per-PR failures are logged as warnings and do not abort the overall fetch.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    n_workers = min(MAX_PR_REVIEW_WORKERS, max(1, len(pr_numbers)))
    n = 0

    def _worker(chunk: list[int]) -> int:
        count = 0
        # Global connection ceiling (issue #375): the slot is taken before
        # the connection opens and returned after it closes, so an exception
        # on the work path cannot leak a permit -- and a worker waiting for
        # a slot holds no connection and no advisory lock (those live on the
        # repo thread's own connection).
        with _pr_review_connection_scope():
            conn2 = db.connect(settings)
            try:
                with httpx.Client(headers=headers, auth=_GitHubAuth(provider), timeout=30.0, follow_redirects=True) as cl:
                    for no in chunk:
                        try:
                            _sync_pr_reviews(
                                cl, conn2, embedder=None, settings=settings,
                                repo=repo, issue_no=no,
                                do_index=False,
                            )
                            count += 1
                        except Exception as exc:
                            log.warning(
                                "PR #%d review fetch failed, continuing: %s", no, exc,
                            )
                conn2.commit()
            finally:
                conn2.close()
        return count

    chunks = _split_into_chunks(pr_numbers, n_workers)
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_worker, c): c[0] for c in chunks if c}
        for future in as_completed(futures):
            try:
                n += future.result()
            except Exception as exc:
                log.warning("PR review batch fetch failed: %s", exc)
    return n


# ── Fetch (API only, no chunk/embed) ──────────────────────────────────────


def _fetch_dormant_open_bodies(
    client: httpx.Client,
    conn: psycopg.Connection,
    repo: str,
    max_wait: float = 60.0,
    max_retries: int = 3,
) -> int:
    """One-time fetch of ``state=open`` issues/PRs WITHOUT ``since`` filter.

    Upserts **body rows only** (``comment_id=0``) into ``issue_items``.
    Does NOT advance any cursor -- the normal streams own cursors.

    This catches open issues/PRs whose ``updated_at`` predates the backfill
    seed date (dormant open items that the ``since``-filtered stream skips).
    Only a few percent of total items, so API cost is negligible.
    """
    n = 0
    params = {
        "state": "open",
        "per_page": 100,
    }
    for page in _api_pages_gen(client, f"{API}/repos/{repo}/issues", params,
                               repo=repo, max_wait=max_wait,
                               max_retries=max_retries):
        if not page:
            break
        for it in page:
            no = it["number"]
            kind = "pr" if "pull_request" in it else "issue"
            author = (it.get("user") or {}).get("login")
            row = {
                "repo": repo,
                "issue_no": no,
                "comment_id": 0,
                "kind": kind,
                "title": _clean_text(it.get("title")),
                "author": author,
                "is_bot": _is_bot(it.get("user")),
                "state": it.get("state"),
                "path": None,
                "line": None,
                "body": _clean_text(it.get("body") or ""),
                "url": it.get("html_url"),
                "created_at": it.get("created_at"),
                "updated_at": it.get("updated_at"),
                "labels": [label["name"] for label in it.get("labels", [])],
            }
            _upsert_issue_item(conn, row)
            n += 1
        conn.commit()
    log.info("dormant open bodies fetched for %s: %d", repo, n)
    return n


def fetch_issues(
    settings: Settings,
    conn: psycopg.Connection,
    repo: str,
    provider: TokenProvider,
    *,
    skip_pr_reviews: bool | None = None,
    backfill_since: str | None = None,
) -> int:
    """Fetch issues/PRs/comments/reviews from GitHub API and upsert into issue_items.

    Does NOT write to chunks — only populates issue_items and advances sync cursors.
    Returns the number of items fetched (not necessarily indexed).

    When skip_pr_reviews is True, PR review submissions are not fetched for
    non-dev repos (prevents blocking on large reference repos).
    When False, PR reviews are always fetched.
    When None (default), skips for non-dev repos
    (same as original sync_issues guard).
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    n_fetched = 0
    # --- Backfill seeding: seed cursors when repo has none (first fetch) ---
    was_seeded = False
    if backfill_since:
        cur_issues = get_cursor(conn, repo, "issues")
        if cur_issues is None:
            for _kind in ("issues", "issue_comments", "pr_review_comments"):
                set_cursor(conn, repo, _kind, backfill_since)
            was_seeded = True

    with httpx.Client(
        headers=headers, auth=_GitHubAuth(provider), timeout=30.0, follow_redirects=True,
    ) as client:
        # --- Body (issues endpoint includes PRs) ---
        since = get_cursor(conn, repo, "issues")
        params = {
            "state": "all",
            "sort": "updated",
            "direction": "asc",
            "per_page": 100,
        }
        if since:
            params["since"] = since
        pr_numbers: list[int] = []
        _had_issues_page = False
        for page in _api_pages_gen(client, f"{API}/repos/{repo}/issues", params,
                                  not_found_ok=True, repo=repo,
                                  max_wait=settings.rate_limit_max_wait,
                                  max_retries=settings.rate_limit_max_retries):
            _had_issues_page = True
            if not page:
                break
            for it in page:
                no = it["number"]
                kind = "pr" if "pull_request" in it else "issue"
                if kind == "pr":
                    pr_numbers.append(no)
                author = (it.get("user") or {}).get("login")
                row = {
                    "repo": repo,
                    "issue_no": no,
                    "comment_id": 0,
                    "kind": kind,
                    "title": _clean_text(it.get("title")),
                    "author": author,
                    "is_bot": _is_bot(it.get("user")),
                    "state": it.get("state"),
                    "path": None,
                    "line": None,
                    "body": _clean_text(it.get("body") or ""),
                    "url": it.get("html_url"),
                    "created_at": it.get("created_at"),
                    "updated_at": it.get("updated_at"),
                    "labels": [label["name"] for label in it.get("labels", [])],
                }
                _upsert_issue_item(conn, row)
                n_fetched += 1
            conn.commit()
            set_cursor(conn, repo, "issues", page[-1]["updated_at"])
        if not _had_issues_page:
            log.warning("Issues API returned 404 for %s — issues disabled, skipping", repo)

        if get_cursor(conn, repo, "issues") is None:
            set_cursor(conn, repo, "issues", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

        # --- Issue/PR comments ---
        since = get_cursor(conn, repo, "issue_comments")
        params = {"sort": "updated", "direction": "asc", "per_page": 100}
        if since:
            params["since"] = since
        _any_issue_comments = False
        for page in _api_pages_gen(client, f"{API}/repos/{repo}/issues/comments", params,
                                  not_found_ok=True, repo=repo,
                                  max_wait=settings.rate_limit_max_wait,
                                  max_retries=settings.rate_limit_max_retries):
            _any_issue_comments = True
            if not page:
                break
            for c in page:
                no = int(c["issue_url"].rstrip("/").rsplit("/", 1)[-1])
                title, state, issue_kind = _issue_title_state_kind(conn, repo, no)
                author = (c.get("user") or {}).get("login")
                _upsert_issue_item(conn, {
                    "repo": repo, "issue_no": no, "comment_id": c["id"],
                    "kind": "comment", "title": None, "author": author,
                    "is_bot": _is_bot(c.get("user")), "state": state, "path": None, "line": None,
                    "body": _clean_text(c.get("body") or ""), "url": c.get("html_url"),
                    "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
                })
                n_fetched += 1
            conn.commit()
            set_cursor(conn, repo, "issue_comments", page[-1]["updated_at"])

        if not _any_issue_comments or get_cursor(conn, repo, "issue_comments") is None:
            set_cursor(conn, repo, "issue_comments", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

        # --- PR review comments (with path/line/diff_hunk) ---
        since = get_cursor(conn, repo, "pr_review_comments")
        params = {"sort": "updated", "direction": "asc", "per_page": 100}
        if since:
            params["since"] = since
        _any_pr_review_comments = False
        for page in _api_pages_gen(client, f"{API}/repos/{repo}/pulls/comments", params,
                                  not_found_ok=True, repo=repo,
                                  max_wait=settings.rate_limit_max_wait,
                                  max_retries=settings.rate_limit_max_retries):
            _any_pr_review_comments = True
            if not page:
                break
            for c in page:
                no = int(c["pull_request_url"].rstrip("/").rsplit("/", 1)[-1])
                title, state, _ = _issue_title_state_kind(conn, repo, no)
                author = (c.get("user") or {}).get("login")
                line = c.get("line") or c.get("original_line")
                body = _clean_text(c.get("body") or "")
                diff_hunk = c.get("diff_hunk")
                if diff_hunk:
                    body = f"{body}\n\n```diff\n{_clean_text(diff_hunk)}\n```"
                _upsert_issue_item(conn, {
                    "repo": repo, "issue_no": no, "comment_id": c["id"],
                    "kind": "pr_review_comment", "title": None, "author": author,
                    "is_bot": _is_bot(c.get("user")), "state": state,
                    "path": c.get("path"), "line": line,
                    "body": body, "url": c.get("html_url"),
                    "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
                })
                n_fetched += 1
            conn.commit()
            set_cursor(conn, repo, "pr_review_comments", page[-1]["updated_at"])

        if not _any_pr_review_comments or get_cursor(conn, repo, "pr_review_comments") is None:
            set_cursor(conn, repo, "pr_review_comments", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

        # --- PR review submissions (fetch only, no indexing) ---
        # Apply guard: skip PR reviews for non-dev repos unless explicitly
        # requested.  skip_pr_reviews=None (default) skips for non-dev repos;
        # skip_pr_reviews=False always fetches; skip_pr_reviews=True skips.
        _skip_reviews = skip_pr_reviews
        if _skip_reviews is None:
            _skip_reviews = repo not in settings.dev_repos
        if not _skip_reviews and pr_numbers:
            n_fetched += _fetch_pr_reviews_parallel(
                settings, repo, provider, pr_numbers,
            )

        # --- One-time state=open pass for seeded repos ---
        if was_seeded:
            _fetch_dormant_open_bodies(
                client, conn, repo,
                max_wait=settings.rate_limit_max_wait,
                max_retries=settings.rate_limit_max_retries,
            )

    return n_fetched


# ── Index (read issue_items, chunk + embed) ───────────────────────────────


BATCH_INDEX_SIZE = 200


def index_issues(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    repo: str,
    buffer: ChunkBuffer | None = None,
    budget: IndexBudget | None = None,
) -> int:
    """Incremental index of issue_items for *repo*.

    Only (re)indexes rows where ``indexed_at IS NULL`` or
    ``updated_at > indexed_at``.  Idempotent: running this multiple
    times against the same issue_items produces the same chunks.

    **Durability invariant**: chunks are always committed *before*
    ``indexed_at`` is set, so a killed index run resumes correctly.

    ``budget`` (issue #377): when given, the per-repo issue loop stops at
    the next batch boundary once ``budget.exhausted()`` -- the batch
    boundary is already the durability boundary (chunks committed before
    ``indexed_at``), so stopping there is safe by construction; the next
    run resumes via ``indexed_at``.  ``None`` (the default) means
    unbounded: the pre-#377 behaviour, unchanged.  A budget-truncated stop
    is a normal outcome: the batch commits, a stop line is logged, and the
    remaining rows are left pending (the caller counts them and records
    progress without ``finished_at``).

    Every committed batch also advances ``sync_runs.last_progress_at``
    (the liveness heartbeat: a DB reader can tell a grinding run from a
    wedged one without docker stats).

    Returns the number of items indexed.
    """
    # --- Step 1: select only items that need (re)indexing ---
    # PENDING_ISSUE_ITEMS_WHERE is the single definition of "pending" --
    # the remaining-work counters (db.count_pending_issue_items) use the
    # same constant, so selection and counting cannot disagree (issue #377).
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT issue_no, comment_id, kind, title, author, is_bot,
                      state, path, line, body, url, created_at, updated_at
               FROM issue_items
               WHERE repo = %s
                 AND {PENDING_ISSUE_ITEMS_WHERE}
               ORDER BY issue_no, comment_id""",
            (repo,),
        )
        rows = cur.fetchall()

    if not rows:
        # Nothing to index -- propagate states and stop before reading the
        # body rows, which are only needed by the indexing loop below.
        # In the steady state this is the whole of the work (issue #378).
        _propagate_issue_states(conn, repo)
        return 0

    # --- Step 2: fetch ALL body rows for the indexing loop ---
    # This must see every body row regardless of indexed_at so that an
    # item being reindexed picks up its parent issue's kind and state
    # even when the body row itself is unchanged (outcome 6).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT issue_no, kind, state FROM issue_items "
            "WHERE repo = %s AND comment_id = 0",
            (repo,),
        )
        body_rows = cur.fetchall()

    issue_bodies: dict[int, tuple[str | None, str | None]] = {}
    for r in body_rows:
        issue_bodies[r[0]] = (r[1], r[2])  # kind, state from body row

    n_indexed = 0
    n_batches = (len(rows) + BATCH_INDEX_SIZE - 1) // BATCH_INDEX_SIZE

    # Process in batches to maintain the durability invariant
    for i in range(0, len(rows), BATCH_INDEX_SIZE):
        # Budget check at the batch boundary (issue #377): stopping here is
        # safe by construction -- the previous batch's chunks and indexed_at
        # are already committed, so the next run resumes via indexed_at.
        if budget is not None and budget.exhausted():
            log.info(
                "index issues repo=%s batch=%d/%d stopped_by_budget=1 "
                "remaining=%d",
                repo, i // BATCH_INDEX_SIZE + 1, n_batches, len(rows) - i,
            )
            break
        batch = rows[i:i + BATCH_INDEX_SIZE]
        batch_keys: list[tuple[int, int]] = []

        for r in batch:
            issue_no = r[0]
            comment_id = r[1]
            kind = r[2]   # item kind: "issue"/"pr"/"comment"/"pr_review"/"pr_review_comment"
            title = r[3]
            author = r[4]
            is_bot = r[5]
            item_state = r[6]
            path = r[7]
            line = r[8]
            body = r[9] or ""
            url = r[10]
            created_at = r[11]
            updated_at = r[12]

            # Determine the issue kind (for issues/comments, use the stored kind;
            # for PR reviews, use "pr")
            if kind == "pr_review" or kind == "pr_review_comment":
                issue_kind = "pr"
            elif kind == "comment":
                # For comments, get the issue kind from the body row
                issue_kind = (issue_bodies.get(issue_no) or (None, None))[0]
            else:
                issue_kind = kind  # "issue" or "pr"

            # Determine the state to use (use the body's state for consistency)
            state = (issue_bodies.get(issue_no) or (None, None))[1] or item_state

            # Determine chunk_key based on item type
            if comment_id == 0:
                chunk_key = f"issue:{repo}:{issue_no}:body"
                source_type = "issue"
            elif kind == "comment":
                chunk_key = f"issue:{repo}:{issue_no}:c{comment_id}"
                source_type = "issue"
            elif kind == "pr_review":
                # comment_id is negative for PR review submissions
                chunk_key = f"pr_review_submission:{repo}:{issue_no}:r{-comment_id}"
                source_type = "pr_review"
                # PR review submissions have body with [state] prefix handled at fetch time
            elif kind == "pr_review_comment":
                chunk_key = f"pr_review:{repo}:{issue_no}:rc{comment_id}"
                source_type = "pr_review"
            else:
                log.warning("index_issues: unknown kind=%s for %s issue_no=%d comment_id=%d",
                            kind, repo, issue_no, comment_id)
                continue

            if not is_bot or _should_index(is_bot, author, settings):
                _index_item(
                    settings, conn, embedder,
                    chunk_key=chunk_key,
                    source_type=source_type,
                    repo=repo, issue_no=issue_no, comment_id=comment_id if comment_id != 0 else None,
                    kind=issue_kind,
                    title=title, body=body,
                    state=state, author=author,
                    path=path, line=line,
                    created_at=created_at, updated_at=updated_at,
                    url=url,
                    buffer=buffer,
                )
                n_indexed += 1

            batch_keys.append((issue_no, comment_id))

        # ---- Durability invariant: chunks first, then indexed_at ----
        # Flush buffer (commits chunks) or commit non-buffer inserts.
        if buffer is not None:
            buffer.flush()
        else:
            conn.commit()

        # Set indexed_at for this batch (separate transaction after chunks are committed)
        with conn.cursor() as cur:
            for issue_no_pk, comment_id_pk in batch_keys:
                cur.execute(
                    "UPDATE issue_items SET indexed_at = now() "
                    "WHERE repo = %s AND issue_no = %s AND comment_id = %s",
                    (repo, issue_no_pk, comment_id_pk),
                )
        conn.commit()

        # Liveness heartbeat (issue #377): committed right after the batch,
        # so a caller who can read the DB sees this repo advance batch by
        # batch -- grinding is distinguishable from wedged without docker
        # stats (which misreported a DB-blocked-but-working run as idle).
        db.touch_sync_progress(conn, repo)
        log.info(
            "index issues repo=%s batch=%d/%d items=%d",
            repo, i // BATCH_INDEX_SIZE + 1, n_batches, len(batch),
        )

    # Propagate states to chunks (over ALL body rows, not just indexed)
    _propagate_issue_states(conn, repo)

    return n_indexed


# ── Combined (fetch + index) — backward compatible ───────────────────────


def sync_issues(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    repo: str,
    provider: TokenProvider,
    buffer: ChunkBuffer | None = None,
) -> int:
    """Incremental sync of issues/PRs/comments/reviews.
    When buffer specified (bulk path), uses ChunkBuffer for batch embedding.

    This is the combined path: fetch + index.
    """
    # Original guard: skip PR reviews for non-dev repos unless bulk
    skip_pr = buffer is None and repo not in settings.dev_repos
    fetch_issues(settings, conn, repo, provider, skip_pr_reviews=skip_pr)
    return index_issues(settings, conn, embedder, repo, buffer=buffer)
