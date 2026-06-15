"""shiori MCP サーバー（Streamable HTTP）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import config, db, github_sync, ingest, search
from .config import Settings, load_settings
from .embedding import Embedder

logger = logging.getLogger(__name__)

settings: Settings = load_settings()
mcp = FastMCP("shiori", host=settings.mcp_host, port=settings.mcp_port)

_embedder: Embedder | None = None
_sync_task: asyncio.Task | None = None
_shutdown_event: asyncio.Event | None = None


def _get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = Embedder(settings.embedding_model)
    return _embedder


def _conn():
    return db.connect(settings)


def _resolve_repo(repo: str | None) -> str:
    if repo:
        return repo
    repos = settings.repos
    if not repos:
        raise ValueError("SHIORI_REPOS が未設定です")
    return repos[0]


def _make_filters(
    source_type: str | None = None,
    language: str | None = None,
    state: str | None = None,
    repo: str | None = None,
    path_prefix: str | None = None,
    updated_after: str | None = None,
    prog_lang: str | None = None,
) -> dict:
    """None でないフィルタだけを含む辞書を返す。"""
    raw = {
        "source_type": source_type,
        "language": language,
        "state": state,
        "repo": repo,
        "path_prefix": path_prefix,
        "updated_after": updated_after,
        "prog_lang": prog_lang,
    }
    return {k: v for k, v in raw.items() if v is not None}


@mcp.tool(name="shiori_search")
def semantic_search(
    query: str,
    source_type: str | None = None,
    language: str | None = None,
    state: str | None = None,
    repo: str | None = None,
    path_prefix: str | None = None,
    updated_after: str | None = None,
    prog_lang: str | None = None,
    top_k: int | None = None,
    sort_by: str = "score",
    sort_order: str = "desc",
) -> list[dict[str, Any]]:
    """意味ベースの検索（入口ツール）。言い換え・概念・クロスリンガル（日本語クエリで英語ドキュメント）に強い。
    内部でキーワード検索とのハイブリッド融合 (RRF) を行う。
    ポインタ（path / heading_path / issue_no）＋スニペット＋URL を返す。全文は read 系で取得すること。
    filters: source_type は doc / issue / pr_review / code、language は ja / en、
    state は open / closed、prog_lang は python / go / rust 等、
    updated_after は ISO8601 日付。
    sort_by: "score"（既定）/ "updated_at" / "created_at"。
      ランキングは関連度主に固定（issue #69）。一次ソース（doc/code）は RRF スコア順、
      二次ソース（issue/pr_review）は RRF スコア＋state/updated_at tie-break。
      純粋な日付置換ソートは行わず、既定（score）のまま使うことを推奨。
    sort_order: "desc"（既定）/ "asc"。"""
    with _conn() as conn:
        return search.semantic_search(
            settings, conn, _get_embedder(), query,
            _make_filters(source_type, language, state, repo, path_prefix, updated_after, prog_lang),
            top_k,
            sort_by,
            sort_order,
        )


@mcp.tool(name="shiori_keyword_search")
def keyword_search(
    query: str,
    source_type: str | None = None,
    language: str | None = None,
    state: str | None = None,
    repo: str | None = None,
    path_prefix: str | None = None,
    updated_after: str | None = None,
    prog_lang: str | None = None,
    top_k: int | None = None,
    sort_by: str = "score",
    sort_order: str = "desc",
) -> list[dict[str, Any]]:
    """キーワード検索（日本語対応トークナイズ）。関数名・API 名・エラーコード・設定キーなど
    固有の文字列の厳密一致に強い。通常は shiori_search を使い、厳密一致が必要なときに
    このツールを使うこと。
    code チャンクは content（シグネチャ＋docstring）と symbols（識別子分割済み文字列）の
    OR 検索になるため、camelCase/snake_case の部分一致でも発見できる。
    sort_by: "score"（既定）/ "updated_at" / "created_at"。
      ランキングは関連度主に固定（issue #69）。一次ソース（doc/code）はスコア順、
      二次ソース（issue/pr_review）はスコア＋state/updated_at tie-break。
      純粋な日付置換ソートは行わず、既定（score）のまま使うことを推奨。
    sort_order: "desc"（既定）/ "asc"。"""
    with _conn() as conn:
        return search.keyword_search(
            settings, conn, query,
            _make_filters(source_type, language, state, repo, path_prefix, updated_after, prog_lang),
            top_k,
            sort_by,
            sort_order,
        )


# list_tree の source_type で有効な値
_VALID_SOURCE_TYPES = {"doc", "code"}


@mcp.tool(name="shiori_list_tree")
def list_tree(
    path: str | None = None,
    source_type: str | None = None,
    extension: str | None = None,
    repo: str | None = None,
) -> list[dict[str, str]]:
    """索引済みドキュメント＋コードファイルのパス一覧。path を渡すとその配下に絞る。
    リポジトリの構造を把握し、当たりをつけるのに使う。

    ■ 二ストア・モデルと source_type の意味:
      - source_type='doc' (索引): doc_files テーブル（Postgres に索引済みの Markdown のみ）。
        索引されていないドキュメントは返らない。
      - source_type='code' (クローン): ディスク上のクローン（main 固定）を os.walk した結果。
        索引前でも物理的に存在すれば返る。バイナリ・ロックファイル等は除外。
      - 省略時: 両方を返すが、各エントリの source フィールドで出所を区別できる。

    コードファイル（.py, .ts, .go 等）もクローンファイルシステムから返す。
    コードは索引前でも発見可能。

    戻り値は [{"path": ..., "source": "doc"|"code"}, ...] のリスト（path でソート済み）。

    source_type: 'doc'（索引済み Markdown）/'code'（クローンのコードファイル）で絞り込み。
                 省略時は両方を返す。無効な値はエラー。
    extension:   '.py' や '.md' 等の拡張子でフィルタ（先頭ドットの有無は自由）。
                 大文字小文字無視。各取得経路でフィルタするため効率的。"""
    import os as _os

    resolved_repo = _resolve_repo(repo)
    results: list[dict[str, str]] = []

    # 無効な source_type は早期エラー
    if source_type is not None and source_type not in _VALID_SOURCE_TYPES:
        raise ValueError(
            f"無効な source_type: {source_type!r}。"
            f"有効な値: {', '.join(sorted(_VALID_SOURCE_TYPES))}"
        )

    ext_filter = None
    if extension:
        ext_filter = extension.lower() if extension.startswith(".") else f".{extension.lower()}"

    # doc 側（索引済み doc_files）
    if source_type is None or source_type == "doc":
        doc_results = _list_doc_files(resolved_repo, path, ext_filter)
        results.extend(doc_results)

    # code 側（クローンファイルシステム）
    if source_type is None or source_type == "code":
        repo_dir = settings.repo_dir(resolved_repo)
        if _os.path.isdir(repo_dir):
            code_results = _list_code_files(repo_dir, path, ext_filter)
            results.extend(code_results)

    results.sort(key=lambda r: r["path"])
    return results


def _list_doc_files(
    repo: str, path: str | None, ext_filter: str | None
) -> list[dict[str, str]]:
    """索引済み doc_files テーブルからパス一覧を返す。"""
    query = """
        SELECT DISTINCT path FROM doc_files
        WHERE repo = %s
    """
    params: list[Any] = [repo]
    if path:
        query += " AND path LIKE %s"
        params.append(path.rstrip("%") + "%")
    if ext_filter:
        query += " AND LOWER(path) LIKE %s"
        params.append(f"%{ext_filter}")

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    return [{"path": r[0], "source": "doc"} for r in rows]


def _list_code_files(
    repo_dir: str, path: str | None, ext_filter: str | None
) -> list[dict[str, str]]:
    """クローンのファイルシステムからコードファイル一覧を返す。"""
    import os as _os

    results: list[dict[str, str]] = []
    base = repo_dir
    if path:
        candidate = _os.path.join(base, path)
        if _os.path.commonpath([_os.path.abspath(candidate), _os.path.abspath(base)]) != _os.path.abspath(base):
            return results
        base = candidate

    excluded_exts = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
                     ".pdf", ".zip", ".gz", ".tar", ".lock", ".sum"}
    for root, dirs, files in _os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git")]
        for f in files:
            _, fext = _os.path.splitext(f)
            if fext.lower() in excluded_exts:
                continue
            if ext_filter and fext.lower() != ext_filter:
                continue
            full = _os.path.join(root, f)
            rel = _os.path.relpath(full, repo_dir)
            results.append({"path": rel, "source": "code"})
    return results


@mcp.tool(name="shiori_read_file")
def read_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """指定ファイルの全文（または start_line〜end_line の範囲）を取得する。
    検索結果のポインタから本当に必要な範囲だけ読むこと。
    ドキュメントだけでなくコードファイルも読める。

    このツールは索引（Postgres）ではなく、ローカルクローン（main ブランチ固定）の
    実ファイルを直接読み取る。索引が存在しなくても、クローンがあれば読める。
    PR head / 別 ref / diff の取得は GitHub MCP に委譲すること。"""
    import os as _os
    resolved_repo = _resolve_repo(repo)
    repo_dir = settings.repo_dir(resolved_repo)
    file_path = _os.path.join(repo_dir, path)
    if not _os.path.isfile(file_path):
        raise FileNotFoundError(f"ファイルが見つかりません: {path} (repo={resolved_repo})")

    with open(file_path, encoding="utf-8", errors="replace") as fp:
        lines = fp.readlines()

    total = len(lines)
    start = max(1, (start_line or 1))
    end = min(total, (end_line or total))
    selected = lines[start - 1 : end] if start <= end else []
    return {
        "path": path,
        "repo": resolved_repo,
        "total_lines": total,
        "start_line": start,
        "end_line": end,
        "content": "".join(selected),
    }


@mcp.tool(name="shiori_read_issue")
def read_issue(number: int, repo: str | None = None, exclude_noise_bots: bool = False) -> dict[str, Any]:
    """issue / PR のスレッド全体（本文＋コメント＋レビューコメント）を時系列で取得する。
    bot コメントも含まれる（is_bot で識別可能）。

    number: issue または PR 番号。
    repo: リポジトリ名（"owner/name"）。省略時は SHIORI_REPOS の最初のリポジトリ。
    exclude_noise_bots: True で allowlist 外の bot（CI / dependabot 等）を除外する（既定 False）。
        allowlist（SHIORI_INDEX_BOT_LOGINS）登録済み bot の投稿は残る（issue #44）。"""
    from . import issue_reader
    resolved_repo = _resolve_repo(repo)
    with _conn() as conn:
        return issue_reader.read_issue_thread(
            settings=settings,
            conn=conn,
            repo=resolved_repo,
            number=number,
            exclude_noise_bots=exclude_noise_bots,
        )


@mcp.tool(name="shiori_pr_changes")
def pr_changes(number: int, repo: str | None = None) -> dict[str, Any]:
    """PR の変更ファイルマップ（メタデータ）を返す。ポインタ層のツール（issue #54）。

    保持するもの（メタデータ）:
      - head_sha: PR の head コミット SHA（force-push 追従用）
      - ファイル一覧: path / status / additions / deletions / changes / blob_url

    保持しないもの（コンテンツ）:
      - patch hunk 全文 → GitHub MCP の pull_request_read(method='get_diff') で取得
      - PR head のファイル全文 → GitHub MCP の get_file_contents(sha=head_sha, ...) で取得

    このツールは索引（DB）からメタデータを返す。shiori_read_file は main ブランチ固定のため、
    PR head のファイル内容が必要な場合は必ず GitHub MCP を使うこと。
    """
    from . import pr_changes as pr_module
    resolved_repo = _resolve_repo(repo)
    with _conn() as conn:
        return pr_module.get_pr_changes(conn, resolved_repo, number)


@mcp.tool(name="shiori_ingest")
def ingest(rebuild: bool = False, repo: str | None = None) -> dict[str, Any]:
    """docs / issue/PR / code を GitHub から同期し索引を更新する（差分同期なので通常は数秒）。
    検索結果が古い・未索引と思われる場合（直近の変更がヒットしない、ユーザーが
    最近の変更に言及している等）に呼ぶ。rebuild=True で索引を破棄して全件作り直す
    （埋め込みモデル変更時のみ）。repo 省略時は設定済みの全リポジトリを同期する。
    code 索引は SHIORI_INDEX_CODE=true で有効化。

    rebuild=True は MCP ツールからは既定で無効化されている。使用するには
    環境変数 SHIORI_ALLOW_REBUILD=true の設定が必要（issue #63）。
    repo は SHIORI_REPOS に含まれるもののみ有効。"""
    if rebuild and not settings.allow_rebuild:
        raise ValueError(
            "rebuild=True は MCP ツールからは許可されていません。"
            "環境変数 SHIORI_ALLOW_REBUILD=true を設定してください。"
        )
    repos = [repo] if repo else settings.repos
    try:
        ingest.run_ingest(settings, repos, rebuild=rebuild)
    except Exception:
        logger.exception("ingest 失敗")
        raise
    return {"status": "ok", "rebuild": rebuild, "repos": repos}


@mcp.tool(name="shiori_status")
def status() -> dict[str, Any]:
    """索引の鮮度と健全性を返す。リポジトリごとに最終同期の完了時刻（last_synced_at）・
    経過秒数（age_seconds）・実行経路（cli / runner / mcp / auto）・直近の更新件数・
    チャンク数内訳（doc / issue / pr_review / code）・issue_items 全件数・差分同期カーソル・
    警告（warnings）を返す。「索引は最新か?」の判断に使う:
    age_seconds が小さければ同期済み、大きい／last_synced_at が null なら
    shiori_ingest で差分同期すること。warnings があれば索引に異常がある可能性。"""
    from . import status as status_module
    with _conn() as conn:
        return status_module.get_status(conn, settings)


async def _auto_sync_loop(interval: int):
    """バックグラウンド自動同期ループ。"""
    global _shutdown_event
    _shutdown_event = asyncio.Event()
    logger.info("自動同期を開始しました（間隔 %ds）", interval)
    while not _shutdown_event.is_set():
        try:
            await asyncio.sleep(interval)
            if _shutdown_event.is_set():
                break
            logger.debug("自動同期を実行中…")
            ingest.run_ingest(settings)
            logger.debug("自動同期完了")
        except Exception:
            logger.exception("自動同期中にエラー")


def _handle_shutdown():
    """SIGTERM/SIGINT ハンドラ。"""
    logger.info("シャットダウンシグナルを受信")
    if _shutdown_event:
        _shutdown_event.set()


def main():
    """エントリポイント。"""
    global _sync_task

    interval = settings.sync_interval_seconds

    # シグナルハンドラ
    signal.signal(signal.SIGTERM, lambda *_: _handle_shutdown())
    signal.signal(signal.SIGINT, lambda *_: _handle_shutdown())

    # 起動時同期（初回）
    try:
        ingest.run_ingest(settings)
    except Exception:
        logger.exception("起動時同期に失敗（継続します）")

    # バックグラウンド定期同期
    if interval > 0:
        _sync_task = asyncio.create_task(_auto_sync_loop(interval))

    try:
        mcp.run(transport="streamable-http")
    finally:
        if _sync_task:
            _sync_task.cancel()


if __name__ == "__main__":
    main()
