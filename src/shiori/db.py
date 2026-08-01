"""DB connection (detailed design/04).
docs/issue/pr_review/code share a single DB.
pgvector for embedding queries.
pgroonga for JP/EN full-text search (TokenMecab/Mecab preferred).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import psycopg

from .config import Settings

log = logging.getLogger(__name__)


def connect(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(settings.database_url, autocommit=False)


@contextmanager
def connect_scope(settings: Settings) -> Iterator[psycopg.Connection]:
    """Short-lived DB connection that cannot leak across a phase boundary (issue #373).

    ``connect()`` opens with ``autocommit=False``, so even a single SELECT
    leaves the connection in an open transaction.  A pre-flight connection
    carried across a long phase (parallel fetch, embedding) would sit idle
    in that transaction until PostgreSQL kills it
    (``idle_in_transaction_session_timeout``); the next phase then fails
    with ``IdleInTransactionSessionTimeout``.

    Use this for pre-flight work (bulk-path detection, schema prep,
    circuit-breaker pre-check): the transaction is committed on success,
    rolled back on exception (which is re-raised), and the connection is
    always closed before the ``with`` block exits.  The long phase then
    begins with no leftover connection and opens its own.
    """
    conn = connect(settings)
    try:
        yield conn
    except BaseException:
        # rollback() can itself raise (e.g. the server already killed this
        # backend). Closing lives in finally so that failure cannot leak the
        # connection -- which is the single thing this helper exists to prevent.
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        conn.close()


def get_cursor(conn: psycopg.Connection, repo: str, kind: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cursor FROM sync_state WHERE repo = %s AND kind = %s", (repo, kind)
        )
        row = cur.fetchone()
    return row[0] if row else None


def set_cursor(conn: psycopg.Connection, repo: str, kind: str, cursor: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_state (repo, kind, cursor) VALUES (%s, %s, %s)
            ON CONFLICT (repo, kind) DO UPDATE SET cursor = EXCLUDED.cursor
            """,
            (repo, kind, cursor),
        )
    conn.commit()


def get_cursors(conn: psycopg.Connection, repo: str) -> dict[str, str | None]:
    with conn.cursor() as cur:
        cur.execute("SELECT kind, cursor FROM sync_state WHERE repo = %s", (repo,))
        return dict(cur.fetchall())


def record_sync_run(
    conn: psycopg.Connection,
    repo: str,
    route: str,
    docs_updated: int,
    issues_indexed: int,
    code_indexed: int = 0,
):
    """Record sync success per repo and return completion timestamp (DB's now()) (issue #22 / #33).
    Skipped executions (advisory lock) not recorded. Uses DB now() for cross-path consistency.

    Also clears attempt-tracking fields (last_error, consecutive_failures) since a
    successful sync ends any failure streak (issue #187). Callers that want the
    attempt itself recorded (e.g. for last_attempt_at) should also call
    record_sync_attempt(..., success=True).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_runs (repo, route, finished_at, docs_updated, issues_indexed, code_indexed)
            VALUES (%s, %s, now(), %s, %s, %s)
            ON CONFLICT (repo) DO UPDATE SET
                route = EXCLUDED.route,
                finished_at = EXCLUDED.finished_at,
                docs_updated = EXCLUDED.docs_updated,
                issues_indexed = EXCLUDED.issues_indexed,
                code_indexed = EXCLUDED.code_indexed
            RETURNING finished_at
            """,
            (repo, route, docs_updated, issues_indexed, code_indexed),
        )
        row = cur.fetchone()
        finished_at = row[0] if row is not None else None
    conn.commit()
    return finished_at


def record_sync_skip(
    conn: psycopg.Connection,
    repo: str,
) -> None:
    """Record that *repo* could not be locked (advisory lock held elsewhere).

    A skip is deliberately NOT an attempt: ``last_attempt_at`` /
    ``last_error`` / ``consecutive_failures`` are untouched, so the circuit
    breaker (#345) never sees a skip as a failure. Instead the row carries a
    durable, countable trace: ``last_skipped_at`` (DB ``now()``) and
    ``skip_count``, which counts *consecutive* skips and is reset to 0 by a
    successful attempt (``record_sync_attempt(success=True)``).

    Before this, a skipped repo left only a log line -- and log retention is
    ~9 hours while the daily lane's period is 24 hours, so yesterday's skip
    was unanswerable the next morning. Now ``SELECT last_skipped_at,
    skip_count FROM sync_runs WHERE repo = ...`` answers "has this repo been
    skipped every day for a week?" from the database alone.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_runs (repo, last_skipped_at, skip_count)
            VALUES (%s, now(), 1)
            ON CONFLICT (repo) DO UPDATE SET
                last_skipped_at = now(),
                skip_count = sync_runs.skip_count + 1
            """,
            (repo,),
        )
    conn.commit()


def record_sync_attempt(
    conn: psycopg.Connection,
    repo: str,
    success: bool,
    error: str | None = None,
) -> None:
    """Record that a sync was *attempted* for repo, regardless of outcome (issue #187).

    sync_runs previously only gained a row on success, via record_sync_run, so a
    repo whose auto-sync loop failed every attempt left no trace at all -- the
    post-incident investigation for issue #187 could not reconstruct the outage
    window from the DB. This records last_attempt_at unconditionally and tracks
    last_error / consecutive_failures so shiori_status can surface a dead sync
    loop instead of reporting stale "last success" data as healthy.

    On success, clears last_error and resets consecutive_failures to 0. On
    failure, increments consecutive_failures and stores the error message
    (truncated to avoid unbounded row growth from long tracebacks).

    A successful attempt also resets ``skip_count`` (issue #374): a repo that
    processes again is no longer in a skip streak, so "skipped every day for
    a week?" is answered by ``skip_count`` alone.
    """
    if success:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sync_runs (repo, last_attempt_at, last_error, consecutive_failures, skip_count)
                VALUES (%s, now(), NULL, 0, 0)
                ON CONFLICT (repo) DO UPDATE SET
                    last_attempt_at = now(),
                    last_error = NULL,
                    consecutive_failures = 0,
                    skip_count = 0
                """,
                (repo,),
            )
    else:
        truncated_error = (error or "")[:2000]
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sync_runs (repo, last_attempt_at, last_error, consecutive_failures)
                VALUES (%s, now(), %s, 1)
                ON CONFLICT (repo) DO UPDATE SET
                    last_attempt_at = now(),
                    last_error = EXCLUDED.last_error,
                    consecutive_failures = sync_runs.consecutive_failures + 1
                """,
                (repo, truncated_error),
            )
    conn.commit()


def get_sync_runs(conn: psycopg.Connection) -> dict[str, dict]:
    """Latest sync record per repo. age_seconds based on DB clock.

    last_synced_at / age_seconds reflect the last *successful* sync (finished_at,
    nullable -- issue #187). last_attempt_at / last_error / consecutive_failures
    reflect the most recent attempt regardless of outcome, so a repo that has
    never succeeded still reports its failure history.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT repo, route, finished_at,
                   EXTRACT(EPOCH FROM (now() - finished_at))::bigint,
                   docs_updated, issues_indexed, code_indexed,
                   last_attempt_at, last_error, consecutive_failures
            FROM sync_runs
            """
        )
        rows = cur.fetchall()
    return {
        r[0]: {
            "last_synced_at": r[2].isoformat() if r[2] is not None else None,
            "age_seconds": int(r[3]) if r[3] is not None else None,
            "route": r[1],
            "docs_updated": r[4],
            "issues_indexed": r[5],
            "code_added": r[6],
            "last_attempt_at": r[7].isoformat() if r[7] is not None else None,
            "last_error": r[8],
            "consecutive_failures": r[9],
        }
        for r in rows
    }


def get_sync_run(
    conn: psycopg.Connection, repo: str
) -> dict | None:
    """Latest sync record for a single repo (issue #350 review).
    Returns same shape as get_sync_runs values, or None when no row exists.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT route, finished_at,
                   EXTRACT(EPOCH FROM (now() - finished_at))::bigint,
                   docs_updated, issues_indexed, code_indexed,
                   last_attempt_at, last_error, consecutive_failures
            FROM sync_runs WHERE repo = %s
            """,
            (repo,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "last_synced_at": row[1].isoformat() if row[1] is not None else None,
        "age_seconds": int(row[2]) if row[2] is not None else None,
        "route": row[0],
        "docs_updated": row[3],
        "issues_indexed": row[4],
        "code_added": row[5],
        "last_attempt_at": row[6].isoformat() if row[6] is not None else None,
        "last_error": row[7],
        "consecutive_failures": row[8],
    }


def get_sync_attempt(
    conn: psycopg.Connection, repo: str
) -> tuple[int, datetime | None]:
    """Return (consecutive_failures, last_attempt_at) for *repo*.

    Returns (0, None) when the repo has no recorded attempts yet.
    Used by the circuit breaker to decide whether to skip a repo
    (issue #345).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT consecutive_failures, last_attempt_at FROM sync_runs WHERE repo = %s",
            (repo,),
        )
        row = cur.fetchone()
    if row is None:
        return (0, None)
    try:
        return (int(row[0] or 0), row[1])
    except (IndexError, TypeError, ValueError):
        return (0, None)


def get_chunk_counts(conn: psycopg.Connection, repo: str) -> dict[str, int]:
    """Chunk count by source_type (issue #31)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_type, count(*) FROM chunks WHERE repo = %s GROUP BY source_type",
            (repo,),
        )
        return dict(cur.fetchall())


def get_issue_item_count(conn: psycopg.Connection, repo: str) -> int:
    """Total issue_item rows (includes bots; issue #31)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM issue_items WHERE repo = %s", (repo,)
        )
        row = cur.fetchone()
        return row[0] if row else 0


def vec_literal(vec) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


def _sanitize_content(text: str) -> str:
    """Remove NUL (0x00) bytes that PostgreSQL text fields cannot contain (issue #111).

    Unlike _clean_text() in github_sync.py (which removes all control chars 0x00-0x1F),
    this only strips NUL because PostgreSQL text columns accept other control characters.
    Call sites that read raw files (sync_docs/sync_code) additionally pass through
    _clean_text() for full control-char sanitisation; the NUL-only guard here is the
    last line of defence for any code path that feeds content into the DB.

    New chunk-insertion functions must call this on content before writing to the DB.
    """
    return text.replace("\x00", "")


def delete_chunks_by_key(conn: psycopg.Connection, chunk_key: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM chunks WHERE chunk_key = %s", (chunk_key,))


def insert_chunk(
    conn: psycopg.Connection,
    *,
    chunk_key: str,
    chunk_index: int,
    source_type: str,
    repo: str,
    content: str,
    embedding,
    path: str | None = None,
    issue_no: int | None = None,
    comment_id: int | None = None,
    kind: str | None = None,
    language: str | None = None,
    heading_path: str | None = None,
    state: str | None = None,
    author: str | None = None,
    line: int | None = None,
    end_line: int | None = None,
    commit_sha: str | None = None,
    prog_lang: str | None = None,
    symbols: str | None = None,
    created_at=None,
    updated_at=None,
    url: str | None = None,
) -> None:
    content = _sanitize_content(content)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chunks (
                chunk_key, chunk_index, source_type, repo, path, issue_no,
                comment_id, kind, language, heading_path, content, embedding,
                state, author, line, end_line, commit_sha, prog_lang, symbols,
                created_at, updated_at, url
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (chunk_key, chunk_index) DO UPDATE SET
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding,
                kind = EXCLUDED.kind,
                language = EXCLUDED.language,
                heading_path = EXCLUDED.heading_path,
                state = EXCLUDED.state,
                author = EXCLUDED.author,
                line = EXCLUDED.line,
                end_line = EXCLUDED.end_line,
                commit_sha = EXCLUDED.commit_sha,
                prog_lang = EXCLUDED.prog_lang,
                symbols = EXCLUDED.symbols,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                url = EXCLUDED.url
            """,
            (
                chunk_key, chunk_index, source_type, repo, path, issue_no,
                comment_id, kind, language, heading_path, content, vec_literal(embedding),
                state, author, line, end_line, commit_sha, prog_lang, symbols,
                created_at, updated_at, url,
            ),
        )


_BULK_INSERT_SQL = """
    INSERT INTO chunks (
        chunk_key, chunk_index, source_type, repo, path, issue_no,
        comment_id, kind, language, heading_path, content, embedding,
        state, author, line, end_line, commit_sha, prog_lang, symbols,
        created_at, updated_at, url
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
    ON CONFLICT (chunk_key, chunk_index) DO UPDATE SET
        content = EXCLUDED.content,
        embedding = EXCLUDED.embedding,
        kind = EXCLUDED.kind,
        language = EXCLUDED.language,
        heading_path = EXCLUDED.heading_path,
        state = EXCLUDED.state,
        author = EXCLUDED.author,
        line = EXCLUDED.line,
        end_line = EXCLUDED.end_line,
        commit_sha = EXCLUDED.commit_sha,
        prog_lang = EXCLUDED.prog_lang,
        symbols = EXCLUDED.symbols,
        created_at = EXCLUDED.created_at,
        updated_at = EXCLUDED.updated_at,
        url = EXCLUDED.url
"""


def bulk_insert_chunks(conn: psycopg.Connection, rows: list[dict]) -> None:
    """Bulk insert chunks via executemany (issue #72)."""
    if not rows:
        return
    with conn.cursor() as cur:
        params = [
            (
                r["chunk_key"],
                r["chunk_index"],
                r["source_type"],
                r["repo"],
                r.get("path"),
                r.get("issue_no"),
                r.get("comment_id"),
                r.get("kind"),
                r.get("language"),
                r.get("heading_path"),
                _sanitize_content(r["content"]),
                vec_literal(r["embedding"]),
                r.get("state"),
                r.get("author"),
                r.get("line"),
                r.get("end_line"),
                r.get("commit_sha"),
                r.get("prog_lang"),
                r.get("symbols"),
                r.get("created_at"),
                r.get("updated_at"),
                r.get("url"),
            )
            for r in rows
        ]
        cur.executemany(_BULK_INSERT_SQL, params)


def get_issues_by_numbers(
    conn: psycopg.Connection, repo: str, issue_nos: list[int]
) -> dict[int, dict]:
    """Get title and state for a set of issue numbers (issue #97)."""
    if not issue_nos:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT issue_no, title, state, kind, url
            FROM issue_items
            WHERE repo = %s AND issue_no = ANY(%s) AND comment_id = 0
            """,
            (repo, issue_nos),
        )
        rows = cur.fetchall()
    return {
        r[0]: {
            "title": r[1],
            "state": r[2],
            "kind": r[3],
            "url": r[4],
        }
        for r in rows
    }


def find_inbound_refs(
    conn: psycopg.Connection, repo: str, issue_no: int
) -> list[dict]:
    """Find other issues/PRs that reference this issue (issue #97).

    Searches issue_items body for '#{issue_no}' pattern and returns
    the referencing items.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (issue_no) issue_no, comment_id, kind, author, body, url, created_at
            FROM issue_items
            WHERE repo = %s
              AND issue_no != %s
              AND body ~ %s
            ORDER BY issue_no, (comment_id = 0) DESC
            """,
            (repo, issue_no, f"(?<!\\w)#{issue_no}(?![\\d])"),
        )
        rows = cur.fetchall()
    return [
        {
            "issue_no": r[0],
            "author": r[3],
            "body_snippet": (r[4] or "")[:200],
            "url": r[5],
            "created_at": r[6].isoformat() if r[6] else None,
        }
        for r in rows
    ]

def get_code_chunks(
    conn: psycopg.Connection,
    repo: str,
    prog_lang: str | None = None,
    path_prefix: str | None = None,
) -> list[dict]:
    """Get code chunks for api_reference report (issue #156)."""
    query = """
        SELECT path, heading_path, line, end_line, content, prog_lang
        FROM chunks
        WHERE repo = %s AND source_type = 'code' AND content NOT LIKE '[%%] (module)%%'
    """
    params = [repo]
    if prog_lang:
        query += " AND prog_lang = %s"
        params.append(prog_lang)
    if path_prefix:
        query += " AND path LIKE %s || '%'"
        params.append(path_prefix)
    query += " ORDER BY path, line"

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    return [
        {
            "path": r[0],
            "heading_path": r[1],
            "line": r[2],
            "end_line": r[3],
            "content": r[4],
            "prog_lang": r[5],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# repo_index_state: pull-type sync tracking (#236)
# ---------------------------------------------------------------------------


def upsert_clone_head(
    conn: psycopg.Connection,
    repo: str,
    clone_head: str,
) -> None:
    """Record Phase 1 completion: store on-disk HEAD after refresh_clone."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO repo_index_state (repo, clone_head)
            VALUES (%s, %s)
            ON CONFLICT (repo) DO UPDATE SET
                clone_head = EXCLUDED.clone_head
            """,
            (repo, clone_head),
        )
    conn.commit()


def upsert_indexed_head(
    conn: psycopg.Connection,
    repo: str,
    indexed_head: str,
    last_sync_error: str | None = None,
) -> None:
    """Record Phase 2 completion: store indexed HEAD after sync succeeds."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO repo_index_state (repo, indexed_head, last_sync_at, last_sync_error)
            VALUES (%s, %s, now(), %s)
            ON CONFLICT (repo) DO UPDATE SET
                indexed_head = EXCLUDED.indexed_head,
                last_sync_at = now(),
                last_sync_error = EXCLUDED.last_sync_error
            """,
            (repo, indexed_head, last_sync_error),
        )
    conn.commit()


def record_repo_sync_error(
    conn: psycopg.Connection,
    repo: str,
    error: str,
) -> None:
    """Record a Phase 2 sync error for the repo."""
    truncated_error = (error or "")[:2000]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO repo_index_state (repo, last_sync_error)
            VALUES (%s, %s)
            ON CONFLICT (repo) DO UPDATE SET
                last_sync_error = EXCLUDED.last_sync_error
            """,
            (repo, truncated_error),
        )
    conn.commit()


def get_repo_index_state(
    conn: psycopg.Connection, repo: str
) -> dict:
    """Get repo_index_state for a single repo (issue #350).
    Returns {clone_head, indexed_head, ...} or empty dict when no row exists.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT clone_head, indexed_head, last_sync_at, last_sync_error "
            "FROM repo_index_state WHERE repo = %s",
            (repo,),
        )
        row = cur.fetchone()
    if row is None:
        return {}
    return {
        "clone_head": row[0],
        "indexed_head": row[1],
        "last_sync_at": row[2].isoformat() if row[2] is not None else None,
        "last_sync_error": row[3],
    }


def get_all_repo_index_state(
    conn: psycopg.Connection,
) -> dict[str, dict]:
    """Get repo_index_state for all repos. Returns {repo: {clone_head, indexed_head, ...}}."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT repo, clone_head, indexed_head, last_sync_at, last_sync_error "
            "FROM repo_index_state"
        )
        rows = cur.fetchall()
    return {
        r[0]: {
            "clone_head": r[1],
            "indexed_head": r[2],
            "last_sync_at": r[3].isoformat() if r[3] is not None else None,
            "last_sync_error": r[4],
        }
        for r in rows
    }
