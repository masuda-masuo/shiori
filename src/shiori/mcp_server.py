"""MCP server (detailed design/06).

Tool composition (decision: shiori_search is the unified entry point; keyword_search is kept separate for exact-match):
- search(query, filters?)             : Semantic + keyword hybrid search (entry point tool). Use this first.
- keyword_search(query, filters?)     : Exact-match-oriented keyword search (Japanese-aware). Use when exact match is needed.
- list_tree(path?, source_type?, extension?): Tree listing of indexed docs + code files.
- read_file(path, start?, end?)       : Read (part of) a file from the local clone.
- read_issue(number, repo?, exclude_noise_bots?) : Get full issue/PR thread.
- pr_changes(number, repo?, include_diff?) : Return PR change file map (+ unified diff if needed) (issue #54, #100).
- read_pr_file(number, path, start?, end?, repo?) : Transparently get PR head file content (issue #81).
- ingest(rebuild?, repo?)             : Incremental sync of docs / issue / PR / code (index update).
- status()                            : Index freshness and health (last sync time, count breakdown, warnings).

Tool names exposed to MCP clients have a shiori_ prefix (issue #8) to avoid name collisions
with other MCP servers (filesystem, etc.). Function names remain as-is.

Search results always include pointer + snippet + GitHub URL. Full text is retrieved via read tools.

Index update has 3 paths (decision from issue #2, #6):
- shiori_ingest tool: On-demand update by agent/user.
- SHIORI_SYNC_INTERVAL_SECONDS: Background auto-sync within the serve process (safety net).
  Recommended value is 3600 seconds since event-driven is the primary mode.
- self-hosted runner: Immediate incremental sync on push / issue / PR events (issue #6).
  Sync body is _do_sync(). PostgreSQL advisory lock ensures cross-process exclusive access.

On each successful sync, completion timestamp and route (mcp / auto. runner / cli are
recorded in ingest.py) are written to sync_runs, queryable via shiori_status (issue #22).

Code indexing (issue #33) is enabled via SHIORI_INDEX_CODE=true. Shares the same clone as
sync_docs, re-indexing only changed files via sha delta.

Security (issue #63):
- _do_sync repo argument is validated against settings.repos (allowlist); unknowns are rejected.
- shiori_ingest rebuild=True is only allowed when SHIORI_ALLOW_REBUILD=true.

Bulk path optimisation (issue #72):
  On first-run or rebuild, heavy indexes (HNSW / pgroonga) are created after data load,
  embeddings are batched across files, and chunk insertion is bulkified.
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
from .github_sync import (
    ChunkBuffer,
    _git,
    _git_delete_ref,
    _git_fetch_ref,
    sync_code,
    sync_docs,
    sync_issues,
)

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
    """Determine whether to use the bulk path: rebuild=True, or chunks table is empty / does not exist (issue #72)."""
    if rebuild:
        return True
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('chunks')")
        if cur.fetchone()[0] is None:
            return True
        cur.execute("SELECT count(*) FROM chunks")
        return cur.fetchone()[0] == 0


def _do_sync(
    repos: list[str] | None = None,
    rebuild: bool = False,
    route: str = "mcp",
) -> dict[str, Any]:
    """Sync body. Called from both the ingest tool and the auto-sync loop.

    Intra-process exclusion: _sync_lock (threading.Lock) for early skip.
    Cross-process exclusion: PostgreSQL advisory lock (pg_try_advisory_lock) prevents
    concurrent execution with runner jobs (separate process).
    Both are non-blocking (failure to acquire = skip).
    Records completion timestamp and route to sync_runs per repository (issue #22 / #33).

    First-run / rebuild uses the bulk path: creates heavy indexes after data load, batches embeddings (issue #72).
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
            # --- バルク経路判定（新規DB対応。issue #72） ---
            is_bulk = _is_bulk_path(conn, rebuild)

            # --- スキーマ準備: migrate_light は冪等なのでロック外でOK ---
            if is_bulk:
                log.info("bulk path detected (rebuild=%s), using light schema + deferred indexes", rebuild)
                db.migrate_light(conn, settings)
            else:
                db.migrate(conn, settings)

            # --- プロセス横断排他: advisory lock ---
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_KEY,))
                acquired = cur.fetchone()[0]
            if not acquired:
                return {"status": "skipped", "reason": "別プロセスで同期が実行中です"}
            try:
                # --- バルク経路: 破壊的操作はロック内で（issue #72） ---
                if is_bulk:
                    if rebuild:
                        log.warning("rebuild: 既存の索引と同期カーソルを破棄します")
                        with conn.cursor() as cur:
                            cur.execute(
                                "TRUNCATE chunks, doc_files, issue_items, sync_state"
                            )
                        conn.commit()
                    db.drop_heavy_indexes(conn)

                if is_bulk:
                    buffer = ChunkBuffer(conn, embedder, batch_size=_BULK_BUFFER_SIZE)

                for repo in targets:
                    n_docs = sync_docs(
                        settings, conn, embedder, repo, provider,
                        buffer=buffer if is_bulk else None,
                    )
                    if is_bulk:
                        buffer.flush()
                        conn.commit()  # メタデータをコミット
                    n_items = sync_issues(
                        settings, conn, embedder, repo, provider,
                        buffer=buffer if is_bulk else None,
                    )
                    if is_bulk:
                        buffer.flush()
                        conn.commit()  # メタデータをコミット
                    n_code = sync_code(
                        settings, conn, embedder, repo, provider,
                        buffer=buffer if is_bulk else None,
                    )
                    if is_bulk:
                        buffer.flush()
                        conn.commit()  # メタデータをコミット
                    finished_at = db.record_sync_run(
                        conn, repo, route, n_docs, n_items, n_code
                    )
                    result["repos"][repo] = {
                        "docs_updated": n_docs,
                        "issues_indexed": n_items,
                        "code_added": n_code,
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
    """Check if the filename has a document extension (case-insensitive)."""
    return any(filename.lower().endswith(ext) for ext in _DOC_EXTENSIONS)


def _is_excluded_file(filename: str) -> bool:
    """Check if the filename has an excluded extension (case-insensitive)."""
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in _EXCLUDE_EXTENSIONS)


def _match_extension(path: str, extension: str) -> bool:
    """Check if the extension matches the given value (case-insensitive, with or without leading '.')."""
    ext = extension if extension.startswith(".") else "." + extension
    return path.lower().endswith(ext.lower())


def _walk_code_files(base: str, prefix: str, extension: str | None = None) -> set[str]:
    """Walk the clone and return a set of relative paths for code files.

    - Skips noise directories (.git / node_modules / .venv / __pycache__ etc.)
    - Excludes .md / .mdx / .markdown (handled by doc_files table)
    - Excludes non-text extensions (binary, assets, lock files, etc.)
    - When prefix is given, only returns paths matching that prefix or its subtree
      (e.g. prefix="src" matches "src/main.py" but not "src2/main.py")
    - When extension is given, applies extension filter during walk (case-insensitive)
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
        "まず shiori_search で検索し、ポインタ＋スニペットを得て、"
        "必要な範囲だけ shiori_read_file / shiori_read_issue で取得すること。"
        "固有名詞・API 名・エラーコード・関数名等の厳密一致には shiori_keyword_search を使う。"
        "索引が古い・未索引と思われる場合（直近の変更がヒットしない等）は"
        "shiori_ingest を呼んで差分同期する。索引が最新かどうかの確認は shiori_status。"
        "コードファイルは shiori_list_tree で発見し shiori_read_file で読める"
        "（path, start_line, end_line 指定可）。"
        "shiori_list_tree は source_type='doc'/'code' や extension='.py' で絞り込み可能。"
        "コードの検索は shiori_search / shiori_keyword_search で可能"
        "（source_type='code' で絞り込み可、prog_lang フィルタで言語指定も可）。"
        "PR の変更ファイルマップは shiori_pr_changes で取得できる。"
        "PR head のファイル内容は shiori_read_pr_file で透過的に取得できる（issue #81）。"
        "\n"
        "■ 二ストア・モデル（情報の出所）\n"
        "shiori は 2 つの独立したデータソースを持つ:\n"
        "1. 索引（Postgres/pgvector/pgroonga）\n"
        "   - shiori_search / shiori_keyword_search: 埋め込み＋全文検索\n"
        "   - shiori_read_issue: issue/PR スレッド（索引済みのもののみ）\n"
        "   - shiori_list_tree (source_type='doc'): 索引済み doc_files テーブル\n"
        "   - shiori_pr_changes: PR 変更ファイルマップ（索引済みメタデータ）\n"
        "   - 鮮度は shiori_ingest / 自動同期に依存\n"
        "2. クローン（ディスク、main ブランチ固定）\n"
        "   - shiori_read_file: 実ファイルを直接読み取り（索引不要、クローンがあれば読める）\n"
        "   - shiori_read_pr_file: PR head のファイルを git 経由で取得（ワーキングツリー非破壊）\n"
        "   - shiori_list_tree (source_type='code'): os.walk で物理的に存在するコードファイル\n"
        "   - クローンは sync_docs が clone --depth=1 + reset --hard origin/HEAD で維持\n"
        "   - shiori_read_pr_file はクローンを起点に git fetch で PR head を取得して読む"
    ),
)


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
    """Semantic search (entry point tool). Strong for paraphrasing, concepts, and cross-lingual (Japanese query for English docs).
    Internally performs hybrid fusion with keyword search (RRF).
    Returns pointer (path / heading_path / issue_no) + snippet + URL. Full text via read tools.
    filters: source_type is doc / issue / pr_review / code, language is ja / en,
    state is open / closed, prog_lang is python / go / rust etc.,
    updated_after is ISO8601 date.
    sort_by: "score" (default) / "updated_at" / "created_at".
      Accepted for backward compatibility, but ranking is always relevance-primary (issue #69).
      Primary sources (doc/code) are RRF-score-ordered,
      secondary sources (issue/pr_review) use RRF score + state/updated_at tie-break.
      Pure date-replacement sort is not performed; use default (score).
    sort_order: "desc" (default) / "asc" (asc inverts the entire composite key).
    """
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
    """Keyword search (Japanese-aware tokenisation). Strong for exact matches of function names, API names,
    error codes, and config keys. Normally use shiori_search; use this tool when exact match is needed.
    code chunks are an OR search of content (signature + docstring) and symbols (tokenised identifiers),
    so partial matches of camelCase/snake_case are also found.
    sort_by: "score" (default) / "updated_at" / "created_at".
      Accepted for backward compatibility, but ranking is always relevance-primary (issue #69).
      Primary sources (doc/code) are score-ordered,
      secondary sources (issue/pr_review) use score + state/updated_at tie-break.
      Pure date-replacement sort is not performed; use default (score).
    sort_order: "desc" (default) / "asc" (asc inverts the entire composite key).
    """
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
) -> list[dict[str, Any]]:
    """List indexed docs + code file paths. When path is given, filter by that subtree.
    Use this to understand the repository structure and get file pointers.

    ■ Two-store model and meaning of source_type:
      - source_type='doc' (index): doc_files table (Postgres-indexed Markdown only).
        Unindexed documents are not returned.
      - source_type='code' (clone): os.walk result of the disk clone (main branch fixed).
        Returns files that exist physically even before indexing. Binary/lock files etc. are excluded.
      - When omitted: returns both, distinguishable by the source field per entry.

    Code files (.py, .ts, .go etc.) are also returned from the clone filesystem.
    Code is discoverable even before indexing.

    Returns a list of [{"path": ..., "source": "doc"|"code"}, ...] (sorted by path).

    source_type: 'doc' (indexed Markdown) /'code' (clone code files) filter.
                 When omitted, returns both. Invalid values raise an error.
    extension:   Filter by extension like '.py' or '.md' (leading dot optional).
                 Case-insensitive. Filters at each retrieval path for efficiency.
    """
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
    """Get full content (or start_line~end_line range) of the specified file.
    Only read the range truly needed from search result pointers.
    Can read both documents and code files.

    This tool reads from the local clone (main branch fixed), not from the index (Postgres).
    As long as the clone exists, files can be read even without an index.
    PR head / other ref / diff retrieval is delegated to GitHub MCP or shiori_read_pr_file.
    """
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

    hints: list[str] = []
    if end_line is None and total > _LARGE_FILE_THRESHOLD:
        hints.append(
            f"File is large ({total} lines). "
            "Use start_line/end_line for range-based reading."
        )

    result: dict[str, Any] = {
        "repo": target,
        "path": path,
        "start_line": s + 1,
        "end_line": e,
        "total_lines": total,
        "content": body,
    }
    if hints:
        result["hints"] = hints
    return result


def _read_issue_single(target: str, number: int, exclude_noise_bots: bool) -> dict[str, Any]:
    """Get a single issue (internal helper). Raises ValueError if not indexed."""
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
    # allowlist 外の bot を除外（issue #44）
    if exclude_noise_bots:
        allowlist = settings.index_bot_logins
        rows = [
            r for r in rows
            if not r[4] or (r[3] and r[3].lower() in allowlist)
        ]
        if not rows:
            raise ValueError(f"#{number} の全項目が allowlist 外の bot です")
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


@mcp.tool(name="shiori_read_issue")
def read_issue(
    number: int | None = None,
    repo: str | None = None,
    exclude_noise_bots: bool = False,
    numbers: list[int] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Get the full issue/PR thread (body + comments + review comments) chronologically.
    bot comments are also included (identifiable via is_bot).

    number: issue or PR number (single fetch; mutually exclusive with numbers).
    repo: Repository name ("owner/name"). When omitted, uses the first repo in SHIORI_REPOS.
    exclude_noise_bots: When True, exclude bots outside the allowlist (CI / dependabot etc.) (default False).
        Posts from bots registered in the allowlist (SHIORI_INDEX_BOT_LOGINS) remain (issue #44).
    numbers: Multiple issue/PR numbers (batch fetch). When set, returns an array.
"""
    if number is not None and numbers is not None:
        raise ValueError("number と numbers は同時に指定できません")
    target = _resolve_repo(repo)
    if numbers is not None:
        if len(numbers) > 50:
            raise ValueError(f"numbers は最大50件までです（{len(numbers)}件指定されました）")
        results: list[dict[str, Any]] = []
        for n in numbers:
            try:
                result = _read_issue_single(target, n, exclude_noise_bots)
                result["status"] = "ok"
                results.append(result)
            except ValueError as e:
                results.append({
                    "repo": target,
                    "number": n,
                    "status": "error",
                    "error": str(e),
                })
        return results
    if number is None:
        raise ValueError("number または numbers を指定してください")
    return _read_issue_single(target, number, exclude_noise_bots)


@mcp.tool(name="shiori_pr_changes")
def pr_changes(
    number: int,
    repo: str | None = None,
    include_diff: bool = False,
) -> dict[str, Any]:
    """Return PR change file map (metadata). Pointer-level tool (issue #54, #100).

    Holds (metadata):
      - head_sha: PR head commit SHA (tracks force-push)
      - File list: path / status / additions / deletions / changes / blob_url
      - diff: unified diff when include_diff=True (issue #100)

    number: PR number
    repo: Repository name ("owner/name"). When omitted, uses the first repo in SHIORI_REPOS.
    include_diff: When True, also fetch unified diff (default False). Fetches from GitHub's
        refs/pull/{N}/head via git fetch and returns the diff against merge-base.
    """
    target = _resolve_repo(repo)
    with _conn() as conn:
        files, head_sha = db.get_pr_changes(conn, target, number)
    if head_sha is None:
        raise ValueError(
            f"PR #{number} の変更ファイルマップが見つかりません。"
            "shiori_ingest で同期してください。"
        )
    result: dict[str, Any] = {
        "repo": target,
        "number": number,
        "head_sha": head_sha,
        "files": files,
    }
    if include_diff:
        base = os.path.realpath(settings.repo_dir(target))
        if not os.path.isdir(os.path.join(base, ".git")):
            raise FileNotFoundError(
                f"{target} のクローンが存在しません。shiori_ingest で同期してください。"
            )
        ref = f"pull/{number}/head"
        tmp_ref = None
        try:
            provider = build_token_provider(settings)
            tmp_ref = _git_fetch_ref(ref, cwd=base, provider=provider)
            result["diff"] = _git(
                ["diff", f"HEAD...{tmp_ref}", "--unified=3"], cwd=base
            )
        finally:
            if tmp_ref:
                _git_delete_ref(tmp_ref, cwd=base)
    return result


@mcp.tool(name="shiori_read_pr_file")
def read_pr_file(
    number: int,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Get PR head file content (or start_line~end_line range).
    After getting the changed file list from shiori_pr_changes, use this tool to read each file.
    Unlike shiori_read_file, this can transparently read files from the PR head branch.
    Fetches from GitHub's refs/pull/{N}/head via git fetch and extracts file content
    without modifying the working tree. Temporary refs are cleaned up automatically.

    number: PR number
    path: File path (e.g. "src/main.py")
    repo: Repository name ("owner/name"). When omitted, uses the first repo in SHIORI_REPOS.
    start_line: Start line (1-indexed)
    end_line: End line (1-indexed, defaults to last line)
    """
    target = _resolve_repo(repo)
    base = os.path.realpath(settings.repo_dir(target))

    if not os.path.isdir(os.path.join(base, ".git")):
        raise FileNotFoundError(
            f"{target} のクローンが存在しません。shiori_ingest で同期してください。"
        )

    ref = f"pull/{number}/head"
    tmp_ref = None
    try:
        provider = build_token_provider(settings)
        tmp_ref = _git_fetch_ref(ref, cwd=base, provider=provider)

        # git show でファイル内容を取得
        try:
            content = _git(["show", f"{tmp_ref}:{path}"], cwd=base)
        except RuntimeError as exc:
            raise FileNotFoundError(
                f"PR #{number} に {path} が見つかりません: {exc}"
            )

        lines = content.splitlines()
        total = len(lines)
        s = max((start_line or 1) - 1, 0)
        e = min(end_line or total, total)
        body = "\n".join(lines[s:e])

        hints: list[str] = []
        if end_line is None and total > _LARGE_FILE_THRESHOLD:
            hints.append(
                f"File is large ({total} lines). "
                "Use start_line/end_line for range-based reading."
            )

        result: dict[str, Any] = {
            "repo": target,
            "number": number,
            "path": path,
            "start_line": s + 1,
            "end_line": e,
            "total_lines": total,
            "content": body,
        }
        if hints:
            result["hints"] = hints
        return result
    finally:
        if tmp_ref:
            _git_delete_ref(tmp_ref, cwd=base)


@mcp.tool(name="shiori_ingest")
def ingest(rebuild: bool = False, repo: str | None = None) -> dict[str, Any]:
    """Sync docs / issue/PR / code from GitHub and update the index (incremental, normally a few seconds).
    Call when search results seem stale or not yet indexed (recent changes not hitting, user mentioning
    recent changes, etc.). rebuild=True discards the index and rebuilds from scratch
    (only when embedding model changes). When repo is omitted, syncs all configured repos.
    Code indexing is enabled with SHIORI_INDEX_CODE=true.

    rebuild=True is disabled by default from the MCP tool. Requires
    setting SHIORI_ALLOW_REBUILD=true (issue #63).
    Only repos in SHIORI_REPOS are valid.
    """
    if rebuild and not settings.allow_rebuild:
        raise ValueError(
            "rebuild=True は MCP ツールからは実行できません。"
            "CLI（python -m shiori ingest --rebuild）を使用するか、"
            "環境変数 SHIORI_ALLOW_REBUILD=true を設定してください。"
        )
    return _do_sync(repos=[repo] if repo else None, rebuild=rebuild, route="mcp")


_STALE_SECONDS = 86400  # 24 時間
_LARGE_FILE_THRESHOLD = 500  # この行数を超えるファイルに range 指定 hint を表示


def _build_warnings(
    info: dict,
    chunk_counts: dict[str, int],
    items_in_db: int,
    cursors: dict[str, str | None],
) -> list[str]:
    """Detect index anomalies and return a warning list (issue #31)."""
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
    """Return index freshness and health. Per-repository: last sync completion time (last_synced_at),
    elapsed seconds (age_seconds), execution route (cli / runner / mcp / auto), recent additions
    (docs_updated / issues_indexed / code_added), chunk count breakdown (chunks),
    code_chunks, issue_items total count, incremental sync cursors, warnings.
    Use to determine if the index is up to date:
    if age_seconds is small, the index is synced; if large or last_synced_at is null,
    call shiori_ingest for incremental sync. If warnings are present, the index may have issues.
    """
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
                "code_added": None,
            }
            chunk_counts = db.get_chunk_counts(conn, repo)
            items_in_db = db.get_issue_item_count(conn, repo)
            cursors = db.get_cursors(conn, repo)
            info["chunks"] = chunk_counts
            info["code_chunks"] = chunk_counts.get("code", 0)
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
