"""ingest ジョブ（詳細設計/01・07）。

決定: 同期はオンデマンド実行。
    docker compose run --rm app python -m shiori ingest
スケジュール実行が必要な場合はホスト側 cron 等から同コマンドを叩く。
認証は build_token_provider で構築し、全リポジトリの同期で共有する（詳細設計/09）。

プロセス横断排他（issue #6）:
    PostgreSQL advisory lock (pg_try_advisory_lock) を使い、serve プロセスの
    自動同期や MCP ツール ingest との同時実行を防ぐ。
    SYNC_LOCK_KEY は mcp_server.py と同じ値（0x5348494F = 'SHIO'）。

鮮度の記録（issue #22 / #33）:
    リポジトリごとの同期完了時に sync_runs へ完了時刻と経路を記録する。
    経路は環境変数 SHIORI_INGEST_ROUTE（既定 'cli'）。reindex.yml（self-hosted
    runner）は 'runner' を設定して実行経路を識別できるようにする。

セキュリティ（issue #63）:
    指定された repo を SHIORI_REPOS（allowlist）と照合し、含まれないものは拒否する。

バルク経路最適化（issue #72）:
    初回または rebuild では、重い索引（HNSW／pgroonga）をロード後に一括作成し、
    埋め込みをファイル横断バッチ化、チャンク挿入をバルク化する。
"""

from __future__ import annotations

import logging
import os
import time

from . import db
from .config import Settings, load_settings
from .embedding import Embedder
from .github_auth import build_token_provider
from .github_sync import ChunkBuffer, sync_code, sync_docs, sync_issues

log = logging.getLogger(__name__)

# PostgreSQL advisory lock キー（mcp_server.py と共有。'SHIO' の ASCII）
SYNC_LOCK_KEY = 0x5348494F

# バルク経路の ChunkBuffer フラッシュ閾値（issue #72）
_BULK_BUFFER_SIZE = 500


def _is_bulk_path(conn, rebuild: bool) -> bool:
    """バルク経路か判定する: rebuild=True または chunks テーブルが空／未存在。

    新規 DB（chunks テーブル未作成）もバルク扱いする（issue #72）。
    """
    if rebuild:
        return True
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('chunks')")
        if cur.fetchone()[0] is None:
            return True
        cur.execute("SELECT count(*) FROM chunks")
        return cur.fetchone()[0] == 0


def run_ingest(
    settings: Settings | None = None,
    repos: list[str] | None = None,
    rebuild: bool = False,
) -> None:
    settings = settings or load_settings()

    # allowlist 検証: 明示的に指定された repo が settings.repos に含まれるか（issue #63）
    if repos is not None:
        allowed = set(settings.repos)
        invalid = sorted(set(repos) - allowed)
        if invalid:
            raise SystemExit(
                f"指定されたリポジトリは SHIORI_REPOS に含まれていません: "
                f"{', '.join(invalid)}"
            )

    targets = repos or settings.repos
    if not targets:
        raise SystemExit("SHIORI_REPOS が未設定です（例: SHIORI_REPOS=owner/name）")

    provider = build_token_provider(settings)
    route = os.environ.get("SHIORI_INGEST_ROUTE", "cli")

    conn = db.connect(settings)

    # --- バルク経路判定（ロック前に判定のみ。新規DB対応。issue #72） ---
    is_bulk = _is_bulk_path(conn, rebuild)

    # --- スキーマ準備: migrate_light は冪等なのでロック外でOK ---
    if is_bulk:
        log.info("bulk path detected (rebuild=%s), using light schema + deferred indexes", rebuild)
        db.migrate_light(conn, settings)
    else:
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

    t_total = time.monotonic()

    try:
        # --- バルク経路: 破壊的操作はロック内で（issue #72） ---
        if is_bulk:
            if rebuild:
                log.warning("rebuild: 既存の索引と同期カーソルを破棄します")
                with conn.cursor() as cur:
                    cur.execute("TRUNCATE chunks, doc_files, issue_items, sync_state")
                conn.commit()
            db.drop_heavy_indexes(conn)

        embedder = Embedder(settings.embedding_model, settings.embedding_dim)

        if is_bulk:
            buffer = ChunkBuffer(conn, embedder, batch_size=_BULK_BUFFER_SIZE)

        for repo in targets:
            log.info("=== %s ===", repo)

            # docs phase
            t0 = time.monotonic()
            n_docs = sync_docs(
                settings, conn, embedder, repo, provider,
                buffer=buffer if is_bulk else None,
            )
            if is_bulk:
                n_flushed = buffer.flush()
                conn.commit()  # メタデータ（doc_files, set_cursor）をコミット
                log.info("docs flushed: %d chunks", n_flushed)
            t_docs = time.monotonic() - t0
            log.info("docs: %d files updated (%.1fs)", n_docs, t_docs)

            # issues phase
            t0 = time.monotonic()
            n_items = sync_issues(
                settings, conn, embedder, repo, provider,
                buffer=buffer if is_bulk else None,
            )
            if is_bulk:
                n_flushed = buffer.flush()
                conn.commit()  # メタデータ（issue_items, set_cursor）をコミット
                log.info("issues flushed: %d chunks", n_flushed)
            t_issues = time.monotonic() - t0
            log.info("issues/PR: %d items indexed (%.1fs)", n_items, t_issues)

            # code phase
            t0 = time.monotonic()
            n_code = sync_code(
                settings, conn, embedder, repo, provider,
                buffer=buffer if is_bulk else None,
            )
            if is_bulk:
                n_flushed = buffer.flush()
                conn.commit()  # メタデータ（doc_files, set_cursor）をコミット
                log.info("code flushed: %d chunks", n_flushed)
            t_code = time.monotonic() - t0
            log.info("code: %d files updated (%.1fs)", n_code, t_code)

            finished_at = db.record_sync_run(
                conn, repo, route, n_docs, n_items, n_code
            )
            log.info("synced at %s (route=%s)", finished_at.isoformat(), route)

        # --- バルク経路: 重量索引を一括作成 ---
        if is_bulk:
            t0 = time.monotonic()
            db.create_heavy_indexes(conn)
            t_idx = time.monotonic() - t0
            log.info("heavy indexes created (%.1fs)", t_idx)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_type, count(*) FROM chunks GROUP BY 1 ORDER BY 1"
            )
            for st, n in cur.fetchall():
                log.info("chunks[%s] = %d", st, n)

        t_total_elapsed = time.monotonic() - t_total
        log.info("total ingest time: %.1fs", t_total_elapsed)

    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
        conn.close()
