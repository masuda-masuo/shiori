"""MCP サーバー（詳細設計/06）。

ツール構成（決定: shiori_search が統合入口。keyword_search は厳密一致専用で分離維持）:
- search(query, filters?)             : 意味＋キーワードのハイブリッド検索（入口ツール）。まずこれを使う。
- keyword_search(query, filters?)     : 完全一致寄りのキーワード検索（日本語対応）。厳密一致が欲しいときに使う。
- list_tree(path?, source_type?, extension?): 索引済みドキュメント＋コードファイルのツリー閲覧。
- read_file(path, start?, end?)       : ローカルクローンからファイル（の一部）を取得。
- read_issue(number, repo?, exclude_noise_bots?) : issue/PR スレッド全体を取得。
- pr_changes(number, repo?)           : PR の変更ファイルマップ（ポインタ）を返す（issue #54）。
- ingest(rebuild?, repo?)             : docs / issue / PR / code の差分同期（索引更新）。
- status()                            : 索引の鮮度と健全性（最終同期時刻・件数内訳・警告）。

実際に MCP クライアントに公開されるツール名は shiori_ 接頭辞付き（issue #8）。
filesystem 等の他 MCP サーバーとの名前衝突を避けるため。関数名は据え置き。

検索結果は常にポインタ＋スニペット＋ GitHub URL。全文は read 系で取得する。

索引更新は 3 経路（issue #2, #6 の決定）:
- shiori_ingest ツール: エージェント／ユーザーによるオンデマンド更新。
- SHIORI_SYNC_INTERVAL_SECONDS: serve プロセス内のバックグラウンド自動同期（保険）。
  イベント駆動が主になったため推奨値は 3600 秒。
- self-hosted runner: push / issue / PR イベントで即時差分同期（issue #6）。
  同期の実体は _do_sync()。PostgreSQL advisory lock でプロセス横断の排他を保証。

同期が成功するたびに sync_runs へ完了時刻と経路（mcp / auto。runner / cli は
ingest.py 側で記録）を残し、shiori_status で照会できる（issue #22）。

code 索引（issue #33）は SHIORI_INDEX_CODE=true で有効化。sync_docs と同一クローンを
共有し、sha デルタで変化ファイルのみ再索引する。

セキュリティ（issue #63）:
- _do_sync の repo 引数は settings.repos と照合し、allowlist 外は拒否。
- shiori_ingest の rebuild=True は SHIORI_ALLOW_REBUILD=true 時のみ許可。

バルク経路最適化（issue #72）:
  初回または rebuild では、重い索引（HNSW／pgroonga）をロード後に一括作成し、
  埋め込みをファイル横断バッチ化、チャンク挿入をバルク化する。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import db, search
from .config import Settings, load_settings
from .embedding import Embedder
from .github_auth import build_token_provider
from .github_sync import ChunkBuffer, sync_code, sync_docs, sync_issues

log = logging.getLogger(__name__)

settings: Settings = load_settings()
_embedder: Embedder | None = None
_embedder_lock = threading.Lock()
_sync_lock = threading.Lock()

# PostgreSQL advisory lock キー（プロセス横断排他。ingest.py と共有）
# 'SHIO' の ASCII コード列を 32bit に詰めた値
SYNC_LOCK_KEY = 0x5348494F

# バルク経路の ChunkBuffer フラッシュ閾値（issue #72）
_BULK_BUFFER_SIZE = 500


def _get_embedder() -> Embedder:
    global _embedder
    with _embedder_lock:
        if _embedder is None:
            _embedder = Embedder(settings.embedding_model, settings.embedding_dim)
    return _embedder


def _conn():
    return db.connect(settings)


def _resolve_repo(repo: str | None) -> str:
    if repo:
        return repo
    if len(settings.repos) == 1:
        return settings.repos[0]
    raise ValueError(
        f"repo を指定してください。設定済み: {', '.join(settings.repos) or '(なし)'}"
    )


def _make_filters(
    source_type: str | None,
    language: str | None,
    state: str | None,
    repo: str | None,
    path_prefix: str | None,
    updated_after: str | None,
    prog_lang: str | None = None,
) -> dict:
    return {
        "source_type": source_type,
        "language": language,
        "state": state,
        "repo": repo,
        "path_prefix": path_prefix,
        "updated_after": updated_after,
        "prog_lang": prog_lang,
    }


def _is_bulk_path(conn, rebuild: bool) -> bool:
    """バルク経路か判定する: rebuild=True または chunks が空（issue #72）。"""
    if rebuild:
        return True
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks")
        return cur.fetchone()[0] == 0


def _do_sync(
    repos: list[str] | None = None,
    rebuild: bool = False,
    route: str = "mcp",
) -> dict[str, Any]:
    """差分同期の実体。ingest ツールと自動同期ループの両方から呼ばれる。

    プロセス内排他: _sync_lock（threading.Lock）で早期 skip。
    プロセス横断排他: PostgreSQL advisory lock (pg_try_advisory_lock) で、
    runner ジョブ（別プロセス）との同時実行を防ぐ。
    どちらも非ブロッキング（取得失敗 = skip）。
    リポジトリごとの完了時に sync_runs へ完了時刻と経路を記録する（issue #22 / #33）。

    初回／rebuild はバルク経路: 重量索引をロード後に一括作成し、埋め込みをバッチ化する（issue #72）。
    """
    # allowlist 検証: 明示的に指定された repo が settings.repos に含まれるか（issue #63）
    if repos is not None:
        allowed = set(settings.repos)
        invalid = sorted(set(repos) - allowed)
        if invalid:
            raise ValueError(
                f"指定されたリポジトリは SHIORI_REPOS に含まれていません: "
                f"{', '.join(invalid)}"
            )

    if not _sync_lock.acquire(blocking=False):
        return {"status": "skipped", "reason": "同期が既に実行中です"}
    try:
        targets = repos or settings.repos
        if not targets:
            return {"status": "error", "reason": "SHIORI_REPOS が未設定です"}
        provider = build_token_provider(settings)
        embedder = _get_embedder()
        result: dict[str, Any] = {"status": "ok", "repos": {}}
        with _conn() as conn:
            # --- バルク経路判定とスキーマ準備（issue #72） ---
            is_bulk = _is_bulk_path(conn, rebuild)

            if is_bulk:
                log.info("bulk path detected (rebuild=%s), using light schema + deferred indexes", rebuild)
                db.migrate_light(conn, settings)
                if rebuild:
                    log.warning("rebuild: 既存の索引と同期カーソルを破棄します")
                    with conn.cursor() as cur:
                        cur.execute(
                            "TRUNCATE chunks, doc_files, issue_items, sync_state"
                        )
                    conn.commit()
                db.drop_heavy_indexes(conn)
            else:
                db.migrate(conn, settings)

            # --- プロセス横断排他: advisory lock ---
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_KEY,))
                acquired = cur.fetchone()[0]
            if not acquired:
                return {"status": "skipped", "reason": "別プロセスで同期が実行中です"}
            try:
                if is_bulk:
                    buffer = ChunkBuffer(conn, embedder, batch_size=_BULK_BUFFER_SIZE)

                for repo in targets:
                    n_docs = sync_docs(
                        settings, conn, embedder, repo, provider,
                        buffer=buffer if is_bulk else None,
                    )
                    if is_bulk:
                        buffer.flush()
                    n_items = sync_issues(
                        settings, conn, embedder, repo, provider,
                        buffer=buffer if is_bulk else None,
                    )
                    if is_bulk:
                        buffer.flush()
                    n_code = sync_code(
                        settings, conn, embedder, repo, provider,
                        buffer=buffer if is_bulk else None,
                    )
                    if is_bulk:
                        buffer.flush()
                    finished_at = db.record_sync_run(
                        conn, repo, route, n_docs, n_items, n_code
                    )
                    result["repos"][repo] = {
                        "docs_updated": n_docs,
                        "issues_indexed": n_items,
                        "code_indexed": n_code,
                        "synced_at": finished_at.isoformat(),
                    }
                    log.info(
                        "synced %s: docs=%d issues=%d code=%d (route=%s)",
                        repo, n_docs, n_items, n_code, route,
                    )

                # --- バルク経路: 重量索引を一括作成（issue #72） ---
                if is_bulk:
                    db.create_heavy_indexes(conn)

            finally:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
        return result
    finally:
        _sync_lock.release()
