"""MCP サーバー（詳細設計/06）。

ツール構成（決定: semantic / keyword は分離を維持。semantic が入口で内部ハイブリッド）:
- semantic_search(query, filters?)  : 意味検索（内部で RRF ハイブリッド）。入口。
- keyword_search(query, filters?)   : 完全一致寄りのキーワード検索（日本語対応）。
- list_tree(path?)                  : 索引済みドキュメントのツリー閲覧。
- read_file(path, start?, end?)     : ローカルクローンからファイル（の一部）を取得。
- read_issue(number)                : issue/PR スレッド全体を取得。
- ingest(rebuild?, repo?)           : docs / issue / PR の差分同期（索引更新）。

検索結果は常にポインタ＋スニペット＋ GitHub URL。全文は read 系で取得する。

索引更新は 2 経路（issue #2 の決定）:
- ingest ツール: エージェント／ユーザーによるオンデマンド更新。
- SHIORI_SYNC_INTERVAL_SECONDS: serve プロセス内のバックグラウンド自動同期。
  間隔が「索引の古さの上限」を保証する。mcp-launcher 等が GITHUB_TOKEN を
  注入・更新する構成では、serve プロセス内で同期することで短命トークンを
  そのまま使える（長期トークンを別途用意する必要がない）。
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
from .github_sync import sync_docs, sync_issues

log = logging.getLogger(__name__)

settings: Settings = load_settings()
_embedder: Embedder | None = None
_embedder_lock = threading.Lock()
_sync_lock = threading.Lock()


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
) -> dict:
    return {
        "source_type": source_type,
        "language": language,
        "state": state,
        "repo": repo,
        "path_prefix": path_prefix,
        "updated_after": updated_after,
    }


def _do_sync(repos: list[str] | None = None, rebuild: bool = False) -> dict[str, Any]:
    """差分同期の実体。ingest ツールと自動同期ループの両方から呼ばれる。
    実行中の同期があれば skip する（ロックで排他）。"""
    if not _sync_lock.acquire(blocking=False):
        return {"status": "skipped", "reason": "同期が既に実行中です"}
    try:
        targets = repos or settings.repos
        if not targets:
            return {"status": "error", "reason": "SHIORI_REPOS が未設定です"}
        embedder = _get_embedder()
        result: dict[str, Any] = {"status": "ok", "repos": {}}
        with _conn() as conn:
            db.migrate(conn, settings)
            if rebuild:
                log.warning("rebuild: 既存の索引と同期カーソルを破棄します")
                with conn.cursor() as cur:
                    cur.execute(
                        "TRUNCATE chunks, doc_files, issue_items, sync_state"
                    )
                conn.commit()
            for repo in targets:
                n_docs = sync_docs(settings, conn, embedder, repo)
                n_items = sync_issues(settings, conn, embedder, repo)
                result["repos"][repo] = {
                    "docs_updated": n_docs,
                    "issues_indexed": n_items,
                }
                log.info(
                    "synced %s: docs=%d issues=%d", repo, n_docs, n_items
                )
        return result
    finally:
        _sync_lock.release()


def _auto_sync_loop(interval: int) -> None:
    while True:
        time.sleep(interval)
        try:
            log.info("auto sync: %s", _do_sync())
        except Exception:
            log.exception("auto sync failed")


mcp = FastMCP(
    "shiori",
    host=settings.mcp_host,
    port=settings.mcp_port,
    instructions=(
        "GitHub リポジトリの知識（Markdown ドキュメントと issue/PR の議論）への"
        "ハイブリッド検索。まず semantic_search で検索し、ポインタ＋スニペットを得て、"
        "必要な箇所だけ read_file / read_issue で取得すること。固有名詞・API 名・"
        "エラーコード等の厳密一致には keyword_search を使う。"
        "索引が古い・未索引と思われる場合（直近の変更がヒットしない等）は"
        "ingest を呼んで差分同期する。"
    ),
)


@mcp.tool()
def semantic_search(
    query: str,
    source_type: str | None = None,
    language: str | None = None,
    state: str | None = None,
    repo: str | None = None,
    path_prefix: str | None = None,
    updated_after: str | None = None,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """意味ベースの検索（入口ツール）。言い換え・概念・クロスリンガル（日本語クエリで英語ドキュメント）に強い。
    内部でキーワード検索とのハイブリッド融合 (RRF) を行う。
    ポインタ（path / heading_path / issue_no）＋スニペット＋URL を返す。全文は read 系で取得すること。
    filters: source_type は doc / issue / pr_review、language は ja / en、state は open / closed、
    updated_after は ISO8601 日付。"""
    with _conn() as conn:
        return search.semantic_search(
            settings, conn, _get_embedder(), query,
            _make_filters(source_type, language, state, repo, path_prefix, updated_after),
            top_k,
        )


@mcp.tool()
def keyword_search(
    query: str,
    source_type: str | None = None,
    language: str | None = None,
    state: str | None = None,
    repo: str | None = None,
    path_prefix: str | None = None,
    updated_after: str | None = None,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """キーワード検索（日本語対応トークナイズ）。関数名・API 名・エラーコード・設定キーなど
    固有の文字列の一致に強い。semantic_search で取りこぼした厳密な語に使う。"""
    with _conn() as conn:
        return search.keyword_search(
            settings, conn, query,
            _make_filters(source_type, language, state, repo, path_prefix, updated_after),
            top_k,
        )


@mcp.tool()
def list_tree(path: str | None = None, repo: str | None = None) -> list[str]:
    """索引済みドキュメント（Markdown）のパス一覧。path を渡すとその配下に絞る。
    リポジトリの構造を把握し、当たりをつけるのに使う。"""
    target = _resolve_repo(repo)
    with _conn() as conn, conn.cursor() as cur:
        if path:
            cur.execute(
                "SELECT path FROM doc_files WHERE repo = %s AND path LIKE %s ORDER BY path",
                (target, path.rstrip("/") + "%"),
            )
        else:
            cur.execute(
                "SELECT path FROM doc_files WHERE repo = %s ORDER BY path", (target,)
            )
        return [r[0] for r in cur.fetchall()]


@mcp.tool()
def read_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """指定ファイルの全文（または start_line〜end_line の範囲）を取得する。
    検索結果のポインタから本当に必要な箇所だけ読むこと。"""
    target = _resolve_repo(repo)
    base = os.path.realpath(settings.repo_dir(target))
    full = os.path.realpath(os.path.join(base, path))
    if not full.startswith(base + os.sep):
        raise ValueError("リポジトリ外のパスは読めません")
    if not os.path.isfile(full):
        raise FileNotFoundError(f"{path} はクローンに存在しません（ingest 済みですか？）")
    with open(full, encoding="utf-8", errors="replace") as fp:
        lines = fp.read().splitlines()
    total = len(lines)
    s = max((start_line or 1) - 1, 0)
    e = min(end_line or total, total)
    body = "\n".join(lines[s:e])
    return {
        "repo": target,
        "path": path,
        "start_line": s + 1,
        "end_line": e,
        "total_lines": total,
        "content": body,
    }


@mcp.tool()
def read_issue(number: int, repo: str | None = None) -> dict[str, Any]:
    """issue / PR のスレッド全体（本文＋コメント＋レビューコメント）を時系列で取得する。
    bot コメントも含まれる（is_bot で識別可能）。"""
    target = _resolve_repo(repo)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT comment_id, kind, title, author, is_bot, state, path, line,
                   body, url, created_at
            FROM issue_items
            WHERE repo = %s AND issue_no = %s
            ORDER BY (comment_id = 0) DESC, created_at ASC NULLS LAST
            """,
            (target, number),
        )
        rows = cur.fetchall()
    if not rows:
        raise ValueError(f"#{number} は索引されていません（ingest 済みですか？）")
    head = rows[0]
    return {
        "repo": target,
        "number": number,
        "kind": head[1],
        "title": head[2],
        "state": head[5],
        "url": head[9],
        "items": [
            {
                "author": r[3],
                "is_bot": r[4],
                "kind": r[1],
                **({"path": r[6], "line": r[7]} if r[6] else {}),
                "created_at": r[10].isoformat() if r[10] else None,
                "body": r[8],
                "url": r[9],
            }
            for r in rows
        ],
    }


@mcp.tool()
def ingest(rebuild: bool = False, repo: str | None = None) -> dict[str, Any]:
    """docs と issue/PR を GitHub から同期し索引を更新する（差分同期なので通常は数秒）。
    検索結果が古い・未索引と思われる場合（直近の変更がヒットしない、ユーザーが
    最近の変更に言及している等）に呼ぶ。rebuild=True で索引を破棄して全件作り直す
    （埋め込みモデル変更時のみ）。repo 省略時は設定済みの全リポジトリを同期する。"""
    return _do_sync(repos=[repo] if repo else None, rebuild=rebuild)


def run(transport: str = "streamable-http") -> None:
    with _conn() as conn:
        db.migrate(conn, settings)
    if settings.sync_interval_seconds > 0:
        threading.Thread(
            target=_auto_sync_loop,
            args=(settings.sync_interval_seconds,),
            daemon=True,
        ).start()
        log.info("auto sync enabled: every %ds", settings.sync_interval_seconds)
    log.info("shiori MCP server starting (%s)", transport)
    mcp.run(transport=transport)
