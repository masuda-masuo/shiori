"""MCP server implementation.
~1100 lines: setup (1-90), helpers (90-310), tools (310-1100). Each tool typically <100 lines.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
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


def _infer_repo_from_cwd() -> str | None:
    """Infer repo from git remote of current working directory."""
    try:
        result = subprocess.run(  # noqa: S607
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        remote_url = result.stdout.strip()
        if "github.com" not in remote_url:
            return None
        path_part = remote_url.split("github.com")[-1].lstrip("/:")
        candidate = path_part.replace(".git", "").strip()
        if candidate.count("/") == 1:
            return candidate if candidate in settings.repos else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _validate_repo_name(repo: str) -> str:
    """Validate/resolve an explicit ``repo`` argument (issue #189).

    Before this, any non-empty ``repo`` string was passed through
    unchanged, so an unresolvable repo name and a resolvable-but-not-yet-
    indexed repo both surfaced downstream as the *same* "not indexed"
    error -- which points the caller at a useless ``ingest`` retry when
    the real problem is the argument itself.  This separates the two:

    - Exact ``"owner/name"`` match in ``settings.repos`` -> returned as-is.
    - A short name (no ``/``) that uniquely matches one configured repo's
      ``name`` component -> resolved to the full ``"owner/name"`` form
      (e.g. ``"shiori"`` -> ``"masuda-masuo/shiori"``).
    - A short name matching more than one configured repo -> ``ValueError``
      listing the ambiguous candidates.
    - Anything else (unresolvable full name or short name) -> ``ValueError``
      with the full indexed-repo list, so callers can tell "unknown repo"
      apart from "known repo, not indexed yet".

    When ``SHIORI_REPOS`` is unset (``settings.repos`` empty) there is
    nothing configured to validate against, so *repo* is returned
    unchanged (matches pre-#189 behavior for that case).
    """
    if not settings.repos:
        return repo
    if repo in settings.repos:
        return repo
    if "/" not in repo:
        matches = [r for r in settings.repos if r.split("/", 1)[-1] == repo]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f'ambiguous repo "{repo}". Candidates: {", ".join(matches)}. '
                'Specify the full "owner/repo" form.'
            )
    raise ValueError(
        f'unknown repo "{repo}". Indexed repos: {", ".join(settings.repos)}'
    )


def _resolve_repo(repo: str | None) -> str:
    if repo:
        return _validate_repo_name(repo)
    if not settings.repos:
        raise ValueError("SHIORI_REPOS not set")
    inferred = _infer_repo_from_cwd()
    if inferred:
        return inferred
    log.info(
        "repo not specified and could not be inferred from cwd; "
        "falling back to %s (configured: %s)",
        settings.repos[0],
        ", ".join(settings.repos),
    )
    return settings.repos[0]


def _resolve_repo_filter(repo: str | None) -> str | None:
    """Resolve an optional ``repo`` *search filter* (issue #189).

    Unlike :func:`_resolve_repo`, ``None`` here means "no filter -- search
    across every configured repo", not "fall back to the default repo",
    so ``None`` passes through unchanged.  A given value is still
    validated / short-name-resolved via :func:`_validate_repo_name`.
    """
    if repo is None:
        return None
    return _validate_repo_name(repo)


def _resolve_repos(repo: str | None) -> list[str]:
    """Resolve repo parameter to a list of target repos.

    repo="*" returns all configured repos (cross-repo search).
    repo="owner/name" returns that single repo.
    repo=None returns the default single repo via _resolve_repo (backward compat).
    """
    if repo == "*":
        if not settings.repos:
            raise ValueError("SHIORI_REPOS not set")
        return list(settings.repos)
    return [_resolve_repo(repo)]


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


def _record_pre_loop_sync_failure(targets: list[str], error: str) -> None:
    """Best-effort record of a sync failure that happened before any
    repo-scoped work started -- token provider construction, embedder
    creation (issue #196; #195 is the concrete case that exposed this: a
    missing embedding dependency raises out of _get_embedder() before the
    per-repo loop's own except ever runs, so sync_runs stays silent even
    though the sync is, in effect, failing for every configured repo).

    Opens its own short-lived connection since the caller may not have one
    yet at this point in _do_sync(). Swallows its own errors -- if the DB
    itself is unreachable, this is a no-op and the caller's original
    exception still propagates unchanged (that case is instead covered by
    the module-level _auto_sync_last_error state set by _auto_sync_loop).
    """
    try:
        with _conn() as conn:
            for repo in targets:
                db.record_sync_attempt(conn, repo, success=False, error=error)
    except Exception:
        log.exception("failed to record pre-loop sync failure for %s", targets)


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
        try:
            provider = build_token_provider(settings)
            embedder = _get_embedder()
        except Exception as exc:
            # Failures here (e.g. incomplete GitHub App config -- same root
            # cause as #193; or a missing embedding dependency -- #195) happen
            # before any repo-scoped work starts, so the per-repo except below
            # never runs and sync_runs stays silent even though every target
            # repo is, in effect, failing right now (issue #196).
            _record_pre_loop_sync_failure(targets, str(exc))
            raise
        result: dict[str, Any] = {"status": "ok", "repos": {}}
        with _conn() as conn:
            try:
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
            except Exception as exc:
                # Same rationale as the pre-loop try/except above: a failure in
                # bulk-path detection, migration, or lock acquisition is not
                # yet inside the per-repo loop's own except (issue #196). This
                # one has a live conn, so record directly instead of opening a
                # throwaway one.
                conn.rollback()
                for repo in targets:
                    db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                raise
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

                failed_repos: dict[str, str] = {}
                for repo in targets:
                    try:
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
                        db.record_sync_attempt(conn, repo, success=True)
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
                    except Exception as exc:
                        # Record the failed attempt so shiori_status can report it
                        # (issue #187). record_sync_run / record_sync_attempt each
                        # commit on their own (db.py), so this rollback only
                        # discards *this* repo's own uncommitted work -- any
                        # earlier repo in this same loop already landed via its
                        # own commit and is unaffected (issue #199 rollback-scope
                        # question).
                        conn.rollback()
                        db.record_sync_attempt(conn, repo, success=False, error=str(exc))
                        if is_bulk:
                            # Bulk (initial full ingest) has no per-repo resume
                            # story, so a partial failure still aborts the whole
                            # run immediately, same as before (issue #199).
                            raise
                        # Diff sync (normal operation): one repo's failure must
                        # not block the rest (issue #199) -- record and move on,
                        # then raise an aggregate error once every repo has had
                        # a chance to run.
                        failed_repos[repo] = str(exc)
                        log.exception(
                            "sync failed for %s (route=%s), continuing with "
                            "remaining repos", repo, route,
                        )

                # --- Bulk path: create heavy indexes in batch (issue #72) ---
                if is_bulk:
                    db.create_heavy_indexes(conn)

                if failed_repos:
                    detail = "; ".join(f"{r}: {e}" for r, e in failed_repos.items())
                    raise RuntimeError(
                        f"sync failed for {len(failed_repos)}/{len(targets)} "
                        f"repo(s): {detail}"
                    )

            finally:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_KEY,))
        return result
    finally:
        _sync_lock.release()


# Handle to the running auto-sync thread, set by run() (issue #187). Lets
# shiori_status report whether the loop is actually alive instead of just
# echoing the sync_interval_seconds config value, which stays the same even
# if the thread was never started or has died.
_auto_sync_thread: threading.Thread | None = None

# Module-level: error message from the most recent failed auto-sync loop
# iteration (issue #196). None means the
# last iteration succeeded (or none has run yet). This is a last-resort
# signal: it is set even when _do_sync() fails before ever reaching a live
# DB connection (e.g. the database itself is unreachable), a case
# record_sync_attempt cannot cover no matter where it is called from, since
# it always needs a connection to write through.
_auto_sync_last_error: str | None = None


def _auto_sync_loop(interval: int) -> None:
    global _auto_sync_last_error
    while True:
        time.sleep(interval)
        try:
            log.info("auto sync: %s", _do_sync(route="auto"))
            _auto_sync_last_error = None
        except Exception as exc:
            log.exception("auto sync failed")
            _auto_sync_last_error = str(exc)


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
        "Project-knowledge search MCP: one unified, cross-lingual (ja/en) index over GitHub "
        "repository knowledge — Markdown docs, source code, and issue/PR discussions — searchable "
        "in a single query and traversable across sources (issue -> fix PR -> changed files -> docs). "
        "Not a RAG server — shiori returns pointers + snippets and never generates answers; "
        "you decide what to fetch. "
        "First search with shiori_search to get pointers + snippets, then fetch "
        "only the needed range via shiori_read_file / shiori_read_issue. "
        "For exact match of proper nouns, API names, error codes, function names, use shiori_keyword_search. "
        "Check index freshness with shiori_status. "
        "Code files can be discovered via shiori_list_tree and read via shiori_read_file "
        "(supports path, start_line, end_line). "
        "shiori_list_tree supports filtering by source_type='doc'/'code' and extension='.py'. "
        "Code can be searched via shiori_search / shiori_keyword_search "
        "(filter by source_type='code' and prog_lang filter). "
        "PR change file maps are available via shiori_pr_changes. "
        "PR head file content can be read transparently via shiori_read_pr_file (issue #81). "
        "PR diffs via shiori_pr_diff (unified diff, optionally scoped to one path; issue #96). "
        "PR review comments (with path/line) via shiori_pr_review_comments. "
        "Issue/PR cross-references (closes/duplicate/refs/mention, inbound+outbound) "
        "via shiori_issue_links (issue #97) — useful for duplicate checks and tracing fixes. "
        "\n"
        "■ Two-store model (information sources)\n"
        "shiori has 2 independent data sources:\n"
        "1. Index (Postgres/pgvector/pgroonga)\n"
        "   - shiori_search / shiori_keyword_search: embedding + full-text search\n"
        "   - shiori_read_issue: issue/PR threads (indexed only)\n"
        "   - shiori_list_tree (source_type='doc'): indexed doc_files table\n"
        "   - shiori_pr_changes: PR change file maps (indexed metadata)\n"
        "   - Freshness depends on auto-sync / CLI ingest\n"
        "2. Clone (disk, pinned to main branch)\n"
        "   - shiori_read_file: read real files directly (no index needed, works if clone exists)\n"
        "   - shiori_read_pr_file: get PR head files via git (non-destructive to working tree)\n"
        "   - shiori_list_tree (source_type='code'): physically existing code files via os.walk\n"
        "   - Clone is maintained by sync_docs via clone --depth=1 + reset --hard origin/HEAD\n"
        "   - shiori_read_pr_file fetches PR head via git fetch starting from the clone\n"
        "3. Clone grep (ripgrep)\n"
        "   - shiori_grep: line-level search in clone files via ripgrep\n"
        "   - Use after shiori_search/keyword_search to narrow down matches to specific lines\n"
        "   - Supports regex, fixed-strings, and context lines"
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
          by thread type. No effect on doc/code results (issue #98).
    repo: "owner/name" filter, or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit to search across all indexed repos."""
    with _conn() as conn:
        return search.semantic_search(
            settings, conn, _get_embedder(), query,
            _make_filters(source_type, language, state, _resolve_repo_filter(repo), path_prefix, updated_after, prog_lang, kind),
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
          by thread type. No effect on doc/code results (issue #98).
    repo: "owner/name" filter, or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit to search across all indexed repos."""
    with _conn() as conn:
        return search.keyword_search(
            settings, conn, query,
            _make_filters(source_type, language, state, _resolve_repo_filter(repo), path_prefix, updated_after, prog_lang, kind),
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
    Understand repo structure and locate files.
    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo."""
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
    PR head files via read_pr_file or GitHub MCP.
    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo."""
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
        raise ValueError(f"#{number} is not indexed (run CLI ingest first)")
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
    kinds it is the overall issue state (open/closed).
    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo. An unresolvable repo
          raises immediately with the indexed-repo list, distinct from
          the "not indexed" error for a known repo whose issue hasn't
          been ingested yet."""
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
    Not stored: patch hunks (via GitHub MCP) and PR head files (via shiori_read_pr_file).
    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo."""
    target = _resolve_repo(repo)
    with _conn() as conn:
        files, head_sha, base_sha = db.get_pr_changes(conn, target, number)
    if head_sha is None:
        raise ValueError(
            f"PR #{number} change file map not found. "
            "Check shiori_status and sync if stale."
        )
    result: dict[str, Any] = {
        "repo": target,
        "number": number,
        "head_sha": head_sha,
        "files": files,
    }
    if base_sha is not None:
        result["base_sha"] = base_sha
    if include_diff:
        diff_text, stat_text = _compute_pr_diff(
            number, target, base_sha, path=None
        )
        result["diff"] = diff_text
        if stat_text:
            result["stats"] = stat_text
    return result


def _compute_pr_diff(
    number: int,
    target: str,
    base_sha: str | None,
    path: str | None = None,
) -> tuple[str, str]:
    """Fetch PR head and compute unified diff + stat (issue #96).

    Returns (diff_text, stat_text). Raises FileNotFoundError if clone
    is missing, or ValueError if the PR is not in the DB.
    """
    git_dir = os.path.realpath(settings.repo_dir(target))
    if not os.path.isdir(os.path.join(git_dir, ".git")):
        raise FileNotFoundError(
            f"Clone for {target} does not exist. Run python -m shiori ingest first."
        )

    ref = f"pull/{number}/head"
    tmp_ref = None
    tmp_base = None
    try:
        provider = build_token_provider(settings)
        tmp_ref = _git_fetch_ref(ref, cwd=git_dir, provider=provider)

        if base_sha:
            tmp_base = _git_fetch_ref(
                base_sha, cwd=git_dir, provider=provider,
            )
            diff_base = tmp_base
        else:
            diff_base = "HEAD"

        args = ["diff", f"{diff_base}..{tmp_ref}", "--unified=3"]
        if path:
            args.extend(["--", path])
        diff_text = _git(args, cwd=git_dir)
        stat_text = _git(
            ["diff", f"{diff_base}..{tmp_ref}", "--stat"], cwd=git_dir
        )
        return diff_text, stat_text.strip() if stat_text else ""
    finally:
        if tmp_ref:
            _git_delete_ref(tmp_ref, cwd=git_dir)
        if tmp_base:
            _git_delete_ref(tmp_base, cwd=git_dir)


@mcp.tool(name="shiori_pr_diff")
def pr_diff(
    number: int,
    path: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """PR の変更差分を返す（issue #96）。

    PRのheadとbaseの差分を取得する。git diff で計算し、ファイル単位の
    差分を返す。path を指定すると特定ファイルの差分のみ返す。

    number: PR番号
    path: 特定ファイルのパスのみ取得（省略時は全ファイル）
    repo: "owner/name"形式。一意に定まる短縮名（"owner/"なし）も可
          （例: "shiori" -> "owner/shiori"）。省略時は既定の設定済みリポジトリ。
    """
    target = _resolve_repo(repo)
    with _conn() as conn:
        _, head_sha, base_sha = db.get_pr_changes(conn, target, number)

    if head_sha is None:
        raise ValueError(
            f"PR #{number} change file map not found. "
            "Check shiori_status and sync if stale."
        )

    diff_text, stat_text = _compute_pr_diff(number, target, base_sha, path)

    return {
        "repo": target,
        "number": number,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "diff": diff_text,
        "stats": stat_text,
    }


@mcp.tool(name="shiori_pr_review_comments")
def pr_review_comments(number: int, repo: str | None = None) -> dict[str, Any]:
    """PR のレビューコメント一覧を返す（issue #96）。

    issue_items テーブルに保存済みの kind='pr_review_comment' を取得する。
    ファイルパス、行番号、本文、作成者、作成日時を含む。
    レビュー履歴の確認や他のレビュアーのコメント把握に使う。

    repo: "owner/name"形式。一意に定まる短縮名（"owner/"なし）も可
          （例: "shiori" -> "owner/shiori"）。省略時は既定の設定済みリポジトリ。
    """
    target = _resolve_repo(repo)
    with _conn() as conn:
        comments = db.get_pr_review_comments(conn, target, number)
    return {
        "repo": target,
        "number": number,
        "count": len(comments),
        "comments": comments,
    }


@mcp.tool(name="shiori_read_pr_file")
def read_pr_file(
    number: int,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Read PR head file content (or range). Delegated from read_file with PR-specific fetch.
    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo."""
    target = _resolve_repo(repo)
    base = os.path.realpath(settings.repo_dir(target))

    if not os.path.isdir(os.path.join(base, ".git")):
        raise FileNotFoundError(
            f"Clone for {target} does not exist. Run python -m shiori ingest first."
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


@mcp.tool(name="shiori_grep")
def grep_search(
    pattern: str,
    repo: str | None = None,
    path: str | None = None,
    regex: bool = True,
    ignore_case: bool = True,
    max_results: int = 200,
) -> dict[str, Any]:
    """Grep clone files with ripgrep. Stage-2 search after shiori_search/keyword_search
    narrowed down the target file. Returns line-level matches.

    When repo="*", search across all configured repositories.
    Each match includes a "repo" field identifying the source repository.

    pattern: search pattern (regex or fixed string)
    repo: target repo ("owner/name"), a short name if it uniquely matches
          one configured (indexed) repo (e.g. "shiori" -> "owner/shiori"),
          "*" for all repos, or None for default
    path: optional file/subdir path within repo to scope the search
    regex: True (default) for regex search, False for fixed-string search.
          Patterns containing literal ``[...]`` (character classes) should use
          ``regex=False`` to avoid silent misinterpretation.
    ignore_case: case-insensitive search (default True)
    max_results: maximum matches to return (default 200)
    """
    targets = _resolve_repos(repo)

    all_matches: list[dict[str, Any]] = []
    total = 0
    skipped_repos: list[str] = []

    for target in targets:
        base = os.path.realpath(settings.repo_dir(target))

        if not os.path.isdir(base):
            skipped_repos.append(target)
            continue

        if path:
            search_path = os.path.join(base, path)
            resolved = os.path.realpath(search_path)
            if not resolved.startswith(os.path.realpath(base) + os.sep) and resolved != os.path.realpath(base):
                raise ValueError("path must be inside the repository")
        else:
            resolved = base

        cmd = ["rg", "-n", "--no-heading", "--color", "never"]
        if ignore_case:
            cmd.append("-i")
        if not regex:
            cmd.append("--fixed-strings")
        cmd.extend(["-e", pattern])
        cmd.append(resolved)

        try:
            rg_result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError:
            raise RuntimeError("ripgrep (rg) is not installed in this container")

        if rg_result.returncode not in (0, 1):
            msg = f"rg failed (exit {rg_result.returncode}): {rg_result.stderr.strip()}"
            if rg_result.returncode == 2:
                msg += " (regex parse error. If you intended a literal search, retry with regex=False)"
            raise RuntimeError(msg)

        if rg_result.stdout:
            for line in rg_result.stdout.splitlines():
                if not line.strip() or line.startswith("--"):
                    continue
                parts = line.split(":", 2)
                if len(parts) >= 2:
                    try:
                        int(parts[1])
                        total += 1
                        text = parts[2] if len(parts) > 2 else ""
                        if len(all_matches) < max_results:
                            rel_path = parts[0]
                            if rel_path.startswith(base + "/"):
                                rel_path = rel_path[len(base) + 1:]
                            all_matches.append({
                                "repo": target,
                                "path": rel_path,
                                "line": int(parts[1]),
                                "text": text,
                            })
                    except ValueError:
                        pass

    truncated = total > max_results

    return {
        "pattern": pattern,
        "path": path or "",
        "total_matches": total,
        "truncated": truncated,
        "matches": all_matches,
        "skipped_repos": skipped_repos,
    }

_REPORT_TEMPLATES: dict[str, str] = {
    "stats": "Language statistics via tokei (files / code / comments / blanks).",
    "symbol_index": "Symbol index via universal-ctags (name / kind / visibility / location).",
    "module_tree": "Mermaid mindmap of repository structure (directory → file → class → function).",
}


def _run_ctags(target_path: str, base: str) -> list[dict[str, Any]]:
    """Run universal-ctags and return list of parsed symbol dicts.

    Shared helper used by symbol_index and module_tree (issue #155) templates.
    Paths in the result are relative to *base*.
    """
    try:
        result = subprocess.run(
            ["ctags", "-R", "--output-format=json", "--fields=+naZ", "-f", "-", target_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "universal-ctags is not installed in this container. "
            "Add universal-ctags to the Dockerfile apt-get install line."
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"ctags failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    symbols: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        path = entry.get("path", "")
        if path.startswith(base + "/"):
            path = path[len(base) + 1:]

        symbols.append({
            "name": entry.get("name", ""),
            "path": path,
            "line": entry.get("line", 0),
            "kind": entry.get("kind", ""),
            "access": entry.get("access", ""),
            "scope": entry.get("scope", ""),
            "scope_kind": entry.get("scopeKind", ""),
        })

    return symbols


@mcp.tool(name="shiori_report")
def report(
    template: str,
    repo: str | None = None,
    path: str | None = None,
    kind: str | None = None,
    public_only: bool = False,
    max_results: int = 500,
) -> dict[str, Any]:
    """Generate a structured report about a repository.

    template: report type ("stats" for language statistics via tokei,
              "symbol_index" for symbol index via universal-ctags,
              "module_tree" for Mermaid mindmap of repo structure)
    repo: target repo ("owner/name"), or a short name if it uniquely
          matches one configured (indexed) repo (e.g. "shiori" ->
          "owner/shiori"); None for default
    path: optional subdirectory within the repo to scope the report
    kind: ctags kind filter (e.g. "function", "class"; symbol_index only)
    public_only: exclude private/protected symbols (symbol_index only)
    max_results: maximum nodes/symbols to return, default 500 (symbol_index/module_tree)
    """
    if template not in _REPORT_TEMPLATES:
        raise ValueError(
            f"Unknown template: '{template}'. "
            f"Valid templates: {', '.join(sorted(_REPORT_TEMPLATES))}"
        )

    target = _resolve_repo(repo)
    base = os.path.realpath(settings.repo_dir(target))

    if not os.path.isdir(base):
        raise FileNotFoundError(
            f"Clone for {target} does not exist. Run python -m shiori ingest first."
        )

    if path:
        resolved = os.path.realpath(os.path.join(base, path))
        if not resolved.startswith(base + os.sep):
            raise ValueError("path must be inside the repository")
        target_path = resolved
    else:
        target_path = base

    if template == "stats":
        markdown = _report_stats(target_path)
    elif template == "module_tree":
        result = _report_module_tree(
            target_path=target_path,
            base=base,
            max_nodes=max_results,
        )
        return {
            "repo": target,
            "template": template,
            "markdown": result["markdown"],
            "truncated": result["truncated"],
        }

    elif template == "symbol_index":
        result = _report_symbol_index(
            target_path=target_path,
            base=base,
            kind=kind,
            public_only=public_only,
            max_results=max_results,
        )
        markdown = result["markdown"]
        truncated = result["truncated"]

        return {
            "repo": target,
            "template": template,
            "markdown": markdown,
            "truncated": truncated,
        }

    return {
        "repo": target,
        "template": template,
        "markdown": markdown,
    }


def _report_stats(target_path: str) -> str:
    """Run tokei and format output as a Markdown table."""
    try:
        result = subprocess.run(
            ["tokei", "--output", "json", target_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "tokei is not installed in this container. "
            "Add tokei to the Dockerfile apt-get install line."
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"tokei failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    data = json.loads(result.stdout)

    sorted_langs = sorted(
        (lang for lang in data if lang != "Total"),
        key=lambda k: k.lower(),
    )

    rows: list[str] = []
    total_files = 0

    for lang in sorted_langs:
        info = data[lang]
        n_files = len(info.get("reports", []))
        code = info.get("code", 0)
        comments = info.get("comments", 0)
        blanks = info.get("blanks", 0)
        total_files += n_files

        rows.append(
            f"| {lang} | {n_files} | {code} | {comments} | {blanks} |"
        )

    total = data.get("Total", {})
    header = "| Language | Files | Code | Comments | Blanks |"
    sep = "| --- | --- | --- | --- | --- |"
    total_row = (
        f"| **Total** | **{total_files}** | "
        f"**{total.get('code', 0)}** | **{total.get('comments', 0)}** | "
        f"**{total.get('blanks', 0)}** |"
    )

    return "\n".join([header, sep] + rows + [total_row])


def _report_symbol_index(
    target_path: str,
    base: str,
    kind: str | None = None,
    public_only: bool = False,
    max_results: int = 500,
) -> dict[str, Any]:
    """Run universal-ctags and format output as a Markdown table.

    Returns dict with "markdown" (str) and "truncated" (bool).
    """
    symbols = _run_ctags(target_path, base)

    if kind:
        symbols = [s for s in symbols if s["kind"] == kind]

    if public_only:
        symbols = [
            s for s in symbols
            if s["access"] not in ("private", "protected")
        ]

    symbols.sort(key=lambda s: (s["path"], s["line"]))

    total = len(symbols)
    truncated = total > max_results
    shown = symbols[:max_results]

    rows: list[str] = []
    for s in shown:
        visibility = s["access"] if s["access"] else ""
        rows.append(
            f"| {s['name']} | {s['kind']} | {visibility} | {s['path']}:{s['line']} |"
        )

    header = "| symbol | kind | visibility | location |"
    sep = "| --- | --- | --- | --- |"

    parts: list[str] = [header, sep] + rows
    if truncated:
        parts.append("")
        parts.append(f"*Truncated: showing {max_results} of {total} symbols.*")

    return {
        "markdown": "\n".join(parts),
        "truncated": truncated,
    }


_MODULE_TREE_MAX_NODES = 300

_TREE_DIR = "d"
_TREE_FILE = "f"
_TREE_SYM = "s"


def _report_module_tree(
    target_path: str,
    base: str,
    max_nodes: int = _MODULE_TREE_MAX_NODES,
) -> dict[str, Any]:
    """Build a Mermaid mindmap of the repo structure from ctags data.

    Returns dict with "markdown" and "truncated".
    """
    symbols = _run_ctags(target_path, base)

    file_paths: set[str] = set()
    for s in symbols:
        file_paths.add(s["path"])

    all_dirs: set[str] = set()
    for fp in sorted(file_paths):
        d = os.path.dirname(fp)
        while d and d != ".":
            all_dirs.add(d)
            d = os.path.dirname(d)

    def _count_nodes(nodes: list[dict]) -> int:
        n = len(nodes)
        for x in nodes:
            n += _count_nodes(x.get("children", []))
        return n

    def _build_tree(dir_path: str) -> list[dict]:
        children: list[dict] = []
        for d in sorted(x for x in all_dirs if os.path.dirname(x) == dir_path):
            sub = _build_tree(d)
            children.append(dict(name=os.path.basename(d), children=sub, t=_TREE_DIR))
        for fp in sorted(x for x in file_paths if os.path.dirname(x) == dir_path):
            file_syms = [s for s in symbols if s["path"] == fp]
            sym_children = _build_symbol_children(file_syms)
            children.append(dict(name=os.path.basename(fp), children=sym_children, t=_TREE_FILE))
        return children

    def _build_symbol_children(file_syms: list[dict]) -> list[dict]:
        top = [s for s in file_syms if not s.get("scope")]
        children: list[dict] = []
        for s in top:
            if not s["name"]:
                continue
            subs = [x for x in file_syms if x.get("scope") == s["name"]]
            sub_c = []
            for ss in subs:
                if ss["name"]:
                    sub_c.append(dict(name=ss["name"], children=[], t=_TREE_SYM))
            children.append(dict(name=s["name"], children=sub_c, t=_TREE_SYM))
        return children

    tree = _build_tree("")
    total_nodes = _count_nodes(tree)
    truncated = total_nodes > max_nodes

    if truncated:
        def _strip_symbols(nodes: list[dict]) -> list[dict]:
            result: list[dict] = []
            for n in nodes:
                if n.get("t") == _TREE_SYM:
                    continue
                kids = _strip_symbols(n.get("children", []))
                result.append(dict(name=n["name"], children=kids, t=n.get("t")))
            return result
        tree = _strip_symbols(tree)
        total_nodes = _count_nodes(tree)

    root_name = os.path.basename(target_path.rstrip("/")) or os.path.basename(base)
    lines = ["```mermaid", "mindmap", f"  root(({root_name}))"]

    def _render(nodes: list[dict], indent: int = 2) -> list[str]:
        result: list[str] = []
        for n in nodes:
            result.append(f"{'  ' * indent}{n['name']}")
            if n.get("children"):
                result.extend(_render(n["children"], indent + 1))
        return result

    lines.extend(_render(tree, indent=3))
    if truncated:
        lines.append(f"  *Truncated: showing {total_nodes} directory nodes (symbol level omitted)*")
    lines.append("```")

    return {"markdown": '\n'.join(lines), "truncated": truncated}


def ingest(rebuild: bool = False, repo: str | None = None) -> dict[str, Any]:
    """Sync docs/issues/code from GitHub and update index (diff sync, typically seconds).
    Check index freshness with shiori_status first — auto-sync keeps the index
    fresh, so ingest is normally unnecessary. Call this only when shiori_status
    reports the index is stale.
    rebuild=True: discard and full rebuild (requires SHIORI_ALLOW_REBUILD=true; issue #63).
    Also treated as rebuild when chunks table is empty."""
    if rebuild and not settings.allow_rebuild:
        raise ValueError(
            "rebuild=True cannot be executed from the MCP tool. "
            "Use the CLI (python -m shiori ingest --rebuild) or "
            "set the environment variable SHIORI_ALLOW_REBUILD=true."
        )
    return _do_sync(repos=[repo] if repo else None, rebuild=rebuild, route="mcp")


_STALE_SECONDS = 86400  # 24 hours; used when auto sync is disabled (sync_interval_seconds <= 0)

# When auto sync is enabled, the stale threshold scales with the configured
# interval instead of the fixed 24h window above (issue #187): a fixed window
# let a completely dead auto-sync loop look "healthy" for up to a day even
# when it was supposed to run every 10 seconds. _STALE_INTERVAL_MULTIPLIER *
# sync_interval_seconds, floored at _STALE_SECONDS_FLOOR to avoid flapping
# warnings on very short intervals.
_STALE_INTERVAL_MULTIPLIER = 30
_STALE_SECONDS_FLOOR = 300  # 5 minutes

_LARGE_FILE_THRESHOLD = 500  # Show range hint for files exceeding this line count


def _stale_threshold_seconds() -> int:
    """Derive the index staleness threshold from the auto-sync interval (issue #187).

    Auto sync enabled (sync_interval_seconds > 0): threshold scales with the
    interval so a dead loop is flagged promptly instead of hiding behind a
    fixed 24h window. Auto sync disabled: keep the original fixed 24h window,
    since there is no interval to derive a threshold from.
    """
    if settings.sync_interval_seconds > 0:
        return max(
            settings.sync_interval_seconds * _STALE_INTERVAL_MULTIPLIER,
            _STALE_SECONDS_FLOOR,
        )
    return _STALE_SECONDS


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
    threshold = _stale_threshold_seconds()
    if age is not None and age > threshold:
        hours = age // 3600
        warnings.append(
            f"{hours} hours since last sync (threshold {threshold}s). Index may be stale"
        )

    # Attempt tracking: consecutive failures mean the loop is running but
    # dying every time, which the freshness check above may not catch yet
    # (issue #187).
    consecutive_failures = info.get("consecutive_failures") or 0
    if consecutive_failures > 0:
        warnings.append(
            f"{consecutive_failures} consecutive sync failures. "
            f"last_error: {info.get('last_error')}"
        )

    # Token provider construction failure: build_token_provider() raised (e.g.
    # incomplete GitHub App config) instead of returning a provider (issue #193).
    # status() must never fail just because the auth config is broken -- that's
    # exactly the situation an operator needs status() to diagnose.
    token_provider_error = info.get("token_provider_error")
    if token_provider_error:
        warnings.append(
            f"token_provider could not be determined: {token_provider_error}"
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
            "Run python -m shiori ingest for diff sync"
        )

    return warnings


# Type precedence for cross-reference merging (closes > duplicate > refs > mention)
_TYPE_PRECEDENCE = {"closes": 0, "duplicate": 1, "refs": 2, "mention": 3}

# Patterns for issue reference classification (issue #97)
_CLOSES_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:#([0-9]+)|https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/([0-9]+))",
    re.IGNORECASE,
)
_DUPLICATE_RE = re.compile(
    r"\bduplicate\s+(?:of\s+)?(?:#([0-9]+)|https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/([0-9]+))",
    re.IGNORECASE,
)
_REFS_RE = re.compile(
    r"\b(?:refs?|see|related(?:\s+to)?)\s+(?:#([0-9]+)|https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/([0-9]+))",
    re.IGNORECASE,
)
_MENTION_RE = re.compile(
    r"(?<!\w)#([0-9]+)"
)


def _extract_refs(text: str | None) -> list[dict]:
    """Extract classified cross-references from body text (issue #97)."""
    if not text:
        return []
    seen: dict[int, str] = {}
    for m in _CLOSES_RE.finditer(text):
        n = int(m.group(1) or m.group(2))
        if n not in seen:
            seen[n] = "closes"
    for m in _DUPLICATE_RE.finditer(text):
        n = int(m.group(1) or m.group(2))
        if n not in seen:
            seen[n] = "duplicate"
    for m in _REFS_RE.finditer(text):
        n = int(m.group(1) or m.group(2))
        if n not in seen:
            seen[n] = "refs"
    for m in _MENTION_RE.finditer(text):
        n = int(m.group(1))
        if n not in seen:
            seen[n] = "mention"
    return [{"issue_no": no, "type": typ} for no, typ in seen.items()]


@mcp.tool(name="shiori_issue_links")
def issue_links(number: int, repo: str | None = None) -> dict[str, Any]:
    """issue/PR の相互参照（inbound/outbound）を返す（issue #97）。

    本文・コメント中の #N 参照を抽出し、種別（closes/duplicate/refs/mention）を
    判定する。参照先のタイトル・state も合わせて返す。inbound はこの issue を
    参照している他の issue/PR の一覧。

    重複チェック、epic 構築、回帰追跡に使う。

    repo: "owner/name"形式。一意に定まる短縮名（"owner/"なし）も可
          （例: "shiori" -> "owner/shiori"）。省略時は既定の設定済みリポジトリ。
    """
    target = _resolve_repo(repo)
    with _conn() as conn:
        bodies = db.get_issue_bodies(conn, target, number)
    if not bodies:
        raise ValueError(f"#{number} は索引されていません（ingest 済みですか？）")

    # Extract outbound refs from all bodies
    outbound_refs: dict[int, dict] = {}
    for b in bodies:
        for ref in _extract_refs(b["body"]):
            n = ref["issue_no"]
            if n == number:
                continue
            if n not in outbound_refs or _TYPE_PRECEDENCE.get(ref["type"], 99) < _TYPE_PRECEDENCE.get(outbound_refs[n]["type"], 99):
                outbound_refs[n] = ref

    # Look up referenced issue details
    outbound_nos = list(outbound_refs)
    with _conn() as conn:
        outbound_details = db.get_issues_by_numbers(conn, target, outbound_nos)
        inbound = db.find_inbound_refs(conn, target, number)

    outbound = []
    for n, ref in outbound_refs.items():
        detail = outbound_details.get(n, {})
        outbound.append({
            "issue_no": n,
            "type": ref["type"],
            "title": detail.get("title"),
            "state": detail.get("state"),
            "kind": detail.get("kind"),
            "url": detail.get("url"),
        })

    return {
        "repo": target,
        "number": number,
        "outbound": outbound,
        "inbound": inbound,
    }


@mcp.tool(name="shiori_status")
def status() -> dict[str, Any]:
    """Index freshness and health. Per-repo: last_synced_at, age_seconds, route, counts,
    items, cursor, warnings, last_attempt_at, last_error, consecutive_failures.
    Also reports auto_sync_running (actual thread liveness, not just config), and
    token_provider: the auth provider actually selected by build_token_provider()
    ("app" | "static" | "token_command" | "anonymous" | "error"), not just what
    the config *intends*. "anonymous" here always means "nothing was configured"
    -- no provider degrades into it silently any more (the mcp_token provider
    that did was removed; issue #188). A configured provider that cannot produce
    a token raises, and surfaces per-repo as last_error instead. If
    build_token_provider() itself raises (e.g. incomplete GitHub App config),
    this reports "error" with the exception message in a matching warning instead
    of letting the whole tool call fail (issue #193) -- this tool is exactly what
    an operator reaches for while diagnosing an auth config problem, so it must
    never fail for that same reason. Also reports auto_sync_last_error: the error
    message from the most recent failed auto-sync loop iteration (None once the
    loop last succeeded), which stays populated even for failures too early for
    _do_sync() to reach a live DB connection to record through
    record_sync_attempt (issue #196)."""
    # Cheap: just selects a TokenProvider class based on Settings, no I/O of its
    # own -- status() never calls get_token(), so polling it never triggers a
    # mint attempt.
    try:
        provider = build_token_provider(settings)
        token_provider = provider.name
        token_provider_error = None
    except Exception as exc:
        # build_token_provider() intentionally raises ValueError when the
        # GitHub App env vars are only partially configured (github_auth.py) --
        # useful for the sync path (fail fast), but status() is the tool an
        # operator reaches for *while* diagnosing an auth problem, so it must
        # never fail itself just because the auth config is broken (issue #193,
        # a regression from #188/#192 which made status() call
        # build_token_provider() unconditionally with no error handling).
        token_provider = "error"
        token_provider_error = str(exc)

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
                "last_attempt_at": None,
                "last_error": None,
                "consecutive_failures": 0,
            }
            chunk_counts = db.get_chunk_counts(conn, repo)
            items_in_db = db.get_issue_item_count(conn, repo)
            cursors = db.get_cursors(conn, repo)
            info["chunks"] = chunk_counts
            info["code_chunks"] = chunk_counts.get("code", 0)
            info["items_in_db"] = items_in_db
            info["cursors"] = cursors
            # Only used to let _build_warnings render the error warning; not part
            # of this function's per-repo return shape (popped below).
            info["token_provider_error"] = token_provider_error
            warnings = _build_warnings(info, chunk_counts, items_in_db, cursors)
            info.pop("token_provider_error", None)
            info["warnings"] = warnings
            repos[repo] = info
    return {
        "repos": repos,
        "sync_interval_seconds": settings.sync_interval_seconds,
        # Actual thread liveness (issue #187), not the config value: a server
        # that never started the loop (or whose loop died some other way that
        # doesn't raise inside _auto_sync_loop's own try/except) previously
        # reported sync_interval_seconds unconditionally, implying sync was
        # running when it might not have been.
        "auto_sync_running": _auto_sync_thread is not None and _auto_sync_thread.is_alive(),
        # Last-resort signal for auto-sync failures that happen so early
        # _do_sync() never reaches a live DB connection to record through
        # (issue #196) -- e.g. the database itself being unreachable. None
        # once the most recent iteration has succeeded.
        "auto_sync_last_error": _auto_sync_last_error,
        # Provider actually selected by build_token_provider() (issue #188).
        "token_provider": token_provider,
    }


def run(transport: str = "streamable-http") -> None:
    global _auto_sync_thread
    with _conn() as conn:
        db.migrate(conn, settings)
    if settings.sync_interval_seconds > 0:
        _auto_sync_thread = threading.Thread(
            target=_auto_sync_loop,
            args=(settings.sync_interval_seconds,),
            daemon=True,
        )
        _auto_sync_thread.start()
        log.info("auto sync enabled: every %ds", settings.sync_interval_seconds)
    log.info("shiori MCP server starting (%s)", transport)
    mcp.run(transport=transport)
