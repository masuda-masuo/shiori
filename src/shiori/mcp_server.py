"""MCP サーバー（詳細設計/06）。

ツール構成（決定: semantic / keyword は分離を維持。semantic が入口で内部ハイブリッド）:
- semantic_search(query, filters?)  : 意味検索（内部で RRF ハイブリッド）。入口。
- keyword_search(query, filters?)   : 完全一致寄りのキーワード検索（日本語対応）。
- list_tree(path?, source_type?, extension?): 索引済みドキュメント＋コードファイルのツリー閲覧。
- read_file(path, start?, end?)     : ローカルクローンからファイル（の一部）を取得。
- read_issue(number)                : issue/PR スレッド全体を取得。
- pr_changes(number, repo?)         : PR の変更ファイルマップ（ポインタ）を返す（issue #54）。
- ingest(rebuild?, repo?)           : docs / issue / PR / code の差分同期（索引更新）。
- status()                          : 索引の鮮度と健全性（最終同期時刻・件数内訳・警告）。

実際に MCP クライアントに公開されるツール名は shiori_ 接頭辞付き（issue #8）。
filesystem 等の他 MCP サーバーとの名前衰突を避けるため。関数名は据え置き。

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
from .github_sync import sync_code, sync_docs, sync_issues

log = logging.getLogger(__name__)

settings: Settings = load_settings()
_embedder: Embedder | None = None
_embedder_lock = threading.Lock()
_sync_lock = threading.Lock()

# PostgreSQL advisory lock キー（プロセス横断排他。ingest.py と共有）
# 'SHIO' の ASCII コード列を 32bit に詰めた値
SYNC_LOCK_KEY = 0x5348494F


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
    """
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
            db.migrate(conn, settings)
            # --- プロセス横断排他: advisory lock ---
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_KEY,))
                acquired = cur.fetchone()[0]
            if not acquired:
                return {"status": "skipped", "reason": "別プロセスで同期が実行中です"}
            try:
                if rebuild:
                    log.warning("rebuild: 既存の索引と同期カーソルを破棄します")
                    with conn.cursor() as cur:
                        cur.execute(
                            "TRUNCATE chunks, doc_files, issue_items, sync_state"
                        )
                    conn.commit()
                for repo in targets:
                    n_docs = sync_docs(settings, conn, embedder, repo, provider)
                    n_items = sync_issues(settings, conn, embedder, repo, provider)
                    n_code = sync_code(settings, conn, embedder, repo, provider)
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
            finally:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
        return result
    finally:
        _sync_lock.release()


def _auto_sync_loop(interval: int) -> None:
    while True:
        time.sleep(interval)
        try:
            log.info("auto sync: %s", _do_sync(route="auto"))
        except Exception:
            log.exception("auto sync failed")


# ── _walk_code_files: コードファイル収集 ──

# ドキュメント拡張子（大文字小文字無視）。doc_files テーブルが担当するため walk から除外
_DOC_EXTENSIONS = {".md", ".mdx", ".markdown"}

# os.walk でスキップするディレクトリ名（設計 10 決定 7: 量と質の両面でノイズ除去）
_EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    ".venv", "venv",
    "dist", "build",
    "__pycache__",
    ".tox", ".eggs",
    ".next",  # Next.js
    "target",  # Rust
}

# コードリストに含めないファイル拡張子（大文字小文字無視）
# バイナリ・アセット・ロックファイル等、LLM が読んでも有益でないもの
_EXCLUDE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pyc", ".pyo",
    ".so", ".dylib", ".dll", ".wasm",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".pdf",
    ".lock",  # package-lock.json, yarn.lock, Gemfile.lock 等
    ".min.js", ".min.css",  # minified
}


def _is_doc_file(filename: str) -> bool:
    """ファイル名がドキュメント拡張子か（大文字小文字無視）。"""
    return any(filename.lower().endswith(ext) for ext in _DOC_EXTENSIONS)


def _is_excluded_file(filename: str) -> bool:
    """ファイル名が除外拡張子か（大文字小文字無視）。"""
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in _EXCLUDE_EXTENSIONS)


def _match_extension(path: str, extension: str) -> bool:
    """拡張子が指定値にマッチするか（大文字小文字無視、'.' 有無両対応）。"""
    ext = extension if extension.startswith(".") else "." + extension
    return path.lower().endswith(ext.lower())


def _walk_code_files(base: str, prefix: str, extension: str | None = None) -> set[str]:
    """クローンを walk し、コードファイルの相対パス集合を返す。

    - .git / node_modules / .venv / __pycache__ 等のノイズディレクトリをスキップ
    - .md / .mdx / .markdown は doc_files テーブルが担当するため除外
    - バイナリ・アセット・ロックファイル等の非テキスト拡張子も除外
    - prefix が指定された場合はそのパス自身またはその配下のファイルのみを返す
      （例: prefix="src" は "src/main.py" にマッチ、"src2/main.py" にはマッチしない）
    - extension が指定された場合はウォーク中に拡張子フィルタを適用（大文字小文字無視）
    """
    paths: set[str] = set()
    if not os.path.isdir(base):
        return paths
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        rel_dir = os.path.relpath(dirpath, base)
        if rel_dir == ".":
            rel_dir = ""
        for fn in filenames:
            if _is_doc_file(fn) or _is_excluded_file(fn):
                continue
            rel_path = os.path.join(rel_dir, fn) if rel_dir else fn
            if prefix and not (
                rel_path == prefix or rel_path.startswith(prefix + "/")
            ):
                continue
            if extension and not _match_extension(rel_path, extension):
                continue
            paths.add(rel_path)
    return paths


mcp = FastMCP(
    "shiori",
    host=settings.mcp_host,
    port=settings.mcp_port,
    instructions=(
        "GitHub リポジトリの知識（Markdown ドキュメントと issue/PR の議論、"
        "およびソースコード）へのハイブリッド検索。"
        "まず shiori_semantic_search で検索し、ポインタ＋スニペットを得て、"
        "必要な範囲だけ shiori_read_file / shiori_read_issue で取得すること。固有名詞・API 名・"
        "エラーコード・関数名等の厳密一致には shiori_keyword_search を使う。"
        "索引が古い・未索引と思われる場合（直近の変更がヒットしない等）は"
        "shiori_ingest を呼んで差分同期する。索引が最新かどうかの確認は shiori_status。"
        "コードファイルは shiori_list_tree で発見し shiori_read_file で読める"
        "（path, start_line, end_line 指定可）。"
        "shiori_list_tree は source_type='doc'/'code' や extension='.py' で絞り込み可能。"
        "コードの検索は shiori_semantic_search / shiori_keyword_search で可能"
        "（source_type='code' で絞り込み可、prog_lang フィルタで言語指定も可）。"
        "PR の変更ファイルマップは shiori_pr_changes で取得できる。"
        "\n"
        "■ 二ストア・モデル（情報の出所）\n"
        "shiori は 2 つの独立したデータソースを持つ:\n"
        "1. 索引（Postgres/pgvector/pgroonga）\n"
        "   - shiori_semantic_search / shiori_keyword_search: 埋め込み＋全文検索\n"
        "   - shiori_read_issue: issue/PR スレッド（索引済みのもののみ）\n"
        "   - shiori_list_tree (source_type='doc'): 索引済み doc_files テーブル\n"
        "   - shiori_pr_changes: PR 変更ファイルマップ（索引済みメタデータ）\n"
        "   - 鮮度は shiori_ingest / 自動同期に依存\n"
        "2. クローン（ディスク、main ブランチ固定）\n"
        "   - shiori_read_file: 実ファイルを直接読み取り（索引不要、クローンがあれば読める）\n"
        "   - shiori_list_tree (source_type='code'): os.walk で物理的に存在するコードファイル\n"
        "   - クローンは sync_docs が clone --depth=1 + reset --hard origin/HEAD で維持\n"
        "   - 常に main ブランチ時点。PR head / 別 ref / diff は GitHub MCP に委譲"
    ),
)


@mcp.tool(name="shiori_semantic_search")
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
) -> list[dict[str, Any]]:
    """意味ベースの検索（入口ツール）。言い換え・概念・クロスリンガル（日本語クエリで英語ドキュメント）に強い。
    内部でキーワード検索とのハイブリッド融合 (RRF) を行う。
    ポインタ（path / heading_path / issue_no）＋スニペット＋URL を返す。全文は read 系で取得すること。
    filters: source_type は doc / issue / pr_review / code、language は ja / en、
    state は open / closed、prog_lang は python / go / rust 等、
    updated_after は ISO8601 日付。"""
    with _conn() as conn:
        return search.semantic_search(
            settings, conn, _get_embedder(), query,
            _make_filters(source_type, language, state, repo, path_prefix, updated_after, prog_lang),
            top_k,
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
) -> list[dict[str, Any]]:
    """キーワード検索（日本語対応トークナイズ）。関数名・API 名・エラーコード・設定キーなど
    固有の文字列の一致に強い。semantic_search で取りこぼした厳密な語に使う。
    code チャンクは content（シグネチャ＋docstring）と symbols（識別子分割済み文字列）の
    OR 検索になるため、camelCase/snake_case の部分一致でも発見できる。"""
    with _conn() as conn:
        return search.keyword_search(
            settings, conn, query,
            _make_filters(source_type, language, state, repo, path_prefix, updated_after, prog_lang),
            top_k,
        )


# list_tree の source_type で有効な値
_VALID_SOURCE_TYPES = {"doc", "code"}


@mcp.tool(name="shiori_list_tree")
def list_tree(
    path: str | None = None,
    source_type: str | None = None,
    extension: str | None = None,
    repo: str | None = None,
) -> list[dict[str, Any]]:
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
    # バリデーション
    if source_type is not None and source_type not in _VALID_SOURCE_TYPES:
        raise ValueError(
            f"無効な source_type '{source_type}' です。"
            f"有効な値: {', '.join(sorted(_VALID_SOURCE_TYPES))}"
        )

    target = _resolve_repo(repo)
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. 索引済みドキュメント（doc_files テーブル）
    if source_type is None or source_type == "doc":
        with _conn() as conn, conn.cursor() as cur:
            if path:
                prefix = path.rstrip("/")
                cur.execute(
                    "SELECT path FROM doc_files"
                    " WHERE repo = %s AND (path = %s OR path LIKE %s)"
                    " ORDER BY path",
                    (target, prefix, prefix + "/%"),
                )
            else:
                cur.execute(
                    "SELECT path FROM doc_files WHERE repo = %s ORDER BY path",
                    (target,),
                )
            for r in cur.fetchall():
                p = r[0]
                if extension and not _match_extension(p, extension):
                    continue
                if p not in seen:
                    seen.add(p)
                    entries.append({"path": p, "source": "doc"})

    # 2. コードファイル（クローンファイルシステム）
    if source_type is None or source_type == "code":
        base = os.path.realpath(settings.repo_dir(target))
        prefix = path.rstrip("/") if path else ""
        code_paths = _walk_code_files(base, prefix, extension=extension)
        # 最終ソートがあるためコード側だけの事前ソートは不要
        for p in code_paths:
            if p not in seen:
                seen.add(p)
                entries.append({"path": p, "source": "code"})

    # path でソート（doc と code が混在する場合）
    entries.sort(key=lambda e: e["path"])
    return entries


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
    target = _resolve_repo(repo)
    base = os.path.realpath(settings.repo_dir(target))
    full = os.path.realpath(os.path.join(base, path))
    if not full.startswith(base + os.sep):
        raise ValueError("リポジトリ外のパスは読めません")
    if not os.path.isfile(full):
        raise FileNotFoundError(f"{path} はクローンに存在しません（同期が必要かもしれません）")
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


@mcp.tool(name="shiori_read_issue")
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
                **( {"path": r[6], "line": r[7]} if r[6] else {}),
                "created_at": r[10].isoformat() if r[10] else None,
                "body": r[8],
                "url": r[9],
            }
            for r in rows
        ],
    }


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
    target = _resolve_repo(repo)
    with _conn() as conn:
        files, head_sha = db.get_pr_changes(conn, target, number)
    if not files:
        raise ValueError(
            f"PR #{number} の変更ファイルマップが見つかりません。"
            "shiori_ingest で同期してください。"
        )
    return {
        "repo": target,
        "number": number,
        "head_sha": head_sha,
        "files": files,
    }


@mcp.tool(name="shiori_ingest")
def ingest(rebuild: bool = False, repo: str | None = None) -> dict[str, Any]:
    """docs / issue/PR / code を GitHub から同期し索引を更新する（差分同期なので通常は数秒）。
    検索結果が古い・未索引と思われる場合（直近の変更がヒットしない、ユーザーが
    最近の変更に言及している等）に呼ぶ。rebuild=True で索引を破棄して全件作り直す
    （埋め込みモデル変更時のみ）。repo 省略時は設定済みの全リポジトリを同期する。
    code 索引は SHIORI_INDEX_CODE=true で有効化。"""
    return _do_sync(repos=[repo] if repo else None, rebuild=rebuild, route="mcp")


_STALE_SECONDS = 86400  # 24 時間


def _build_warnings(
    info: dict,
    chunk_counts: dict[str, int],
    items_in_db: int,
    cursors: dict[str, str | None],
) -> list[str]:
    """索引の異常を検出して警告リストを返す（issue #31）。"""
    warnings: list[str] = []

    # 鮮度: 最終同期から長時間経過
    age = info.get("age_seconds")
    if age is not None and age > _STALE_SECONDS:
        hours = age // 3600
        warnings.append(
            f"最終同期から {hours} 時間経過。索引が古い可能性があります"
        )

    # 構造的欠落: issue_items はあるが chunks が極端に少ない
    # pr_review を含めて比較する（review comment 比率の高いリポジトリでの過検知防止。issue #35）
    total_issue_chunks = chunk_counts.get("issue", 0) + chunk_counts.get("pr_review", 0)
    if items_in_db > 0 and total_issue_chunks < items_in_db // 2:
        warnings.append(
            f"issue_items は {items_in_db} 件あるが chunks[issue]+chunks[pr_review] は {total_issue_chunks} 件。"
            "bot 除外（SHIORI_INDEX_BOT_LOGINS）または索引欠落の可能性があります"
        )

    # 未同期カテゴリ: sync_state にカーソルがない種類
    all_kinds = {"docs", "issues", "issue_comments", "pr_review_comments"}
    missing = [k for k in all_kinds if k not in cursors]
    if missing:
        warnings.append(
            f"未同期の種類があります: {', '.join(missing)}。"
            "shiori_ingest で差分同期してください"
        )

    return warnings


@mcp.tool(name="shiori_status")
def status() -> dict[str, Any]:
    """索引の鮮度と健全性を返す。リポジトリごとに最終同期の完了時刻（last_synced_at）・
    経過秒数（age_seconds）・実行経路（cli / runner / mcp / auto）・直近の更新件数・
    チャンク数内訳（doc / issue / pr_review / code）・issue_items 全件数・差分同期カーソル・
    警告（warnings）を返す。「索引は最新か?」の判断に使う:
    age_seconds が小さければ同期済み、大きい／last_synced_at が null なら
    shiori_ingest で差分同期すること。warnings があれば索引に異常がある可能性。"""
    with _conn() as conn:
        runs = db.get_sync_runs(conn)
        repos: dict[str, Any] = {}
        for repo in settings.repos:
            info = runs.get(repo) or {
                "last_synced_at": None,
                "age_seconds": None,
                "route": None,
                "docs_updated": None,
                "issues_indexed": None,
                "code_indexed": None,
            }
            chunk_counts = db.get_chunk_counts(conn, repo)
            items_in_db = db.get_issue_item_count(conn, repo)
            cursors = db.get_cursors(conn, repo)
            info["chunks"] = chunk_counts
            info["items_in_db"] = items_in_db
            info["cursors"] = cursors
            warnings = _build_warnings(info, chunk_counts, items_in_db, cursors)
            info["warnings"] = warnings
            repos[repo] = info
    return {
        "repos": repos,
        "sync_interval_seconds": settings.sync_interval_seconds,
    }


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
