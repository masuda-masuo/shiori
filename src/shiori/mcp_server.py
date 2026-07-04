"""MCP server implementation.
~1100 lines: setup (1-90), helpers (90-310), tools (310-1100). Each tool typically <100 lines.
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

# PostgreSQL advisory lock key (cross-process mutex, shared with ingest.py)
# ASCII codes for 'SHIO' packed into 32 bits
SYNC_LOCK_KEY = 0x5348494F

# ChunkBuffer flush threshold for bulk path (issue #72)
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
        f"Please specify a repo. Configured: {', '.join(settings.repos) or '(none)'}"
    )


def _make_filters(
    source_type: str | None,
    language: str | None,
    state: str | None,
    repo: str | None,
    path_prefix: str | None,
    updated_after: str | None,
    prog_lang: str | None = None,
    kind: str | None = None,
) -> dict:
    return {
        "source_type": source_type,
        "language": language,
        "state": state,
        "repo": repo,
        "path_prefix": path_prefix,
        "updated_after": updated_after,
        "prog_lang": prog_lang,
        "kind": kind,
    }


def _is_bulk_path(conn, rebuild: bool) -> bool:
    """Determine bulk path: rebuild=True or chunks empty/missing (issue #72)."""
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
    """Incremental sync body. Called by both ingest tool and auto-sync loop.
    Process-level exclusion via _sync_lock (threading.Lock)."""
    # Allowlist validation: ensure specified repo is in settings.repos (issue #63)
    if repos is not None:
        allowed = set(settings.repos)
        invalid = sorted(set(repos) - allowed)
        if invalid:
            raise ValueError(
                f"Specified repos not in SHIORI_REPOS: "
                f"{', '.join(invalid)}"
            )

    if not _sync_lock.acquire(blocking=False):
        return {"status": "skipped", "reason": "sync already running"}
    try:
        targets = repos or settings.repos
        if not targets:
            return {"status": "error", "reason": "SHIORI_REPOS not set"}
        provider = build_token_provider(settings)
        embedder = _get_embedder()
        result: dict[str, Any] = {"status": "ok", "repos": {}}
        with _conn() as conn:
            # --- Bulk path detection (handles fresh DB. Issue #72) ---
            is_bulk = _is_bulk_path(conn, rebuild)

            # --- Schema prep: migrate_light is idempotent, safe outside lock ---
            if is_bulk:
                log.info("bulk path detected (rebuild=%s), using light schema + deferred indexes", rebuild)
                db.migrate_light(conn, settings)
            else:
                db.migrate(conn, settings)

            # --- Cross-process mutex: advisory lock ---
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_KEY,))
                acquired = cur.fetchone()[0]
            if not acquired:
                return {"status": "skipped", "reason": "sync already running in another process"}
            try:
                # --- Bulk path: destructive operations inside the lock (issue #72) ---
                if is_bulk:
                    if rebuild:
                        log.warning("rebuild: discarding existing index and sync cursors")
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
                        conn.commit()  # Commit metadata
                    n_items = sync_issues(
                        settings, conn, embedder, repo, provider,
                        buffer=buffer if is_bulk else None,
                    )
                    if is_bulk:
                        buffer.flush()
                        conn.commit()  # Commit metadata
                    n_code = sync_code(
                        settings, conn, embedder, repo, provider,
                        buffer=buffer if is_bulk else None,
                    )
                    if is_bulk:
                        buffer.flush()
                        conn.commit()  # Commit metadata
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

                # --- Bulk path: create heavy indexes in batch (issue #72) ---
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


# ── _walk_code_files: collect code files ──

# Document extensions (case-insensitive). Excluded from walk; handled by doc_files table
_DOC_EXTENSIONS = {".md", ".mdx", ".markdown"}

# Directory names to skip in os.walk (design 10 decision 7: noise reduction for quality and quantity)
_EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    ".venv", "venv",
    "dist", "build",
    "__pycache__",
    ".tox", ".eggs",
    ".next",  # Next.js
    "target",  # Rust
    ".cache",
}

# File extensions excluded from code listing (case-insensitive)
# Binary/asset/lock files that are not useful for LLM reading
_EXCLUDE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pyc", ".pyo",
    ".so", ".dylib", ".dll", ".wasm",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".pdf",
    ".lock",  # package-lock.json, yarn.lock, Gemfile.lock etc.
    ".min.js", ".min.css",  # minified
}


def _is_doc_file(filename: str) -> bool:
    """Check if filename has a document extension (case-insensitive)."""
    return any(filename.lower().endswith(ext) for ext in _DOC_EXTENSIONS)


def _is_excluded_file(filename: str) -> bool:
    """Check if filename has an excluded extension (case-insensitive)."""
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in _EXCLUDE_EXTENSIONS)


def _match_extension(path: str, extension: str) -> bool:
    """Check if extension matches given value (case-insensitive, with/without leading dot)."""
    ext = extension if extension.startswith(".") else "." + extension
    return path.lower().endswith(ext.lower())


def _walk_code_files(base: str, prefix: str, extension: str | None = None) -> set[str]:
    """Walk clone and return code file relative paths.
    Skips .git/node_modules/.venv, binary extensions.
    Only extensions in _CODE_EXTENSIONS."""
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
        "Hybrid search of GitHub repository knowledge (Markdown docs, issue/PR discussions,"
        "and source code)."
        "First search with shiori_search to get pointers + snippets, then fetch"
        "only the needed range via shiori_read_file / shiori_read_issue."
        "For exact match of proper nouns, API names, error codes, function names, use shiori_keyword_search."
        "If the index seems stale (recent changes not found), call shiori_ingest for diff sync."
        "Check index freshness with shiori_status."
        "Code files can be discovered via shiori_list_tree and read via shiori_read_file"
        "(supports path, start_line, end_line)."
        "shiori_list_tree supports filtering by source_type='doc'/'code' and extension='.py'."
        "Code can be searched via shiori_search / shiori_keyword_search"
        "(filter by source_type='code' and prog_lang filter)."
        "PR change file maps are available via shiori_pr_changes."
        "PR head file content can be read transparently via shiori_read_pr_file (issue #81)."
        "\n"
        "\u25a0 Two-store model (information sources)\n"
        "shiori has 2 independent data sources:\n"
        "1. Index (Postgres/pgvector/pgroonga)\n"
        "   - shiori_search / shiori_keyword_search: embedding + full-text search\n"
        "   - shiori_read_issue: issue/PR threads (indexed only)\n"
        "   - shiori_list_tree (source_type='doc'): indexed doc_files table\n"
        "   - shiori_pr_changes: PR change file maps (indexed metadata)\n"
        "   - Freshness depends on shiori_ingest / auto-sync\n"
        "2. Clone (disk, pinned to main branch)\n"
        "   - shiori_read_file: read real files directly (no index needed, works if clone exists)\n"
        "   - shiori_read_pr_file: get PR head files via git (non-destructive to working tree)\n"
        "   - shiori_list_tree (source_type='code'): physically existing code files via os.walk\n"
        "   - Clone is maintained by sync_docs via clone --depth=1 + reset --hard origin/HEAD\n"
        "   - shiori_read_pr_file fetches PR head via git fetch starting from the clone"
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
    kind: str | None = None,
    top_k: int | None = None,
    sort_by: str = "score",
    sort_order: str = "desc",
) -> list[dict[str, Any]]:
    """Semantic search (entry). Strong for paraphrasing, concept, cross-lingual queries.
    Hybrid with keyword search internally.
    kind: 'issue' | 'pr' — further filter source_type='issue'/'pr_review' results
          by thread type. No effect on doc/code results (issue #98)."""
    with _conn() as conn:
        return search.semantic_search(
            settings, conn, _get_embedder(), query,
            _make_filters(source_type, language, state, repo, path_prefix, updated_after, prog_lang, kind),
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
    kind: str | None = None,
    top_k: int | None = None,
    sort_by: str = "score",
    sort_order: str = "desc",
    match_all: bool = False,
) -> list[dict[str, Any]]:
    """Keyword search (Japanese tokenize). Strong for exact matches: function names, API names, error codes, config keys.
    Multi-token queries use OR matching by default (any token can match); tokens that match more/strongly rank higher.
    Pass match_all=True for AND behavior (all tokens must match the same chunk).
    kind: 'issue' | 'pr' — further filter source_type='issue'/'pr_review' results
          by thread type. No effect on doc/code results (issue #98)."""
    with _conn() as conn:
        return search.keyword_search(
            settings, conn, query,
            _make_filters(source_type, language, state, repo, path_prefix, updated_after, prog_lang, kind),
            top_k,
            sort_by,
            sort_order,
            match_all=match_all,
        )


# Valid values for list_tree source_type
_VALID_SOURCE_TYPES = {"doc", "code"}


@mcp.tool(name="shiori_list_tree")
def list_tree(
    path: str | None = None,
    source_type: str | None = None,
    extension: str | None = None,
    repo: str | None = None,
) -> list[dict[str, Any]]:
    """List indexed doc/code file paths. Filter by path/source_type/extension.
    Understand repo structure and locate files."""
    # Validation
    if source_type is not None and source_type not in _VALID_SOURCE_TYPES:
        raise ValueError(
            f"Invalid source_type: '{source_type}'."
            f"Valid values: {', '.join(sorted(_VALID_SOURCE_TYPES))}"
        )

    target = _resolve_repo(repo)
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. Indexed documents (doc_files table)
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

    # 2. Code files (clone filesystem)
    if source_type is None or source_type == "code":
        base = os.path.realpath(settings.repo_dir(target))
        prefix = path.rstrip("/") if path else ""
        code_paths = _walk_code_files(base, prefix, extension=extension)
        # No pre-sort needed on code side; final sort handles it
        for p in code_paths:
            if p not in seen:
                seen.add(p)
                entries.append({"path": p, "source": "code"})

    # Sort by path (when doc and code are interleaved)
    entries.sort(key=lambda e: e["path"])
    return entries


@mcp.tool(name="shiori_read_file")
def read_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Read full file (or range) from clone (not index).
    PR head files via read_pr_file or GitHub MCP."""
    target = _resolve_repo(repo)
    base = os.path.realpath(settings.repo_dir(target))
    full = os.path.realpath(os.path.join(base, path))
    if not full.startswith(base + os.sep):
        raise ValueError("cannot read path outside repository")
    if not os.path.isfile(full):
        raise FileNotFoundError(f"{path} does not exist in clone (sync may be needed)")
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
    """Fetch single issue (internal helper). Raises ValueError if not indexed."""
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
        raise ValueError(f"#{number} is not indexed (has ingest been run?)")
    # Exclude bots outside the allowlist (issue #44)
    if exclude_noise_bots:
        allowlist = settings.index_bot_logins
        rows = [
            r for r in rows
            if not r[4] or (r[3] and r[3].lower() in allowlist)
        ]
        if not rows:
            raise ValueError(f"#{number}: all items are bots outside the allowlist")
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
                "state": r[5],
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
    """Fetch full issue/PR thread chronologically (body + comments + review).
    Bot comments included (identifiable via is_bot).
    Each item has a state field: for kind='pr_review' it is the review
    submission state (APPROVED/COMMENTED/CHANGES_REQUESTED); for other
    kinds it is the overall issue state (open/closed)."""
    if number is not None and numbers is not None:
        raise ValueError("number and numbers cannot be specified together")
    target = _resolve_repo(repo)
    if numbers is not None:
        if len(numbers) > 50:
            raise ValueError(f"numbers supports up to 50 items ({len(numbers)} specified)")
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
        raise ValueError("specify number or numbers")
    return _read_issue_single(target, number, exclude_noise_bots)


@mcp.tool(name="shiori_pr_changes")
def pr_changes(
    number: int,
    repo: str | None = None,
    include_diff: bool = False,
) -> dict[str, Any]:
    """PR change file map (metadata pointer; issue #54, #100).
    Stored: head_sha / path / status / additions / deletions / changes / blob_url
    Not stored: patch hunks (via GitHub MCP) and PR head files (via shiori_read_pr_file)."""
    target = _resolve_repo(repo)
    with _conn() as conn:
        files, head_sha = db.get_pr_changes(conn, target, number)
    if head_sha is None:
        raise ValueError(
            f"PR #{number} change file map not found."
            "Please sync with shiori_ingest."
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
                f"Clone for {target} does not exist. Please run shiori_ingest to sync."
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
    """Read PR head file content (or range). Delegated from read_file with PR-specific fetch."""
    target = _resolve_repo(repo)
    base = os.path.realpath(settings.repo_dir(target))

    if not os.path.isdir(os.path.join(base, ".git")):
        raise FileNotFoundError(
            f"Clone for {target} does not exist. Please run shiori_ingest to sync."
        )

    ref = f"pull/{number}/head"
    tmp_ref = None
    try:
        provider = build_token_provider(settings)
        tmp_ref = _git_fetch_ref(ref, cwd=base, provider=provider)

        # Get file content via git show
        try:
            content = _git(["show", f"{tmp_ref}:{path}"], cwd=base)
        except RuntimeError as exc:
            raise FileNotFoundError(
                f"PR #{number}: {path} not found: {exc}"
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
    """Sync docs/issues/code from GitHub and update index (diff sync, typically seconds).
    rebuild=True: discard and full rebuild (requires SHIORI_ALLOW_REBUILD=true; issue #63).
    Also treated as rebuild when chunks table is empty."""
    if rebuild and not settings.allow_rebuild:
        raise ValueError(
            "rebuild=True cannot be executed from the MCP tool."
            "Use the CLI (python -m shiori ingest --rebuild) or"
            "set the environment variable SHIORI_ALLOW_REBUILD=true."
        )
    return _do_sync(repos=[repo] if repo else None, rebuild=rebuild, route="mcp")


_STALE_SECONDS = 86400  # 24 hours
_LARGE_FILE_THRESHOLD = 500  # Show range hint for files exceeding this line count


def _build_warnings(
    info: dict,
    chunk_counts: dict[str, int],
    items_in_db: int,
    cursors: dict[str, str | None],
) -> list[str]:
    """Detect index anomalies and return warning list (issue #31)."""
    warnings: list[str] = []

    # Freshness: long time since last sync
    age = info.get("age_seconds")
    if age is not None and age > _STALE_SECONDS:
        hours = age // 3600
        warnings.append(
            f"{hours} hours since last sync. Index may be stale"
        )

    # Structural gap: issue_items exists but chunks are extremely few
    # Include pr_review in comparison (prevent false positives on high-review repos. Issue #35)
    total_issue_chunks = chunk_counts.get("issue", 0) + chunk_counts.get("pr_review", 0)
    if items_in_db > 0 and total_issue_chunks < items_in_db // 2:
        warnings.append(
            f"issue_items has {items_in_db} rows but chunks[issue]+chunks[pr_review] has {total_issue_chunks}."
            "Bot exclusion (SHIORI_INDEX_BOT_LOGINS) or indexing gap possible"
        )

    # Unsynced categories: kinds without cursor in sync_state
    all_kinds = {"docs", "issues", "issue_comments", "pr_review_comments"}
    missing = [k for k in all_kinds if k not in cursors]
    if missing:
        warnings.append(
            f"Unsynced categories: {', '.join(missing)}."
            "Please run shiori_ingest for diff sync"
        )

    return warnings


@mcp.tool(name="shiori_status")
def status() -> dict[str, Any]:
    """Index freshness and health. Per-repo: last_synced_at, age_seconds, route, counts, items, cursor, warnings."""
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
