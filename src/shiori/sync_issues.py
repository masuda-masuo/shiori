from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
import psycopg

from .api_utils import API, _GitHubAuth, _api_pages, _api_pages_gen
from .chunk_buffer import ChunkBuffer
from .chunking import detect_language, split_issue_text
from .config import Settings
from .db import delete_chunks_by_key, get_cursor, insert_chunk, set_cursor
from .embedding import Embedder
from .github_auth import TokenProvider
from .sync_utils import _clean_text, _is_bot, _should_index

log = logging.getLogger(__name__)


def _upsert_issue_item(conn: psycopg.Connection, row: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """ INSERT INTO issue_items (
                repo, issue_no, comment_id, kind, title, author, is_bot,
                state, path, line, body, url, created_at, updated_at
            ) VALUES (
                %(repo)s, %(issue_no)s, %(comment_id)s, %(kind)s, %(title)s,
                %(author)s, %(is_bot)s, %(state)s, %(path)s, %(line)s,
                %(body)s, %(url)s, %(created_at)s, %(updated_at)s
            )
            ON CONFLICT (repo, issue_no, comment_id) DO UPDATE SET
                kind = EXCLUDED.kind, title = EXCLUDED.title,
                author = EXCLUDED.author, is_bot = EXCLUDED.is_bot,
                state = EXCLUDED.state, path = EXCLUDED.path,
                line = EXCLUDED.line, body = EXCLUDED.body,
                url = EXCLUDED.url, created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at
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


def _propagate_issue_state(
    conn: psycopg.Connection, repo: str, issue_no: int, state: str | None
) -> None:
    """Propagate issue_items state changes to chunks (issue #56).
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE chunks SET state = %s WHERE repo = %s AND issue_no = %s",
            (state, repo, issue_no),
        )
        if cur.rowcount:
            log.debug(
                "propagated state=%s to %d chunks (repo=%s, issue_no=%d)",
                state, cur.rowcount, repo, issue_no,
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


# ── Fetch (API only, no chunk/embed) ──────────────────────────────────────


def fetch_issues(
    settings: Settings,
    conn: psycopg.Connection,
    repo: str,
    provider: TokenProvider,
    *,
    skip_pr_reviews: bool | None = None,
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

    with httpx.Client(
        headers=headers, auth=_GitHubAuth(provider), timeout=30.0
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
        for page in _api_pages_gen(client, f"{API}/repos/{repo}/issues", params):
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
                }
                _upsert_issue_item(conn, row)
                n_fetched += 1
            conn.commit()
            set_cursor(conn, repo, "issues", page[-1]["updated_at"])

        if get_cursor(conn, repo, "issues") is None:
            set_cursor(conn, repo, "issues", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

        # --- Issue/PR comments ---
        since = get_cursor(conn, repo, "issue_comments")
        params = {"sort": "updated", "direction": "asc", "per_page": 100}
        if since:
            params["since"] = since
        _any_issue_comments = False
        for page in _api_pages_gen(client, f"{API}/repos/{repo}/issues/comments", params, not_found_ok=True):
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
        for page in _api_pages_gen(client, f"{API}/repos/{repo}/pulls/comments", params, not_found_ok=True):
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
        if not _skip_reviews:
            # Re-read issues to find PRs (cursors already advanced above)
            pr_cursor = get_cursor(conn, repo, "issues")
            params_pr = {
                "state": "all",
                "sort": "updated",
                "direction": "asc",
                "per_page": 100,
            }
            if pr_cursor:
                params_pr["since"] = pr_cursor
            for page in _api_pages_gen(client, f"{API}/repos/{repo}/issues", params_pr):
                if not page:
                    break
                for it in page:
                    if "pull_request" not in it:
                        continue
                    no = it["number"]
                    _sync_pr_reviews(
                        client, conn, embedder=None, settings=settings,
                        repo=repo, issue_no=no,
                        do_index=False,  # fetch-only
                    )
                    n_fetched += 1
                conn.commit()

    return n_fetched


# ── Index (read issue_items, chunk + embed) ───────────────────────────────


def index_issues(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    repo: str,
    buffer: ChunkBuffer | None = None,
) -> int:
    """Read issue_items for *repo* and re-generate chunks.

    Idempotent: running this multiple times against the same issue_items
    produces the same chunks.
    Returns the number of items indexed.
    """
    # Read all issue_items for the repo, ordered by issue_no, comment_id
    with conn.cursor() as cur:
        cur.execute(
            """SELECT issue_no, comment_id, kind, title, author, is_bot,
                      state, path, line, body, url, created_at, updated_at
               FROM issue_items
               WHERE repo = %s
               ORDER BY issue_no, comment_id""",
            (repo,),
        )
        rows = cur.fetchall()

    if not rows:
        return 0

    # Collect issue body kinds and states for reference
    # issue_no -> (kind, state) from the body row (comment_id=0)
    issue_bodies: dict[int, tuple[str | None, str | None]] = {}
    for r in rows:
        if r[1] == 0:  # comment_id == 0 => body row
            issue_bodies[r[0]] = (r[3] if r[3] else r[2],
                                  r[6])  # title or kind as kind, state

    n_indexed = 0
    for r in rows:
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

    # Propagate states to chunks
    for issue_no, (_, st) in issue_bodies.items():
        _propagate_issue_state(conn, repo, issue_no, st)

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
