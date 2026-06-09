"""DB 接続とスキーマ。

設計判断（詳細設計/04）:
- docs / issue / pr_review は `source_type` 列で 1 テーブル (`chunks`) に統合する。
- 全文検索は pgroonga。索引作成時に TokenMecab（形態素解析）を試み、
  プラグインが無ければ TokenBigram にフォールバックする。
- read_issue 用に生のスレッドを `issue_items` に保持する（チャンクとは別）。
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

CREATE TABLE IF NOT EXISTS doc_files (
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    content_sha TEXT NOT NULL,
    language TEXT,
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

CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    chunk_key TEXT NOT NULL,       -- 由来を表す自然キー（doc:repo:path 等）
    chunk_index INTEGER NOT NULL DEFAULT 0,
    source_type TEXT NOT NULL CHECK (source_type IN ('doc', 'issue', 'pr_review')),
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
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    url TEXT,
    UNIQUE (chunk_key, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_repo_idx ON chunks (repo);
CREATE INDEX IF NOT EXISTS chunks_source_type_idx ON chunks (source_type);
CREATE INDEX IF NOT EXISTS chunks_updated_at_idx ON chunks (updated_at);
"""


def migrate(conn: psycopg.Connection, settings: Settings) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL.format(dim=settings.embedding_dim))
    conn.commit()

    # pgvector: HNSW (cosine)
    with conn.cursor() as cur:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw "
            "ON chunks USING hnsw (embedding vector_cosine_ops)"
        )
    conn.commit()

    # pgroonga: TokenMecab があれば形態素解析、無ければ TokenBigram にフォールバック
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS chunks_content_pgroonga "
                "ON chunks USING pgroonga (content) WITH (tokenizer = 'TokenMecab')"
            )
        conn.commit()
        log.info("pgroonga index created with TokenMecab")
    except psycopg.Error:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS chunks_content_pgroonga "
                "ON chunks USING pgroonga (content)"
            )
        conn.commit()
        log.info("pgroonga index created with default tokenizer (TokenBigram)")


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
                state, author, line, created_at, updated_at, url
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector,
                %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (chunk_key, chunk_index) DO UPDATE SET
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding,
                language = EXCLUDED.language,
                heading_path = EXCLUDED.heading_path,
                state = EXCLUDED.state,
                author = EXCLUDED.author,
                line = EXCLUDED.line,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                url = EXCLUDED.url
            """,
            (
                chunk_key, chunk_index, source_type, repo, path, issue_no,
                comment_id, language, heading_path, content, vec_literal(embedding),
                state, author, line, created_at, updated_at, url,
            ),
        )
