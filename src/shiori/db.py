"""DB connection and schema (detailed design/04).
docs/issue/pr_review/code share a single DB.
pgvector for embedding queries.
pgroonga for JP/EN full-text search (TokenMecab/Mecab preferred).
"""

from __future__ import annotations

import logging

import psycopg
from psycopg import sql

from .config import Settings

log = logging.getLogger(__name__)


def connect(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(settings.database_url, autocommit=False)


#: Every table keyed by ``repo``.  ``forget_repo`` (drop one repo) and the
#: rebuild TRUNCATE (drop them all) both read this list, so a table added to
#: the schema cannot be remembered by one and forgotten by the other.  Before
#: this existed, rebuild truncated only 4 of the 6 and left ``pr_changes`` /
#: ``sync_runs`` behind.
REPO_SCOPED_TABLES: tuple[str, ...] = (
    "chunks",
    "doc_files",
    "issue_items",
    "pr_changes",
    "sync_state",
    "sync_runs",
)


def truncate_all_repos(conn: psycopg.Connection) -> None:
    """Discard the whole index and every sync cursor (the ``rebuild`` path)."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("TRUNCATE {}").format(
                sql.SQL(", ").join(sql.Identifier(t) for t in REPO_SCOPED_TABLES)
            )
        )


def forget_repo(conn: psycopg.Connection, repo: str) -> dict[str, int]:
    """Drop every row belonging to *repo*. Returns rows deleted per table.

    Deliberately does **not** check *repo* against the ``SHIORI_REPOS``
    allowlist: a repo worth forgetting has usually already been dropped from
    that list (a repository that was renamed, say), so requiring membership
    would refuse exactly the cases this exists for.

    A table that does not exist yet (fresh DB) counts as 0 rows rather than
    raising -- there is simply nothing indexed to forget.
    """
    deleted: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in REPO_SCOPED_TABLES:
            cur.execute("SELECT to_regclass(%s)", (table,))
            if cur.fetchone()[0] is None:
                deleted[table] = 0
                continue
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE repo = %s").format(sql.Identifier(table)),
                (repo,),
            )
            deleted[table] = cur.rowcount
    return deleted


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgroonga;

CREATE TABLE IF NOT EXISTS sync_state (
    repo TEXT NOT NULL,
    kind TEXT NOT NULL,           -- 'docs' | 'issues' | 'issue_comments' | 'pr_review_comments'
    cursor TEXT,                  -- docs: HEAD sha / API: last updated_at (ISO8601)
    PRIMARY KEY (repo, kind)
);

-- Sync execution record (for checking index freshness. Issue #22)
-- sync_state cursor is the "last ingested item updated_at", NOT execution time.
-- Updated on every successful sync (even 0 changes), so it indicates freshness.
CREATE TABLE IF NOT EXISTS sync_runs (
    repo TEXT PRIMARY KEY,
    route TEXT,                   -- 'cli' | 'runner'(deprecated) | 'mcp' | 'auto'
    -- finished_at: last *successful* sync completion. Nullable (issue #187) --
    -- a row can now exist for a repo that has only ever failed to sync.
    finished_at TIMESTAMPTZ,
    docs_updated INTEGER,
    issues_indexed INTEGER,
    code_indexed INTEGER,  -- Returned as code_added via API (key name reflects actual semantics)
    -- Attempt tracking (issue #187): recorded on every attempt, success or
    -- failure, so a silently-dead auto-sync loop is visible even though
    -- this table only keeps the latest row per repo.
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS doc_files (
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    content_sha TEXT NOT NULL,
    language TEXT,
    kind TEXT NOT NULL DEFAULT 'doc',  -- 'doc' | 'code' (issue #33)
    PRIMARY KEY (repo, path)
);

-- Raw data for read_issue (full text stored independently of chunks)
CREATE TABLE IF NOT EXISTS issue_items (
    repo TEXT NOT NULL,
    issue_no INTEGER NOT NULL,
    comment_id BIGINT NOT NULL DEFAULT 0,  -- 0 = issue/PR body
    kind TEXT NOT NULL,            -- 'issue' | 'pr' | 'comment' | 'pr_review_comment'
    title TEXT,
    author TEXT,
    is_bot BOOLEAN NOT NULL DEFAULT FALSE,
    state TEXT,                    -- open | closed
    path TEXT,                     -- pr_review_comment only
    line INTEGER,                  -- pr_review_comment only
    body TEXT,
    url TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    PRIMARY KEY (repo, issue_no, comment_id)
);

-- PR change file map (metadata only. Issue #54)
-- Does not store content (full patch hunks); delegates to GitHub MCP.
-- Tracks force-push via head_sha; re-fetches when changed.
-- Preserves head_sha for 0-file PRs via sentinel row with path=''.
CREATE TABLE IF NOT EXISTS pr_changes (
    repo TEXT NOT NULL,
    issue_no INTEGER NOT NULL,
    head_sha TEXT,
    path TEXT NOT NULL,
    status TEXT,                  -- 'added' | 'modified' | 'removed' | 'renamed'
    additions INTEGER,
    deletions INTEGER,
    changes INTEGER,
    blob_url TEXT,                -- GitHub file blob URL
    PRIMARY KEY (repo, issue_no, path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    chunk_key TEXT NOT NULL,       -- Natural key identifying origin (doc:repo:path etc.)
    chunk_index INTEGER NOT NULL DEFAULT 0,
    source_type TEXT NOT NULL CHECK (source_type IN ('doc', 'issue', 'pr_review', 'code')),
    repo TEXT NOT NULL,
    path TEXT,
    issue_no INTEGER,
    comment_id BIGINT,
    kind TEXT CHECK (kind IN ('issue', 'pr') OR kind IS NULL),  -- 'issue' | 'pr' | NULL (issue #98)
    language TEXT,
    heading_path TEXT,
    content TEXT NOT NULL,
    embedding vector({dim}),
    state TEXT,
    author TEXT,
    line INTEGER,
    end_line INTEGER,             -- Code line range end (NULL if source_type != 'code')
    commit_sha TEXT,              -- SHA for code permalink
    prog_lang TEXT,               -- Programming language (NULL if source_type != 'code')
    symbols TEXT,                 -- Identifier-split string (for pgroonga search)
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    url TEXT,
    UNIQUE (chunk_key, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_repo_idx ON chunks (repo);
CREATE INDEX IF NOT EXISTS chunks_source_type_idx ON chunks (source_type);
CREATE INDEX IF NOT EXISTS chunks_updated_at_idx ON chunks (updated_at);
CREATE INDEX IF NOT EXISTS chunks_repo_issue_no_idx ON chunks (repo, issue_no);
"""

# Heavy index names (constants for DROP/CREATE via bulk path. Issue #72)
_HNSW_INDEX = "chunks_embedding_hnsw"
_PGROONGA_CONTENT_INDEX = "chunks_content_pgroonga"
_PGROONGA_SYMBOLS_INDEX = "chunks_symbols_pgroonga"


def _run_alter_statements(conn: psycopg.Connection) -> None:
    """Idempotent ALTER for existing DB. CREATE TABLE IF NOT EXISTS does not add new columns."""
    # 1. Add 'code' to source_type CHECK constraint (replace existing)
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_source_type_check")
        cur.execute(
            "ALTER TABLE chunks ADD CONSTRAINT chunks_source_type_check "
            "CHECK (source_type IN ('doc', 'issue', 'pr_review', 'code'))"
        )
    conn.commit()

    # 2. Add new columns
    with conn.cursor() as cur:
        for col, typ in [
            ("end_line", "INTEGER"),
            ("commit_sha", "TEXT"),
            ("prog_lang", "TEXT"),
            ("symbols", "TEXT"),
        ]:
            cur.execute(f"ALTER TABLE chunks ADD COLUMN IF NOT EXISTS {col} {typ}")
    conn.commit()

    # 3. Add doc_files.kind (existing rows stay 'doc')
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE doc_files ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'doc'")
    conn.commit()

    # 4. Add chunks.kind (issue #98)
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS kind TEXT")
        cur.execute(
            "ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_kind_check"
        )
        cur.execute(
            "ALTER TABLE chunks ADD CONSTRAINT chunks_kind_check "
            "CHECK (kind IN ('issue', 'pr') OR kind IS NULL)"
        )
    conn.commit()

    # 5. Add sync_runs.code_indexed (returned as code_added via API)
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS code_indexed INTEGER")
    conn.commit()

    # 6. Add pr_changes.base_sha for PR diff support (issue #96)
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE pr_changes ADD COLUMN IF NOT EXISTS base_sha TEXT")
    conn.commit()

    # 7. Sync attempt tracking (issue #187): finished_at can no longer be
    # NOT NULL since a row may now exist for a repo that has only failed;
    # add last_attempt_at/last_error/consecutive_failures for status visibility.
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE sync_runs ALTER COLUMN finished_at DROP NOT NULL")
        cur.execute("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ")
        cur.execute("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS last_error TEXT")
        cur.execute(
            "ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS consecutive_failures "
            "INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()


def migrate_light(conn: psycopg.Connection, settings: Settings) -> None:
    """Create tables, constraints, and btree indexes only. Skip HNSW/pgroonga (issue #72)."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL.format(dim=settings.embedding_dim))
    conn.commit()
    _run_alter_statements(conn)


def _create_pgroonga_index(conn: psycopg.Connection, index_name: str, column: str) -> None:
    """Create pgroonga indexes. Prefers TokenMecab; falls back to TokenBigram."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON chunks USING pgroonga ({column}) WITH (tokenizer = 'TokenMecab')"
            )
        conn.commit()
        log.info("pgroonga index created with TokenMecab: %s", index_name)
    except psycopg.Error:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON chunks USING pgroonga ({column})"
            )
        conn.commit()
        log.info("pgroonga index created with default tokenizer (TokenBigram): %s", index_name)


def create_heavy_indexes(conn: psycopg.Connection) -> None:
    """Create HNSW and pgroonga indexes (issue #72). Avoided during bulk load."""
    # pgvector: HNSW (cosine)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {_HNSW_INDEX} "
            "ON chunks USING hnsw (embedding vector_cosine_ops)"
        )
    conn.commit()
    log.info("HNSW index created: %s", _HNSW_INDEX)

    _create_pgroonga_index(conn, _PGROONGA_CONTENT_INDEX, "content")
    _create_pgroonga_index(conn, _PGROONGA_SYMBOLS_INDEX, "symbols")


def drop_heavy_indexes(conn: psycopg.Connection) -> None:
    """Drop HNSW and pgroonga indexes (issue #72). Temporarily dropped during bulk load for performance."""
    for idx in (_HNSW_INDEX, _PGROONGA_CONTENT_INDEX, _PGROONGA_SYMBOLS_INDEX):
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX IF EXISTS {idx}")
        conn.commit()
        log.info("dropped index: %s", idx)


def migrate(conn: psycopg.Connection, settings: Settings) -> None:
    """Full schema creation (tables + all indexes). Used in incremental path (issue #72).
    Bulk path uses migrate_light() + create_heavy_indexes() after loading.
"""
    migrate_light(conn, settings)
    create_heavy_indexes(conn)


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
        finished_at = cur.fetchone()[0]
    conn.commit()
    return finished_at


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
    """
    if success:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sync_runs (repo, last_attempt_at, last_error, consecutive_failures)
                VALUES (%s, now(), NULL, 0)
                ON CONFLICT (repo) DO UPDATE SET
                    last_attempt_at = now(),
                    last_error = NULL,
                    consecutive_failures = 0
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


def get_pr_changes(
    conn: psycopg.Connection, repo: str, issue_no: int
) -> tuple[list[dict], str | None, str | None]:
    """Fetch PR change file map (issue #54).
    
    Returns (files, head_sha, base_sha). base_sha may be None for legacy rows.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT path, status, additions, deletions, changes, blob_url, head_sha, base_sha
            FROM pr_changes
            WHERE repo = %s AND issue_no = %s
            ORDER BY path
            """,
            (repo, issue_no),
        )
        rows = cur.fetchall()
    head_sha = rows[0][6] if rows else None
    base_sha = rows[0][7] if rows else None
    files = [
        {
            "path": r[0],
            "status": r[1],
            "additions": r[2],
            "deletions": r[3],
            "changes": r[4],
            "blob_url": r[5],
        }
        for r in rows
        if r[0]  # Exclude sentinel rows with empty path
    ]
    return files, head_sha, base_sha


def upsert_pr_changes(
    conn: psycopg.Connection,
    repo: str,
    issue_no: int,
    head_sha: str,
    base_sha: str | None = None,
    files: list[dict] | None = None,
) -> None:
    """Upsert PR change file map (issue #54). Deletes existing entries for the same PR before insert.
    
    base_sha is optional for backward compatibility with callers that don't provide it.
    """
    if files is None:
        files = []
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pr_changes WHERE repo = %s AND issue_no = %s",
            (repo, issue_no),
        )
        if files:
            for f in files:
                cur.execute(
                    """
                    INSERT INTO pr_changes (repo, issue_no, head_sha, base_sha, path, status,
                                            additions, deletions, changes, blob_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        repo, issue_no, head_sha, base_sha,
                        f["filename"],
                        f.get("status"),
                        f.get("additions"),
                        f.get("deletions"),
                        f.get("changes"),
                        f.get("blob_url"),
                    ),
                )
        else:
            # Preserve head_sha even for PR with 0 files (sentinel row, path='')
            cur.execute(
                """
                INSERT INTO pr_changes (repo, issue_no, head_sha, base_sha, path, status,
                                        additions, deletions, changes, blob_url)
                VALUES (%s, %s, %s, %s, '', NULL, NULL, NULL, NULL, NULL)
                """,
                (repo, issue_no, head_sha, base_sha),
            )


def get_pr_head_sha(
    conn: psycopg.Connection, repo: str, issue_no: int
) -> str | None:
    """Get stored PR head_sha for change detection (issue #54)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT head_sha FROM pr_changes WHERE repo = %s AND issue_no = %s LIMIT 1",
            (repo, issue_no),
        )
        row = cur.fetchone()
        return row[0] if row else None


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


def get_pr_review_comments(
    conn: psycopg.Connection, repo: str, issue_no: int
) -> list[dict]:
    """Fetch review comments for a PR (issue #96).
    
    Returns list of review comments with path, line, body, author, created_at.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT comment_id, author, is_bot, path, line, body, url, created_at
            FROM issue_items
            WHERE repo = %s AND issue_no = %s AND kind = 'pr_review_comment'
            ORDER BY created_at ASC
            """,
            (repo, issue_no),
        )
        rows = cur.fetchall()
    return [
        {
            "comment_id": r[0],
            "author": r[1],
            "is_bot": r[2],
            "path": r[3],
            "line": r[4],
            "body": r[5],
            "url": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
        }
        for r in rows
    ]


def get_issue_bodies(
    conn: psycopg.Connection, repo: str, issue_no: int
) -> list[dict]:
    """Get all body texts for an issue/PR for link extraction (issue #97)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT comment_id, kind, author, body, url, created_at
            FROM issue_items
            WHERE repo = %s AND issue_no = %s
            ORDER BY (comment_id = 0) DESC, created_at ASC
            """,
            (repo, issue_no),
        )
        rows = cur.fetchall()
    return [
        {
            "comment_id": r[0],
            "kind": r[1],
            "author": r[2],
            "body": r[3],
            "url": r[4],
            "created_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]


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
        WHERE repo = %s AND source_type = 'code' AND content NOT LIKE '[%] (module)%'
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
