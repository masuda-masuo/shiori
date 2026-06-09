"""ingest ジョブ（詳細設計/01・07）。

決定: 同期はオンデマンド実行。
    docker compose run --rm app python -m shiori ingest
スケジュール実行が必要な場合はホスト側 cron 等から同コマンドを叩く。
"""

from __future__ import annotations

import logging

from . import db
from .config import Settings, load_settings
from .embedding import Embedder
from .github_sync import sync_docs, sync_issues

log = logging.getLogger(__name__)


def run_ingest(
    settings: Settings | None = None,
    repos: list[str] | None = None,
    rebuild: bool = False,
) -> None:
    settings = settings or load_settings()
    targets = repos or settings.repos
    if not targets:
        raise SystemExit("SHIORI_REPOS が未設定です（例: SHIORI_REPOS=owner/name）")

    conn = db.connect(settings)
    db.migrate(conn, settings)

    if rebuild:
        log.warning("rebuild: 既存の索引と同期カーソルを破棄します")
        with conn.cursor() as cur:
            cur.execute("TRUNCATE chunks, doc_files, issue_items, sync_state")
        conn.commit()

    embedder = Embedder(settings.embedding_model, settings.embedding_dim)

    for repo in targets:
        log.info("=== %s ===", repo)
        n_docs = sync_docs(settings, conn, embedder, repo)
        log.info("docs: %d files updated", n_docs)
        n_items = sync_issues(settings, conn, embedder, repo)
        log.info("issues/PR: %d items indexed", n_items)

    with conn.cursor() as cur:
        cur.execute("SELECT source_type, count(*) FROM chunks GROUP BY 1 ORDER BY 1")
        for st, n in cur.fetchall():
            log.info("chunks[%s] = %d", st, n)
    conn.close()
