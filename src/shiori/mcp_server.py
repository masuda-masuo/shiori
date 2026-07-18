"""MCP server implementation.
~1100 lines: setup (1-90), helpers (90-310), tools (310-1100). Each tool typically <100 lines.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

import httpx

from . import db, schema
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
)
from .pipeline import (
    _conn,
    _do_sync,
    _ensure_phase1,
    _get_embedder,  # noqa: F401 — re-export for dashboard
    settings,
)
from .links import merge_outbound_refs
from .tools.registry import mcp
from .tools.common import (
    _get_token_provider,  # noqa: F401 — re-export for tests
    _github_client,
    _infer_repo_from_cwd,  # noqa: F401 — re-export for tests
    _validate_repo_name,  # noqa: F401 — re-export for tests
    _resolve_repo,
    _resolve_repo_filter,  # noqa: F401 — re-export for dashboard
    _resolve_repos,  # noqa: F401 — re-export for tests
    _make_filters,  # noqa: F401 — re-export for tests
)

log = logging.getLogger(__name__)























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

# Re-import moved tools so their tool decorators register
from .tools import search as _t_search, list_tree as _t_list_tree, read as _t_read, pr as _t_pr, grep as _t_grep  # noqa: F401, E402
# Re-export for backward-compatible imports
from .tools.search import semantic_search, keyword_search  # noqa: F401, E402
from .tools.list_tree import list_tree                      # noqa: F401, E402
from .tools.read import read_file, read_issue, read_pr_file, _read_issue_single  # noqa: F401, E402
from .tools.pr import pr_changes, pr_diff, pr_review_comments, _compute_pr_diff  # noqa: F401, E402
from .tools.grep import grep_search                          # noqa: F401, E402

register_dashboard(mcp)

def run(transport: Literal["stdio", "sse", "streamable-http"] = "streamable-http") -> None:
    with _conn() as conn:
        schema.migrate(conn, settings)
    log.info("shiori MCP server starting (%s), pull-type sync (#236)", transport)
    mcp.run(transport=transport)