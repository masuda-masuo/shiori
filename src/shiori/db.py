"""DB 接続とスキーマ。

設計判断（詳細設計/04）:
- docs / issue / pr_review / code は `source_type` 列で 1 テーブル (`chunks`) に統合する。
- 全文検索は pgroonga。索引作成時に TokenMecab（形態素解析）を試み、
  プラグインが無ければ TokenBigram にフォールバックする。
- read_issue 用に生のスレッドを `issue_items` に保持する（チャンクとは別）。
- 初回／rebuild のバルク経路では、重い索引（HNSW／pgroonga）をロード後に一括作成し、
  逐次構築コストを回避する（issue #72）。
"""

from __future__ import annotations

import logging

import psycopg

from .config import Settings

log = logging.getLogger(__name__)


def connect(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(settings.database_url, autocommit=False)


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgroonga;

CREATE TABLE IF NOT EXISTS sync_state (
    repo TEXT NOT NULL,
    kind TEXT NOT NULL,           -- 'docs' | 'issues' | 'issue_comments' | 'pr_review_comments'
    cursor TEXT,                  -- docs: HEAD sha / API: 最終 updated_at (ISO8601)
    PRIMARY KEY (repo, kind)
);

-- 同期実行の記録（索引の鮮度確認用。issue #22）
-- sync_state のカーソルは「最後に取り込んだアイテムの updated_at」であり実行時刻ではない。
-- こちらは変更が 0 件でも同期が成功するたびに更新されるため、鮮度の指標になる。
CREATE TABLE IF NOT EXISTS sync_runs (
    repo TEXT PRIMARY KEY,
    route TEXT,                   -- 'cli' | 'runner' | 'mcp' | 'auto'
    finished_at TIMESTAMPTZ NOT NULL,
    docs_updated INTEGER,
    issues_indexed INTEGER,
    code_indexed INTEGER
);

CREATE TABLE IF NOT EXISTS doc_files (
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    content_sha TEXT NOT NULL,
    language TEXT,
    kind TEXT NOT NULL DEFAULT 'doc',  -- 'doc' | 'code' (issue #33)
    PRIMARY KEY (repo, path)
);

-- read_issue 用の生データ（チャンクとは独立に全文を保持）
CREATE TABLE IF NOT EXISTS issue_items (
    repo TEXT NOT NULL,
    issue_no INTEGER NOT NULL,
    comment_id BIGINT NOT NULL DEFAULT 0,  -- 0 = issue/PR 本文
    kind TEXT NOT NULL,            -- 'issue' | 'pr' | 'comment' | 'pr_review_comment'
    title TEXT,
    author TEXT,
    is_bot BOOLEAN NOT NULL DEFAULT FALSE,
    state TEXT,                    -- open | closed
    path TEXT,                     -- pr_review_comment のみ
    line INTEGER,                  -- pr_review_comment のみ
    body TEXT,
    url TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    PRIMARY KEY (repo, issue_no, comment_id)
);

-- PR 変更ファイルマップ（メタデータのみ。issue #54）
-- コンテンツ（patch hunk 全文）は保持せず、GitHub MCP に委譲する。
-- head_sha で force-push を追従し、変更があれば再取得する。
-- ファイル0件の PR でも head_sha を保持するため、path='' の sentinel 行が挿入される。
CREATE TABLE IF NOT EXISTS pr_changes (
    repo TEXT NOT NULL,
    issue_no INTEGER NOT NULL,
    head_sha TEXT,
    path TEXT NOT NULL,
    status TEXT,                  -- 'added' | 'modified' | 'removed' | 'renamed'
    additions INTEGER,
    deletions INTEGER,
    changes INTEGER,
    blob_url TEXT,                -- GitHub のファイル blob URL
    PRIMARY KEY (repo, issue_no, path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    chunk_key TEXT NOT NULL,       -- 由来を表す自然キー（doc:repo:path 等）
    chunk_index INTEGER NOT NULL DEFAULT 0,
    source_type TEXT NOT NULL CHECK (source_type IN ('doc', 'issue', 'pr_review', 'code')),
    repo TEXT NOT NULL,
    path TEXT,
    issue_no INTEGER,
    comment_id BIGINT,
    language TEXT,
    heading_path TEXT,
    content TEXT NOT NULL,
    embedding vector({dim}),
    state TEXT,
    author TEXT,
    line INTEGER,
    end_line INTEGER,             -- code の行範囲終端（source_type='code' 以外は NULL）
    commit_sha TEXT,              -- コードの permalink 用 SHA
    prog_lang TEXT,               -- プログラミング言語（source_type='code' 以外は NULL）
    symbols TEXT,                 -- 識別子分割済み文字列（pgroonga 検索用）
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

# 重い索引の名前（DROP / CREATE をバルク経路で制御するため定数化。issue #72）
_HNSW_INDEX = "chunks_embedding_hnsw"
_PGROONGA_CONTENT_INDEX = "chunks_content_pgroonga"
_PGROONGA_SYMBOLS_INDEX = "chunks_symbols_pgroonga"


def _run_alter_statements(conn: psycopg.Connection) -> None:
    """既存 DB に対する冪等な ALTER。CREATE TABLE IF NOT EXISTS では新カラムが追加されないため。

    migrate_light / migrate の両方から呼ばれる。
    """
    # 1. source_type CHECK 制約に 'code' を追加（既存制約を置き換え）
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_source_type_check")
        cur.execute(
            "ALTER TABLE chunks ADD CONSTRAINT chunks_source_type_check "
            "CHECK (source_type IN ('doc', 'issue', 'pr_review', 'code'))"
        )
    conn.commit()

    # 2. 新しいカラムを追加
    with conn.cursor() as cur:
        for col, typ in [
            ("end_line", "INTEGER"),
            ("commit_sha", "TEXT"),
            ("prog_lang", "TEXT"),
            ("symbols", "TEXT"),
        ]:
            cur.execute(f"ALTER TABLE chunks ADD COLUMN IF NOT EXISTS {col} {typ}")
    conn.commit()

    # 3. doc_files.kind 追加（既存行は 'doc' のまま）
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE doc_files ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'doc'")
    conn.commit()

    # 4. sync_runs.code_indexed 追加
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS code_indexed INTEGER")
    conn.commit()


def migrate_light(conn: psycopg.Connection, settings: Settings) -> None:
    """テーブル・制約・btree 索引のみ作成。HNSW・pgroonga は作成しない（issue #72）。

    バルク経路（初回／rebuild）で使用。重い索引はデータロード後に create_heavy_indexes() で一括作成する。
    """
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL.format(dim=settings.embedding_dim))
    conn.commit()
    _run_alter_statements(conn)


def create_heavy_indexes(conn: psycopg.Connection) -> None:
    """HNSW と pgroonga 索引を作成する（issue #72）。

    バルク経路で全データロード後に呼ぶ。既に存在する場合は何もしない（IF NOT EXISTS）。
    """
    # pgvector: HNSW (cosine)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {_HNSW_INDEX} "
            "ON chunks USING hnsw (embedding vector_cosine_ops)"
        )
    conn.commit()
    log.info("HNSW index created: %s", _HNSW_INDEX)

    # pgroonga: TokenMecab があれば形態素解析、無ければ TokenBigram にフォールバック
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {_PGROONGA_CONTENT_INDEX} "
                "ON chunks USING pgroonga (content) WITH (tokenizer = 'TokenMecab')"
            )
        conn.commit()
        log.info("pgroonga index created with TokenMecab: %s", _PGROONGA_CONTENT_INDEX)
    except psycopg.Error:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {_PGROONGA_CONTENT_INDEX} "
                "ON chunks USING pgroonga (content)"
            )
        conn.commit()
        log.info("pgroonga index created with default tokenizer (TokenBigram): %s", _PGROONGA_CONTENT_INDEX)

    # symbols 用 pgroonga 索引
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {_PGROONGA_SYMBOLS_INDEX} "
                "ON chunks USING pgroonga (symbols) WITH (tokenizer = 'TokenMecab')"
            )
        conn.commit()
        log.info("pgroonga index on symbols created with TokenMecab: %s", _PGROONGA_SYMBOLS_INDEX)
    except psycopg.Error:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {_PGROONGA_SYMBOLS_INDEX} "
                "ON chunks USING pgroonga (symbols)"
            )
        conn.commit()
        log.info("pgroonga index on symbols created with default tokenizer (TokenBigram): %s", _PGROONGA_SYMBOLS_INDEX)


def drop_heavy_indexes(conn: psycopg.Connection) -> None:
    """HNSW と pgroonga 索引を削除する（issue #72）。

    バルク経路でデータロード前に呼ぶ。存在しない場合は何もしない（IF EXISTS）。
    """
    for idx in (_HNSW_INDEX, _PGROONGA_CONTENT_INDEX, _PGROONGA_SYMBOLS_INDEX):
        with conn.cursor() as cur:
            cur.execute(f"DROP INDEX IF EXISTS {idx}")
        conn.commit()
        log.info("dropped index: %s", idx)


def migrate(conn: psycopg.Connection, settings: Settings) -> None:
    """完全なスキーマ作成（テーブル＋全索引）。増分経路で使用（issue #72）。

    バルク経路では migrate_light() + ロード後 create_heavy_indexes() を使う。
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
    """同期の成功をリポジトリ単位で記録し、完了時刻（DB の now()）を返す（issue #22 / #33）。

    advisory lock で skip された実行は呼び出し側で記録しないこと（成功した同期のみ）。
    時刻は DB の now() を使う: 複数経路・複数プロセスでも単一 DB の時計で一貫する。
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


def get_sync_runs(conn: psycopg.Connection) -> dict[str, dict]:
    """リポジトリごとの最終同期記録。age_seconds は DB 時計基準の経過秒数。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT repo, route, finished_at,
                   EXTRACT(EPOCH FROM (now() - finished_at))::bigint,
                   docs_updated, issues_indexed, code_indexed
            FROM sync_runs
            """
        )
        rows = cur.fetchall()
    return {
        r[0]: {
            "last_synced_at": r[2].isoformat(),
            "age_seconds": int(r[3]),
            "route": r[1],
            "docs_updated": r[4],
            "issues_indexed": r[5],
            "code_indexed": r[6],
        }
        for r in rows
    }


def get_chunk_counts(conn: psycopg.Connection, repo: str) -> dict[str, int]:
    """source_type ごとのチャンク数（issue #31）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_type, count(*) FROM chunks WHERE repo = %s GROUP BY source_type",
            (repo,),
        )
        return dict(cur.fetchall())


def get_issue_item_count(conn: psycopg.Connection, repo: str) -> int:
    """issue_items の総行数（bot 含む全件。issue #31）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM issue_items WHERE repo = %s", (repo,)
        )
        row = cur.fetchone()
        return row[0] if row else 0


def get_pr_changes(
    conn: psycopg.Connection, repo: str, issue_no: int
) -> tuple[list[dict], str | None]:
    """PR の変更ファイルマップを取得する（issue #54）。

    Returns:
        (files, head_sha) のタプル。
        files: ファイル一覧（path / status / additions / deletions / changes / blob_url）
        head_sha: PR の head_sha（全ファイル共通）。ファイル0件でも保持される。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT path, status, additions, deletions, changes, blob_url, head_sha
            FROM pr_changes
            WHERE repo = %s AND issue_no = %s
            ORDER BY path
            """,
            (repo, issue_no),
        )
        rows = cur.fetchall()
    head_sha = rows[0][6] if rows else None
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
        if r[0]  # path が空文字の sentinel 行を除外
    ]
    return files, head_sha


def upsert_pr_changes(
    conn: psycopg.Connection,
    repo: str,
    issue_no: int,
    head_sha: str,
    files: list[dict],
) -> None:
    """PR の変更ファイルマップを upsert する（issue #54）。

    既存行を全削除してから新しいファイル一覧を挿入する（force-push 等で
    ファイル構成が変わるため、差分更新ではなく全置換）。
    ファイル0件の場合も head_sha を保持するため sentinel 行を挿入する。
    呼び出し側が commit すること（upsert_pr_changes 内では commit しない）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pr_changes WHERE repo = %s AND issue_no = %s",
            (repo, issue_no),
        )
        if files:
            for f in files:
                cur.execute(
                    """
                    INSERT INTO pr_changes (repo, issue_no, head_sha, path, status,
                                            additions, deletions, changes, blob_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        repo, issue_no, head_sha,
                        f["filename"],
                        f.get("status"),
                        f.get("additions"),
                        f.get("deletions"),
                        f.get("changes"),
                        f.get("blob_url"),
                    ),
                )
        else:
            # ファイル0件の PR でも head_sha を保持（sentinel 行。path='' で識別）
            cur.execute(
                """
                INSERT INTO pr_changes (repo, issue_no, head_sha, path, status,
                                        additions, deletions, changes, blob_url)
                VALUES (%s, %s, %s, '', NULL, NULL, NULL, NULL, NULL)
                """,
                (repo, issue_no, head_sha),
            )


def get_pr_head_sha(
    conn: psycopg.Connection, repo: str, issue_no: int
) -> str | None:
    """PR の保存済み head_sha を取得する（変更検知用。issue #54）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT head_sha FROM pr_changes WHERE repo = %s AND issue_no = %s LIMIT 1",
            (repo, issue_no),
        )
        row = cur.fetchone()
        return row[0] if row else None


def vec_literal(vec) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


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
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chunks (
                chunk_key, chunk_index, source_type, repo, path, issue_no,
                comment_id, language, heading_path, content, embedding,
                state, author, line, end_line, commit_sha, prog_lang, symbols,
                created_at, updated_at, url
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (chunk_key, chunk_index) DO UPDATE SET
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding,
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
                comment_id, language, heading_path, content, vec_literal(embedding),
                state, author, line, end_line, commit_sha, prog_lang, symbols,
                created_at, updated_at, url,
            ),
        )


_BULK_INSERT_SQL = """
    INSERT INTO chunks (
        chunk_key, chunk_index, source_type, repo, path, issue_no,
        comment_id, language, heading_path, content, embedding,
        state, author, line, end_line, commit_sha, prog_lang, symbols,
        created_at, updated_at, url
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
    ON CONFLICT (chunk_key, chunk_index) DO UPDATE SET
        content = EXCLUDED.content,
        embedding = EXCLUDED.embedding,
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
    """チャンクをバルク挿入する（executemany。issue #72）。

    呼び出し側が commit すること。各行は insert_chunk と同じキーを持つ dict。
    embedding はリスト形式で渡す（vec_literal は内部で適用）。
    """
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
                r.get("language"),
                r.get("heading_path"),
                r["content"],
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
