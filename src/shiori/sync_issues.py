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
    embedder: Embedder,
    settings: Settings,
    repo: str,
    issue_no: int,
    buffer: ChunkBuffer | None = None,
) -> None:
    """Sync PR review submissions from pulls/{issue_no}/reviews (issue #103).

    Review submissions (body + state like COMMENTED/APPROVED/CHANGES_REQUESTED)
    are not returned by pulls/comments (which only has inline review comments).
    Store with comment_id = -(review_id) to avoid collision with inline reviews.
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

        if body and _should_index(is_bot, author, settings):
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


def sync_issues(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    repo: str,
    provider: TokenProvider,
    buffer: ChunkBuffer | None = None,
) -> int:
    """Incremental sync of issues/PRs/comments/reviews.
    When buffer specified (bulk path), uses ChunkBuffer for batch embedding."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    n_indexed = 0

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
                title = _clean_text(it.get("title"))
                body = _clean_text(it.get("body") or "")
                row = {
                    "repo": repo,
                    "issue_no": no,
                    "comment_id": 0,
                    "kind": kind,
                    "title": title,
                    "author": author,
                    "is_bot": _is_bot(it.get("user")),
                    "state": it.get("state"),
                    "path": None,
                    "line": None,
                    "body": body,
                    "url": it.get("html_url"),
                    "created_at": it.get("created_at"),
                    "updated_at": it.get("updated_at"),
                }
                _upsert_issue_item(conn, row)
                _propagate_issue_state(conn, repo, no, it.get("state"))
                if _should_index(row["is_bot"], author, settings):
                    _index_item(
                        settings, conn, embedder,
                        chunk_key=f"issue:{repo}:{no}:body",
                        source_type="issue",
                        repo=repo, issue_no=no, comment_id=None,
                        kind=kind,
                        title=title, body=body,
                        state=it.get("state"), author=author,
                        path=None, line=None,
                        created_at=it.get("created_at"),
                        updated_at=it.get("updated_at"),
                        url=it.get("html_url"),
                        buffer=buffer,
                    )
                    n_indexed += 1
                # Sync review submissions for PRs (only dev repos during diff sync;
                # all repos during bulk/rebuild).  Skips ref repos on diff sync
                # because per-PR sequential API calls block the issues cursor
                # on large repos (e.g. opencode: 113K issues).
                if kind == "pr" and (buffer is not None or repo in settings.dev_repos):
                    _sync_pr_reviews(client, conn, embedder, settings, repo, no, buffer=buffer)
            if buffer is None:
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
                is_bot = _is_bot(c.get("user"))
                body = _clean_text(c.get("body") or "")
                _upsert_issue_item(conn, {
                    "repo": repo, "issue_no": no, "comment_id": c["id"],
                    "kind": "comment", "title": None, "author": author,
                    "is_bot": is_bot, "state": state, "path": None, "line": None,
                    "body": body, "url": c.get("html_url"),
                    "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
                })
                if _should_index(is_bot, author, settings):
                    _index_item(
                        settings, conn, embedder,
                        chunk_key=f"issue:{repo}:{no}:c{c['id']}",
                        source_type="issue",
                        repo=repo, issue_no=no, comment_id=c["id"],
                        kind=issue_kind,
                        title=title, body=body,
                        state=state, author=author, path=None, line=None,
                        created_at=c.get("created_at"), updated_at=c.get("updated_at"),
                        url=c.get("html_url"),
                        buffer=buffer,
                    )
                    n_indexed += 1
            if buffer is None:
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
                is_bot = _is_bot(c.get("user"))
                line = c.get("line") or c.get("original_line")
                # Append diff_hunk to body as context (within decision not to index diffs themselves)
                body = _clean_text(c.get("body") or "")
                diff_hunk = c.get("diff_hunk")
                if diff_hunk:
                    body = f"{body}\n\n```diff\n{_clean_text(diff_hunk)}\n```"
                _upsert_issue_item(conn, {
                    "repo": repo, "issue_no": no, "comment_id": c["id"],
                    "kind": "pr_review_comment", "title": None, "author": author,
                    "is_bot": is_bot, "state": state,
                    "path": c.get("path"), "line": line,
                    "body": body, "url": c.get("html_url"),
                    "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
                })
                if _should_index(is_bot, author, settings):
                    _index_item(
                        settings, conn, embedder,
                        chunk_key=f"pr_review:{repo}:{no}:rc{c['id']}",
                        source_type="pr_review",
                        repo=repo, issue_no=no, comment_id=c["id"],
                        kind="pr",
                        title=title, body=body,
                        state=state, author=author,
                        path=c.get("path"), line=line,
                        created_at=c.get("created_at"), updated_at=c.get("updated_at"),
                        url=c.get("html_url"),
                        buffer=buffer,
                    )
                    n_indexed += 1
            if buffer is None:
                conn.commit()
            set_cursor(conn, repo, "pr_review_comments", page[-1]["updated_at"])

        if not _any_pr_review_comments or get_cursor(conn, repo, "pr_review_comments") is None:
            set_cursor(conn, repo, "pr_review_comments", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    return n_indexed
