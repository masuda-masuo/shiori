"""Schema management: DDL, migrations, index lifecycle, repo-scoped teardown (issue #283)."""
from __future__ import annotations

import logging

import psycopg
from psycopg import sql

from .config import Settings

log = logging.getLogger(__name__)


#: Tables keyed by ``repo``.  ``forget_repo`` (drop one repo) and rebuild
#: (TRUNCATE all repos) both iterate this list.
REPO_SCOPED_TABLES: tuple[str, ...] = (
    "chunks",
    "doc_files",
    "issue_items",
    "sync_state",
    "sync_runs",
    "repo_index_state",
)


def truncate_all_repos(conn: psycopg.Connection) -> None:
    """Discard the whole index and every sync cursor (the ``rebuild`` path)."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("TRUNCATE {}").format(
                sql.SQL(", ").join(sql.Identifier(t) for t in REPO_SCOPED_TABLES)
            )
        )


def reindex_prepare(conn: psycopg.Connection, repos: list[str] | None) -> None:
    """Clear rebuildable index state for a bulk reindex while keeping raw data
    (issue #352): rebuilds ``chunks`` (re-chunk + re-embed) without re-fetching
    from GitHub.

    Deletes only from ``chunks`` and ``doc_files`` -- ``doc_files`` is a
    path+sha cache, not the content itself (the on-disk clone is), so
    clearing it forces re-chunking with no network fetch. ``issue_items``
    rows are preserved; only ``indexed_at`` is reset to NULL so
    ``index_issues`` re-embeds them (issue #318 only re-indexes rows where
    ``indexed_at IS NULL OR updated_at > indexed_at``).

    ``sync_state`` (fetch cursors), ``sync_runs``, and ``repo_index_state``
    are left untouched -- a reindex never re-fetches and never resets
    freshness bookkeeping.

    ``repos=None`` is unscoped (every repo, via ``TRUNCATE``).
    ``repos=[...]`` scopes the delete/update to those repos only.

    Caller commits (and is expected to follow with ``drop_heavy_indexes``).
    """
    with conn.cursor() as cur:
        if repos is None:
            cur.execute("TRUNCATE chunks")
            cur.execute("UPDATE issue_items SET indexed_at = NULL")
            cur.execute("DELETE FROM doc_files")
        else:
            cur.execute("DELETE FROM chunks WHERE repo = ANY(%s)", (repos,))
            cur.execute(
                "UPDATE issue_items SET indexed_at = NULL WHERE repo = ANY(%s)",
                (repos,),
            )
            cur.execute("DELETE FROM doc_files WHERE repo = ANY(%s)", (repos,))


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
            row = cur.fetchone()
            assert row is not None  # a scalar SELECT always yields one row
            if row[0] is None:
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

CREATE TABLE IF NOT EXISTS repo_index_state (
    repo TEXT PRIMARY KEY,
    clone_head TEXT,               -- Phase 1 completed on-disk HEAD
    indexed_head TEXT,             -- Phase 2 completed indexed HEAD
    last_sync_at TIMESTAMPTZ,
    last_sync_error TEXT
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
    indexed_at TIMESTAMPTZ,
    labels TEXT[],                  -- GitHub label names (issue #165)
    PRIMARY KEY (repo, issue_no, comment_id)
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

#: DDL lock-acquisition bound for migrate (issue #362): a blocked ALTER/CREATE
#: must crash loudly within seconds rather than queue silently behind a
#: long-held lock (Postgres lock queues are FIFO, so one stuck ACCESS
#: EXCLUSIVE request blocks every later request, including plain reads).
_DDL_LOCK_TIMEOUT = "5s"


def _existing_columns(conn: psycopg.Connection, table: str) -> dict[str, bool]:
    """{column_name: attnotnull} for *table*'s live columns.

    A plain SELECT against ``pg_attribute`` -- ACCESS SHARE only, never
    blocks behind a held lock (issue #362).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT attname, attnotnull FROM pg_attribute "
            "WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped",
            (table,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def _existing_constraint_defs(
    conn: psycopg.Connection, table: str, names: tuple[str, ...]
) -> dict[str, str]:
    """{conname: whitespace-normalized def} for the constraints in *names*
    that currently exist on *table*. A plain SELECT (ACCESS SHARE only).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = %s::regclass AND conname = ANY(%s)",
            (table, list(names)),
        )
        return {row[0]: " ".join(str(row[1]).split()) for row in cur.fetchall()}


def _run_alter_statements(conn: psycopg.Connection) -> None:
    """Idempotent ALTER for existing DB. CREATE TABLE IF NOT EXISTS does not add new columns.

    Catalog-guarded (issue #362): every ALTER below -- even
    ``ADD COLUMN IF NOT EXISTS`` -- takes ACCESS EXCLUSIVE on its table
    *before* evaluating IF NOT EXISTS, so on an already-migrated DB this used
    to unconditionally queue an ACCESS EXCLUSIVE request behind any
    long-held lock (e.g. a multi-hour ``CREATE INDEX ... USING hnsw``) and,
    because Postgres lock queues are FIFO, block every later request --
    including plain reads -- for the duration. Each ALTER is now preceded by
    a plain-SELECT probe (ACCESS SHARE, never blocks) and only runs when the
    probe shows the target state isn't already there. On doubt (an
    unparseable/unexpected constraint def) the fallback is always to run the
    DROP+ADD -- never to skip.
    """
    ran_any = False

    # 1 & 4. chunks' two CHECK constraints -- fetch both defs in one query.
    # Each expected-def string below must stay in lockstep with its ADD
    # CONSTRAINT SQL: if the SQL changes, update the expected def too, or a
    # future migrate will think the (now-correct) constraint is still stale
    # and keep re-running the DROP+ADD forever (harmless, just pointless).
    source_type_check_sql = (
        "ALTER TABLE chunks ADD CONSTRAINT chunks_source_type_check "
        "CHECK (source_type IN ('doc', 'issue', 'pr_review', 'code'))"
    )
    source_type_check_expected_def = (
        "CHECK ((source_type = ANY (ARRAY['doc'::text, 'issue'::text, "
        "'pr_review'::text, 'code'::text])))"
    )
    kind_check_sql = (
        "ALTER TABLE chunks ADD CONSTRAINT chunks_kind_check "
        "CHECK (kind IN ('issue', 'pr') OR kind IS NULL)"
    )
    kind_check_expected_def = (
        "CHECK (((kind = ANY (ARRAY['issue'::text, 'pr'::text])) OR (kind IS NULL)))"
    )
    chunks_constraints = _existing_constraint_defs(
        conn, "chunks", ("chunks_source_type_check", "chunks_kind_check")
    )

    # 1. Add 'code' to source_type CHECK constraint (replace existing)
    if chunks_constraints.get("chunks_source_type_check") != source_type_check_expected_def:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_source_type_check")
            cur.execute(source_type_check_sql)
        conn.commit()
        ran_any = True
        log.info("migrate: executed chunks_source_type_check constraint (drop+add)")

    # 2. Add new columns -- one probe covers this and step 4's chunks.kind.
    chunks_columns = _existing_columns(conn, "chunks")
    missing_chunks_cols = [
        (col, typ)
        for col, typ in [
            ("end_line", "INTEGER"),
            ("commit_sha", "TEXT"),
            ("prog_lang", "TEXT"),
            ("symbols", "TEXT"),
        ]
        if col not in chunks_columns
    ]
    if missing_chunks_cols:
        with conn.cursor() as cur:
            for col, typ in missing_chunks_cols:
                cur.execute(f"ALTER TABLE chunks ADD COLUMN IF NOT EXISTS {col} {typ}")  # type: ignore[arg-type]
        conn.commit()
        ran_any = True
        log.info("migrate: executed chunks columns %s", [c for c, _ in missing_chunks_cols])

    # 3. Add doc_files.kind (existing rows stay 'doc')
    doc_files_columns = _existing_columns(conn, "doc_files")
    if "kind" not in doc_files_columns:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE doc_files ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'doc'")
        conn.commit()
        ran_any = True
        log.info("migrate: executed doc_files.kind column")

    # 4. Add chunks.kind (issue #98) + its CHECK constraint
    kind_col_missing = "kind" not in chunks_columns
    kind_check_drifted = chunks_constraints.get("chunks_kind_check") != kind_check_expected_def
    if kind_col_missing or kind_check_drifted:
        with conn.cursor() as cur:
            if kind_col_missing:
                cur.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS kind TEXT")
            cur.execute("ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_kind_check")
            cur.execute(kind_check_sql)
        conn.commit()
        ran_any = True
        log.info("migrate: executed chunks.kind column/constraint")

    # 5 & 6. sync_runs -- one probe covers code_indexed, the attempt-tracking
    # columns, and finished_at's NOT NULL flag together.
    sync_runs_columns = _existing_columns(conn, "sync_runs")

    # 5. Add sync_runs.code_indexed (returned as code_added via API)
    if "code_indexed" not in sync_runs_columns:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS code_indexed INTEGER")
        conn.commit()
        ran_any = True
        log.info("migrate: executed sync_runs.code_indexed column")

    # 6. Sync attempt tracking (issue #187): finished_at can no longer be
    # NOT NULL since a row may now exist for a repo that has only failed;
    # add last_attempt_at/last_error/consecutive_failures for status visibility.
    finished_at_not_null = sync_runs_columns.get("finished_at", False)
    missing_attempt_cols = [
        col
        for col in ("last_attempt_at", "last_error", "consecutive_failures")
        if col not in sync_runs_columns
    ]
    if finished_at_not_null or missing_attempt_cols:
        with conn.cursor() as cur:
            if finished_at_not_null:
                cur.execute("ALTER TABLE sync_runs ALTER COLUMN finished_at DROP NOT NULL")
            if "last_attempt_at" in missing_attempt_cols:
                cur.execute("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ")
            if "last_error" in missing_attempt_cols:
                cur.execute("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS last_error TEXT")
            if "consecutive_failures" in missing_attempt_cols:
                cur.execute(
                    "ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS consecutive_failures "
                    "INTEGER NOT NULL DEFAULT 0"
                )
        conn.commit()
        ran_any = True
        log.info("migrate: executed sync_runs attempt-tracking columns")

    # 7. Add repo_index_state table for pull-type sync tracking (#236) --
    # already probes to_regclass (ACCESS SHARE); left as-is.
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('repo_index_state')")
        row = cur.fetchone()
        if row is not None and row[0] is None:
            cur.execute(
                """
                CREATE TABLE repo_index_state (
                    repo TEXT PRIMARY KEY,
                    clone_head TEXT,
                    indexed_head TEXT,
                    last_sync_at TIMESTAMPTZ,
                    last_sync_error TEXT
                )
                """
            )
            ran_any = True
            log.info("migrate: executed repo_index_state table create")
    conn.commit()

    # 8. issue_items columns: indexed_at (#318), labels (#165)
    issue_items_columns = _existing_columns(conn, "issue_items")
    missing_issue_items_cols = [
        (col, typ)
        for col, typ in [("indexed_at", "TIMESTAMPTZ"), ("labels", "TEXT[]")]
        if col not in issue_items_columns
    ]
    if missing_issue_items_cols:
        with conn.cursor() as cur:
            for col, typ in missing_issue_items_cols:
                cur.execute(f"ALTER TABLE issue_items ADD COLUMN IF NOT EXISTS {col} {typ}")  # type: ignore[arg-type]
        conn.commit()
        ran_any = True
        log.info(
            "migrate: executed issue_items columns %s", [c for c, _ in missing_issue_items_cols]
        )

    if not ran_any:
        log.info("schema up to date (no DDL executed)")


def migrate_light(conn: psycopg.Connection, settings: Settings) -> None:
    """Create tables, constraints, and btree indexes only. Skip HNSW/pgroonga (issue #72).

    Bounds the DDL body (SCHEMA_SQL + catalog-guarded ALTERs) with a short
    ``lock_timeout`` (issue #362): a long-held lock elsewhere (e.g. a
    multi-hour ``CREATE INDEX ... USING hnsw``) must make this crash loudly
    within seconds instead of queuing silently behind it -- Postgres lock
    queues are FIFO, so a stuck request here blocks every later one,
    including plain reads. On timeout the psycopg error propagates
    unmodified: no retry, no swallow. The RESET below only runs on the
    success path -- resetting inside an already-failed transaction would
    itself raise and mask the real error -- and the timeout must not leak
    into create_heavy_indexes, which runs later on the same connection.
    """
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SET lock_timeout = {}").format(sql.Literal(_DDL_LOCK_TIMEOUT)))
    conn.commit()

    with conn.cursor() as cur:
        from shiori.config import EMBEDDING_DIM as _EMBEDDING_DIM
        cur.execute(SCHEMA_SQL.format(dim=_EMBEDDING_DIM))  # type: ignore[arg-type]
    conn.commit()

    _run_alter_statements(conn)

    with conn.cursor() as cur:
        cur.execute("RESET lock_timeout")
    conn.commit()


def _create_pgroonga_index(conn: psycopg.Connection, index_name: str, column: str) -> None:
    """Create pgroonga indexes. Prefers TokenMecab; falls back to TokenBigram."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} "  # type: ignore[arg-type]
                f"ON chunks USING pgroonga ({column}) WITH (tokenizer = 'TokenMecab')"
            )
        conn.commit()
        log.info("pgroonga index created with TokenMecab: %s", index_name)
    except psycopg.Error:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} "  # type: ignore[arg-type]
                f"ON chunks USING pgroonga ({column})"
            )
        conn.commit()
        log.info("pgroonga index created with default tokenizer (TokenBigram): %s", index_name)


def create_heavy_indexes(conn: psycopg.Connection, settings: Settings) -> None:
    """Create HNSW and pgroonga indexes (issue #72). Avoided during bulk load.

    Applies two build knobs (issue #352) before the ``CREATE INDEX``:

    - ``max_parallel_maintenance_workers``: always set. Default 0 (serial
      build) uses only backend-private memory and never touches
      ``/dev/shm`` -- pgvector's PARALLEL HNSW build allocates roughly
      ``maintenance_work_mem`` of DSM there, and Docker's 64MB default
      overflows it ("could not resize shared memory segment").
    - ``maintenance_work_mem``: only set when configured (default: leave the
      PostgreSQL default alone).
    """
    with conn.cursor() as cur:
        if settings.maintenance_work_mem:
            cur.execute(
                sql.SQL("SET maintenance_work_mem = {}").format(
                    sql.Literal(settings.maintenance_work_mem)
                )
            )
        cur.execute(
            sql.SQL("SET max_parallel_maintenance_workers = {}").format(
                sql.Literal(settings.max_parallel_maintenance_workers)
            )
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {_HNSW_INDEX} "
            "ON chunks USING hnsw (embedding vector_cosine_ops)"
        )  # type: ignore[arg-type]
    conn.commit()
    log.info("HNSW index created: %s", _HNSW_INDEX)

    _create_pgroonga_index(conn, _PGROONGA_CONTENT_INDEX, "content")
    _create_pgroonga_index(conn, _PGROONGA_SYMBOLS_INDEX, "symbols")


def drop_heavy_indexes(conn: psycopg.Connection) -> None:
    """Drop HNSW and pgroonga indexes (issue #72). Temporarily dropped during bulk load for performance.

    Bounded by the same ``lock_timeout`` as ``migrate_light`` (issue #362,
    extended to this call by #364): DROP INDEX takes a table-level ACCESS
    EXCLUSIVE lock, and Postgres lock queues are FIFO -- a caller that
    reaches this while another lane holds (or is about to hold) a lock on
    ``chunks`` must crash loudly within seconds instead of queuing behind
    it and blocking every later request, including plain reads. This is
    the second defence layer: the call-site gates (issue #364, only
    ``rebuild=True`` or a covering bulk run may call this) keep an
    unrelated scoped run from calling it at all, but even a legitimate
    covering run must not sit at the head of the lock queue indefinitely.
    On timeout the psycopg error propagates unmodified: no retry, no
    swallow -- this repo prefers crash-early over a defensive fallback.
    RESET only runs on the success path, mirroring ``migrate_light``.
    """
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SET lock_timeout = {}").format(sql.Literal(_DDL_LOCK_TIMEOUT)))
    conn.commit()

    for idx in (_HNSW_INDEX, _PGROONGA_CONTENT_INDEX, _PGROONGA_SYMBOLS_INDEX):
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX IF EXISTS {idx}")  # type: ignore[arg-type]
        conn.commit()
        log.info("dropped index: %s", idx)

    with conn.cursor() as cur:
        cur.execute("RESET lock_timeout")
    conn.commit()


def migrate(conn: psycopg.Connection, settings: Settings) -> None:
    """Full schema creation (tables + all indexes). Used in incremental path (issue #72).
    Bulk path uses migrate_light() + create_heavy_indexes() after loading.
    """
    migrate_light(conn, settings)
    create_heavy_indexes(conn, settings)
