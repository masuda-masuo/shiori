"""MCP server implementation.
~1100 lines: setup (1-90), helpers (90-310), tools (310-1100). Each tool typically <100 lines.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading
from typing import Any, Iterator, Literal

import httpx

from mcp.server.fastmcp import FastMCP

from . import db, search, schema
from .dashboard import register_dashboard
from .report import (
    _REPORT_TEMPLATES,
    _stats_data,
    _stats_to_markdown,
    _report_symbol_index,
    _report_module_tree,
    _report_api_reference,
)
from .github_auth import build_token_provider
from .github_sync import (
    API,
    _api_pages,
    _clean_text,
    _git,
    _git_delete_ref,
    _git_fetch_ref,
    _GitHubAuth,
    _is_bot,
)
from .pipeline import (
    _conn,
    _do_sync,
    _ensure_phase1,
    _get_embedder,
    settings,
    _trigger_phase2,
)
from .links import merge_outbound_refs
from .walk_utils import (
    _match_extension,
    _walk_code_files,
)

log = logging.getLogger(__name__)

# Cached token provider (built once, reused across _github_client calls)
_token_provider: Any | None = None
_token_provider_lock = threading.Lock()


def _get_token_provider():
    global _token_provider
    with _token_provider_lock:
        if _token_provider is None:
            _token_provider = build_token_provider(settings)
    return _token_provider


@contextlib.contextmanager
def _github_client() -> Iterator[httpx.Client]:
    provider = _get_token_provider()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(headers=headers, auth=_GitHubAuth(provider), timeout=30.0) as client:
        yield client


def _infer_repo_from_cwd() -> str | None:
    """Infer repo from git remote of current working directory."""
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["git", "remote", "get-url", "origin"],  # noqa: S607
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
        "■ Repo roles (shiori_status shows role per repo)\n"
        "Each repo is either dev or ref:\n"
        "- dev (SHIORI_DEV_REPOS): code IS indexed. shiori_search(source_type='code') finds code chunks.\n"
        "- ref (not in SHIORI_DEV_REPOS): code is clone-only. Use shiori_grep for code; shiori_search still finds issues/PRs/docs.\n"
        "shiori_list_tree(source_type='code') works for both (walks clone on disk).\n"
        "\n"
        "■ Two-store model (information sources)\n"
        "shiori has 2 independent data sources:\n"
        "1. Index (Postgres/pgvector/pgroonga)\n"
        "   - shiori_search / shiori_keyword_search: embedding + full-text search\n"
        "   - shiori_read_issue: issue/PR threads (indexed only)\n"
        "   - shiori_list_tree (source_type='doc'): indexed doc_files table\n"
        "   - shiori_pr_changes: PR change file maps (indexed metadata)\n"
        "   - Freshness depends on pull-type sync (#236)\n"
        "2. Clone (disk, pinned to main branch)\n"
        "   - shiori_read_file: read real files directly (no index needed, works if clone exists)\n"
        "   - shiori_read_pr_file: get PR head files via git (non-destructive to working tree)\n"
        "   - shiori_list_tree (source_type='code'): physically existing code files via os.walk\n"
        "   - Clone is refreshed on-demand (Phase 1: git fetch + reset --hard; #236)\n"
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
    # Phase 1/2 for cross-repo search: refresh all repos when no filter (#236)
    resolved_repo = _resolve_repo_filter(repo)
    if resolved_repo:
        _ensure_phase1(resolved_repo)
        _trigger_phase2(resolved_repo)
    else:
        for r in _resolve_repos("*"):
            _ensure_phase1(r)
            _trigger_phase2(r)
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
    # Phase 1/2 for cross-repo search: refresh all repos when no filter (#236)
    resolved_repo = _resolve_repo_filter(repo)
    if resolved_repo:
        _ensure_phase1(resolved_repo)
        _trigger_phase2(resolved_repo)
    else:
        for r in _resolve_repos("*"):
            _ensure_phase1(r)
            _trigger_phase2(r)
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
        _ensure_phase1(target)  # Phase 1: ensure clone is fresh (#236)
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
    _ensure_phase1(target)
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
    """Fetch single issue/PR via GitHub API.
    Raises ValueError if not found on GitHub.
    """
    with _github_client() as client:
        try:
            resp = client.get(f"{API}/repos/{target}/issues/{number}")
            resp.raise_for_status()
            issue = resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"#{number} not found on GitHub")
            raise

        is_pr = "pull_request" in issue
        kind = "pr" if is_pr else "issue"
        title = issue.get("title") or ""
        state = issue.get("state")
        url = issue.get("html_url")

        items: list[dict[str, Any]] = [{
            "author": (issue.get("user") or {}).get("login"),
            "is_bot": _is_bot(issue.get("user")),
            "kind": kind,
            "state": state,
            "created_at": issue.get("created_at"),
            "body": _clean_text(issue.get("body") or ""),
            "url": url,
        }]

        # Issue/PR comments
        comments = _api_pages(
            client,
            f"{API}/repos/{target}/issues/{number}/comments",
            {"per_page": 100},
        )
        for c in comments:
            items.append({
                "author": (c.get("user") or {}).get("login"),
                "is_bot": _is_bot(c.get("user")),
                "kind": "comment",
                "state": state,
                "created_at": c.get("created_at"),
                "body": _clean_text(c.get("body") or ""),
                "url": c.get("html_url"),
            })

        if is_pr:
            # PR review submissions
            try:
                reviews = _api_pages(
                    client,
                    f"{API}/repos/{target}/pulls/{number}/reviews",
                    {"per_page": 100},
                )
                for r in reviews:
                    rbody = _clean_text(r.get("body") or "")
                    if not rbody:
                        continue
                    items.append({
                        "author": (r.get("user") or {}).get("login"),
                        "is_bot": _is_bot(r.get("user")),
                        "kind": "pr_review",
                        "state": r.get("state"),
                        "created_at": r.get("submitted_at"),
                        "body": rbody,
                        "url": r.get("html_url"),
                    })
            except httpx.HTTPError:
                pass

            # PR inline review comments
            try:
                review_comments = _api_pages(
                    client,
                    f"{API}/repos/{target}/pulls/{number}/comments",
                    {"per_page": 100},
                )
                for rc in review_comments:
                    rc_line = rc.get("line") or rc.get("original_line")
                    diff_hunk = rc.get("diff_hunk")
                    rc_body = _clean_text(rc.get("body") or "")
                    if diff_hunk:
                        rc_body = f"{rc_body}\n\n```diff\n{_clean_text(diff_hunk)}\n```"
                    item: dict[str, Any] = {
                        "author": (rc.get("user") or {}).get("login"),
                        "is_bot": _is_bot(rc.get("user")),
                        "kind": "pr_review_comment",
                        "state": state,
                        "created_at": rc.get("created_at"),
                        "body": rc_body,
                        "url": rc.get("html_url"),
                    }
                    if rc.get("path"):
                        item["path"] = rc["path"]
                        item["line"] = rc_line
                    items.append(item)
            except httpx.HTTPError:
                pass

    # Sort: body first, then by created_at
    body_item = items[0]
    rest = sorted(items[1:], key=lambda x: x.get("created_at") or "")
    items = [body_item] + rest

    # Exclude bots outside the allowlist (issue #44)
    if exclude_noise_bots:
        allowlist = settings.index_bot_logins
        items = [
            item for item in items
            if not item["is_bot"] or (item["author"] and item["author"].lower() in allowlist)
        ]
        if not items:
            raise ValueError(f"#{number}: all items are bots outside the allowlist")

    return {
        "repo": target,
        "number": number,
        "kind": kind,
        "title": title,
        "state": state,
        "url": url,
        "items": items,
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
    Items have a kind field: 'issue', 'pr', 'comment', 'pr_review', or
    'pr_review_comment'.
    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo. An unresolvable repo
          raises immediately with the indexed-repo list."""
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
    """PR change file map computed from git clone (issue #259).
    Uses git diff --name-status + --numstat to build the file list.
    blob_url is omitted because it cannot be computed from git alone.
    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo."""
    target = _resolve_repo(repo)
    git_dir = os.path.realpath(settings.repo_dir(target))
    if not os.path.isdir(os.path.join(git_dir, ".git")):
        raise FileNotFoundError(
            f"Clone for {target} does not exist. Run python -m shiori ingest first."
        )

    provider = build_token_provider(settings)
    tmp_ref = None
    tmp_base = None
    try:
        tmp_ref = _git_fetch_ref(
            f"pull/{number}/head", cwd=git_dir, provider=provider,
        )
        head_sha = _git(["rev-parse", tmp_ref], cwd=git_dir)

        tmp_base = _git_fetch_ref(
            "HEAD", cwd=git_dir, provider=provider,
        )
        base_sha = _git(["rev-parse", tmp_base], cwd=git_dir)

        name_status_text = _git(
            ["diff", "--name-status", f"{tmp_base}..{tmp_ref}"], cwd=git_dir,
        )
        numstat_text = _git(
            ["diff", "--numstat", f"{tmp_base}..{tmp_ref}"], cwd=git_dir,
        )

        numstat_map: dict[str, tuple[int, int]] = {}
        for line in numstat_text.split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                try:
                    add = int(parts[0])
                    delete = int(parts[1])
                except ValueError:
                    add = 0
                    delete = 0
                numstat_map[parts[-1]] = (add, delete)

        files: list[dict[str, Any]] = []
        for line in name_status_text.split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            status = parts[0]
            path = parts[-1]
            add, delete = numstat_map.get(path, (0, 0))
            files.append({
                "path": path,
                "status": status,
                "additions": add,
                "deletions": delete,
                "changes": add + delete,
            })

        result: dict[str, Any] = {
            "repo": target,
            "number": number,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "files": files,
        }
        if include_diff:
            diff_text, stat_text = _compute_pr_diff(
                number, target, base_sha=None, path=None,
                tmp_ref=tmp_ref, tmp_base=tmp_base,
            )
            result["diff"] = diff_text
            if stat_text:
                result["stats"] = stat_text
        return result
    finally:
        with contextlib.suppress(RuntimeError):
            if tmp_ref:
                _git_delete_ref(tmp_ref, cwd=git_dir)
        with contextlib.suppress(RuntimeError):
            if tmp_base:
                _git_delete_ref(tmp_base, cwd=git_dir)


def _compute_pr_diff(
    number: int,
    target: str,
    base_sha: str | None,
    path: str | None = None,
    *,
    tmp_ref: str | None = None,
    tmp_base: str | None = None,
) -> tuple[str, str]:
    """Fetch PR head and compute unified diff + stat (issue #96, #259).

    When base_sha=None, diffs against remote HEAD (default branch).
    When tmp_ref / tmp_base are provided, skips fetch (avoids duplicate
    fetch when the caller has already fetched).
    Returns (diff_text, stat_text). Raises FileNotFoundError if clone
    is missing.
    """
    git_dir = os.path.realpath(settings.repo_dir(target))
    if not os.path.isdir(os.path.join(git_dir, ".git")):
        raise FileNotFoundError(
            f"Clone for {target} does not exist. Run python -m shiori ingest first."
        )

    ref = f"pull/{number}/head"
    owned_ref: str | None = None
    owned_base: str | None = None
    try:
        if tmp_ref:
            diff_head = tmp_ref
        else:
            provider = build_token_provider(settings)
            owned_ref = _git_fetch_ref(ref, cwd=git_dir, provider=provider)
            diff_head = owned_ref

        if tmp_base:
            diff_base = tmp_base
        elif base_sha:
            provider = build_token_provider(settings)
            owned_base = _git_fetch_ref(
                base_sha, cwd=git_dir, provider=provider,
            )
            diff_base = owned_base
        else:
            provider = build_token_provider(settings)
            owned_base = _git_fetch_ref(
                "HEAD", cwd=git_dir, provider=provider,
            )
            diff_base = owned_base

        args = ["diff", f"{diff_base}..{diff_head}", "--unified=3"]
        if path:
            args.extend(["--", path])
        diff_text = _git(args, cwd=git_dir)
        stat_text = _git(
            ["diff", f"{diff_base}..{diff_head}", "--stat"], cwd=git_dir
        )
        return diff_text, stat_text.strip() if stat_text else ""
    finally:
        with contextlib.suppress(RuntimeError):
            if owned_ref:
                _git_delete_ref(owned_ref, cwd=git_dir)
        with contextlib.suppress(RuntimeError):
            if owned_base:
                _git_delete_ref(owned_base, cwd=git_dir)


@mcp.tool(name="shiori_pr_diff")
def pr_diff(
    number: int,
    path: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    """Return PR diff (issue #96).

    Computes the unified diff between PR head and base using git diff.
    Returns per-file diffs. When path is given, returns diff for that file only.

    number: PR number
    path: File path to scope the diff (omit for all files)
    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo.
    """
    target = _resolve_repo(repo)
    diff_text, stat_text = _compute_pr_diff(number, target, base_sha=None, path=path)
    return {
        "repo": target,
        "number": number,
        "diff": diff_text,
        "stats": stat_text,
    }


@mcp.tool(name="shiori_pr_review_comments")
def pr_review_comments(number: int, repo: str | None = None) -> dict[str, Any]:
    """Return PR review comments (issue #96).

    Fetches from GitHub Pull Request Review Comments API directly.
    Includes file path, line number, body, author, and timestamps.
    Useful for reviewing comment history and understanding other reviewers' feedback.

    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo.
    """
    target = _resolve_repo(repo)
    with _github_client() as client:
        try:
            rows = _api_pages(
                client,
                f"{API}/repos/{target}/pulls/{number}/comments",
                {"per_page": 100},
            )
        except httpx.HTTPError as exc:
            raise ValueError(f"PR #{number} not found on GitHub: {exc}")

    comments = []
    for r in rows:
        line = r.get("line") or r.get("original_line")
        comments.append({
            "comment_id": r["id"],
            "author": (r.get("user") or {}).get("login"),
            "is_bot": _is_bot(r.get("user")),
            "path": r.get("path"),
            "line": line,
            "body": _clean_text(r.get("body") or ""),
            "url": r.get("html_url"),
            "created_at": r.get("created_at"),
        })

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

    # Phase 1: refresh clones inline before searching (#236)
    for target in targets:
        _ensure_phase1(target)

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

        cmd = ["rg", "-n", "--no-heading", "--color", "never", "--with-filename"]
        if ignore_case:
            cmd.append("-i")
        if not regex:
            cmd.append("--fixed-strings")
        cmd.extend(["-e", pattern])
        cmd.append(resolved)

        try:
            rg_result = subprocess.run(  # noqa: S603
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
                        line_num = int(parts[1])
                        total += 1
                        text = parts[2] if len(parts) > 2 else ""
                        if len(all_matches) < max_results:
                            rel_path = parts[0]
                            if rel_path.startswith(base + "/"):
                                rel_path = rel_path[len(base) + 1:]
                            all_matches.append({
                                "repo": target,
                                "path": rel_path,
                                "line": line_num,
                                "text": text,
                            })
                        continue
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




@mcp.tool(name="shiori_report")
def report(
    template: str,
    repo: str | None = None,
    path: str | None = None,
    kind: str | None = None,
    public_only: bool = False,
    max_results: int = 500,
    prog_lang: str | None = None,
    max_chars: int = 50000,
) -> dict[str, Any]:
    """Generate a structured report about a repository.

    template: report type ("stats" for language statistics via tokei,
              "symbol_index" for symbol index via universal-ctags,
              "module_tree" for Mermaid mindmap of repo structure,
              "api_reference" for API reference showing classes, functions and docstrings)
    repo: target repo ("owner/name"), or a short name if it uniquely
          matches one configured (indexed) repo (e.g. "shiori" ->
          "owner/shiori"); None for default
    path: optional subdirectory within the repo to scope the report
    kind: ctags kind filter (e.g. "function", "class"; symbol_index only)
    public_only: exclude private/protected symbols (symbol_index only)
    max_results: maximum nodes/symbols to return, default 500 (symbol_index/module_tree)
    prog_lang: programming language filter (e.g. "python"; api_reference only)
    max_chars: maximum output characters, default 50000 (api_reference only)
    """
    if template not in _REPORT_TEMPLATES:
        raise ValueError(
            f"Unknown template: '{template}'. "
            f"Valid templates: {', '.join(sorted(_REPORT_TEMPLATES))}"
        )

    target = _resolve_repo(repo)
    _ensure_phase1(target)  # Phase 1: ensure clone is fresh for report (#236)
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
        data = _stats_data(target_path)
        markdown = _stats_to_markdown(data)
        return {
            "repo": target,
            "template": template,
            "markdown": markdown,
            "data": data,
        }
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
            "data": result.get("data"),
        }

    elif template == "api_reference":
        result = _report_api_reference(
            base=base,
            target_repo=target,
            path_prefix=path,
            prog_lang=prog_lang,
            max_chars=max_chars,
            conn_factory=_conn,
        )
        return {
            "repo": target,
            "template": template,
            "markdown": result["markdown"],
            "truncated": result["truncated"],
        }

    raise AssertionError("Unreachable template code path")

def ingest(rebuild: bool = False, repo: str | None = None) -> dict[str, Any]:
    """Sync docs/issues/code from GitHub and update index (diff sync, typically seconds).
    Check index freshness with shiori_status first -- pull-type sync (#236) refreshes
    the clone on-demand and triggers Phase 2 (re-index) in the background when stale.
    rebuild=True: discard and full rebuild (requires SHIORI_ALLOW_REBUILD=true; issue #63).
    Also treated as rebuild when chunks table is empty."""
    if rebuild and not settings.allow_rebuild:
        raise ValueError(
            "rebuild=True cannot be executed from the MCP tool. "
            "Use the CLI (python -m shiori ingest --rebuild) or "
            "set the environment variable SHIORI_ALLOW_REBUILD=true."
        )
    return _do_sync(repos=[repo] if repo else None, rebuild=rebuild, route="mcp")


_STALE_SECONDS = 86400  # 24 hours; used when sync_interval_seconds <= 0 (debounce disabled)
_STALE_INTERVAL_MULTIPLIER = 30
_STALE_SECONDS_FLOOR = 300  # 5 minutes

_LARGE_FILE_THRESHOLD = 500


def _stale_threshold_seconds() -> int:
    """Derive the index staleness threshold from the debounce interval (#236).

    sync_interval_seconds > 0: threshold scales with the debounce interval (formerly
    auto-sync interval; now means "max N seconds between pulls").
    sync_interval_seconds <= 0: use the fixed 24h window.
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

    # Pull-type freshness: clone on disk is ahead of indexed content (#236)
    if info.get("never_indexed"):
        warnings.append(
            f"clone exists but has never been indexed "
            f"(clone_head={info.get('clone_head', '?')[:7]})"
        )
    elif info.get("index_stale"):
        warnings.append(
            f"index is stale: on-disk clone is ahead of indexed content "
            f"(clone_head={info.get('clone_head', '?')[:7]}, "
            f"indexed_head={info.get('indexed_head', '?')[:7]})"
        )

    # Freshness: long time since last sync
    age = info.get("age_seconds")
    threshold = _stale_threshold_seconds()
    if age is not None and age > threshold:
        hours = age // 3600
        warnings.append(
            f"{hours} hours since last sync (threshold {threshold}s). Index may be stale"
        )

    # Attempt tracking: consecutive failures mean sync has been failing
    # consistently, which the freshness check above may not catch yet
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

@mcp.tool(name="shiori_issue_links")
def issue_links(number: int, repo: str | None = None) -> dict[str, Any]:
    """Return issue/PR cross-references (inbound/outbound) (issue #97).

    Extracts #N references from body text and comments, classifying
    them as closes/duplicate/refs/mention. Includes target title and
    state. Inbound lists other issues/PRs that reference this issue.

    Useful for duplicate detection, epic construction, and regression tracking.

    repo: "owner/name", or a short name if it uniquely matches one
          configured (indexed) repo (e.g. "shiori" -> "owner/shiori").
          Omit for the default configured repo.
    """
    target = _resolve_repo(repo)

    # Fetch issue body + comments from GitHub API
    with _github_client() as client:
        try:
            resp = client.get(f"{API}/repos/{target}/issues/{number}")
            resp.raise_for_status()
            issue = resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"#{number} not found on GitHub")
            raise

        bodies = [{"body": issue.get("body") or ""}]
        try:
            comments = _api_pages(
                client,
                f"{API}/repos/{target}/issues/{number}/comments",
                {"per_page": 100},
            )
            for c in comments:
                if c.get("body"):
                    bodies.append({"body": c["body"]})
        except httpx.HTTPError:
            pass

    # Extract outbound refs from all bodies
    outbound_refs = merge_outbound_refs(bodies, number)

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
    """Index freshness and health. Per-repo: clone_head, indexed_head, index_stale,
    last_synced_at, age_seconds, route, counts, items, cursor, warnings,
    last_attempt_at, last_error, consecutive_failures, last_sync_error, role,
    code_indexed.
    role='dev' means code is indexed (shiori_search finds code);
    role='ref' means clone-only (use shiori_grep for code).
    code_indexed is the dynamic state (whether code chunks exist in DB).
    Also reports token_provider: the auth provider actually selected by
    build_token_provider() ("app" | "static" | "token_command" | "anonymous"
    | "error"), not just what the config *intends*.
    Pull-type sync (#236): no more auto-sync loop. Freshness is measured by
    clone_head vs indexed_head comparison. index_stale=True means the on-disk
    clone (Phase 1) is ahead of the indexed content (Phase 2)."""
    # Cheap: just selects a TokenProvider class based on Settings, no I/O of its
    # own -- status() never calls get_token(), so polling it never triggers a
    # mint attempt.
    try:
        provider = build_token_provider(settings)
        token_provider = provider.name
        token_provider_error = None
    except Exception as exc:
        token_provider = "error"
        token_provider_error = str(exc)

    with _conn() as conn:
        runs = db.get_sync_runs(conn)
        index_state = db.get_all_repo_index_state(conn)
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
            state = index_state.get(repo, {})
            info["clone_head"] = state.get("clone_head")
            info["indexed_head"] = state.get("indexed_head")
            info["last_sync_error"] = state.get("last_sync_error")
            # Stale detection (#236): clone exists but index doesn't match.
            # - never_indexed: clone exists, no indexed_head (never completed Phase 2)
            # - behind: both exist but differ (clone moved ahead since last Phase 2)
            # - fresh: both exist and match
            clone_head = state.get("clone_head")
            indexed_head = state.get("indexed_head")
            if clone_head and indexed_head:
                info["index_stale"] = clone_head != indexed_head
            elif clone_head and not indexed_head:
                info["index_stale"] = True  # never indexed
            else:
                info["index_stale"] = False  # no clone yet
            info["never_indexed"] = bool(clone_head and not indexed_head)
            chunk_counts = db.get_chunk_counts(conn, repo)
            items_in_db = db.get_issue_item_count(conn, repo)
            cursors = db.get_cursors(conn, repo)
            info["chunks"] = chunk_counts
            info["code_chunks"] = chunk_counts.get("code", 0)
            info["items_in_db"] = items_in_db
            info["cursors"] = cursors
            info["role"] = "dev" if repo in settings.dev_repos else "ref"
            info["code_indexed"] = chunk_counts.get("code", 0) > 0
            info["token_provider_error"] = token_provider_error
            warnings = _build_warnings(info, chunk_counts, items_in_db, cursors)
            info.pop("token_provider_error", None)
            info["warnings"] = warnings
            repos[repo] = info
    return {
        "repos": repos,
        "sync_interval_seconds": settings.sync_interval_seconds,
        "token_provider": token_provider,
    }

register_dashboard(mcp)

def run(transport: Literal["stdio", "sse", "streamable-http"] = "streamable-http") -> None:
    with _conn() as conn:
        schema.migrate(conn, settings)
    log.info("shiori MCP server starting (%s), pull-type sync (#236)", transport)
    mcp.run(transport=transport)