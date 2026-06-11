"""ingest ジョブ（詳細設計/01・07）。

決定: 同期はオンデマンド実行。
    docker compose run --rm app python -m shiori ingest
スケジュール実行が必要な場合はホスト側 cron 等から同コマンドを叩く。
認証は build_token_provider で構築し、全リポジトリの同期で共有する（詳細設計/09）。

プロセス横断排他（issue #6）:
    PostgreSQL advisory lock (pg_try_advisory_lock) を使い、serve プロセスの
    自動同期や MCP ツール ingest との同時実行を防ぐ。
    SYNC_LOCK_KEY は mcp_server.py と同じ値（0x5348494F = 'SHIO'）。
"""

from __future__ import annotations

import logging

from . import db
from .config import Settings, load_settings
from .embedding import Embedder
from .github_auth import build_token_provider
from .github_sync import sync_docs, sync_issues

log = logging.getLogger(__name__)

# PostgreSQL advisory lock キー（mcp_server.py と共有。'SHIO' の ASCII）
SYNC_LOCK_KEY = 0x5348494F


def run_ingest(
    settings: Settings | None = None,
    repos: list[str] | None = None,
    rebuild: bool = False,
) -> None:
    settings = settings or load_settings()
    targets = repos or settings.repos
    if not targets:
        raise SystemExit("SHIORI_REPOS が未設定です（例: SHIORI_REPOS=owner/name）")

    provider = build_token_provider(settings)

    conn = db.connect(settings)
    db.migrate(conn, settings)

    # --- プロセス横断排他: advisory lock ---
    # serve の自動同期や MCP ツール ingest と同時に走らないよう DB レベルで排他する。
    # advisory lock はセッション（接続）に紐づくため、取得と解放は同一接続で行う。
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_KEY,))
        acquired = cur.fetchone()[0]
    if not acquired:
        log.info("skipped: 別プロセスで同期が実行中です")
        conn.close()
        return

    try:
        if rebuild:
            log.warning("rebuild: 既存の索引と同期カーソルを破棄します")
            with conn.cursor() as cur:
                cur.execute("TRUNCATE chunks, doc_files, issue_items, sync_state")
            conn.commit()

        embedder = Embedder(settings.embedding_model, settings.embedding_dim)

        for repo in targets:
            log.info("=== %s ===", repo)
            n_docs = sync_docs(settings, conn, embedder, repo, provider)
            log.info("docs: %d files updated", n_docs)
            n_items = sync_issues(settings, conn, embedder, repo, provider)
            log.info("issues/PR: %d items indexed", n_items)

        with conn.cursor() as cur:
            cur.execute("SELECT source_type, count(*) FROM chunks GROUP BY 1 ORDER BY 1")
            for st, n in cur.fetchall():
                log.info("chunks[%s] = %d", st, n)
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
        conn.close()
