from __future__ import annotations

from typing import Any

from .registry import mcp
from .common import _validate_repo_name
from ..pipeline import _conn, settings
from .. import db
from ..github_auth import build_token_provider

_STALE_SECONDS = 86400
_STALE_INTERVAL_MULTIPLIER = 30
_STALE_SECONDS_FLOOR = 300


def _stale_threshold_seconds() -> int:
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
    warnings: list[str] = []

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

    age = info.get("age_seconds")
    threshold = _stale_threshold_seconds()
    if age is not None and age > threshold:
        hours = age // 3600
        warnings.append(
            f"{hours} hours since last sync (threshold {threshold}s). Index may be stale"
        )

    consecutive_failures = info.get("consecutive_failures") or 0
    if consecutive_failures > 0:
        warnings.append(
            f"{consecutive_failures} consecutive sync failures. "
            f"last_error: {info.get('last_error')}"
        )

    token_provider_error = info.get("token_provider_error")
    if token_provider_error:
        warnings.append(
            f"token_provider could not be determined: {token_provider_error}"
        )

    total_issue_chunks = chunk_counts.get("issue", 0) + chunk_counts.get("pr_review", 0)
    if items_in_db > 0 and total_issue_chunks < items_in_db // 2:
        warnings.append(
            f"issue_items has {items_in_db} rows but chunks[issue]+chunks[pr_review] has {total_issue_chunks}."
            "Bot exclusion (SHIORI_INDEX_BOT_LOGINS) or indexing gap possible"
        )

    all_kinds = {"docs", "issues", "issue_comments", "pr_review_comments"}
    missing = [k for k in all_kinds if k not in cursors]
    if missing:
        warnings.append(
            f"Unsynced categories: {', '.join(missing)}."
            "Run python -m shiori ingest for diff sync"
        )

    return warnings


@mcp.tool(name="shiori_status")
def status(repo: str | None = None) -> dict[str, Any]:
    """Report index status for one or all configured repositories (issue #350).

    repo: target repo ("owner/name"), a short name if it uniquely matches
          one configured (indexed) repo (e.g. "shiori" -> "owner/shiori"),
          or None for all repos (default).
    """
    try:
        provider = build_token_provider(settings)
        token_provider = provider.name
        token_provider_error = None
    except Exception as exc:
        token_provider = "error"
        token_provider_error = str(exc)

    with _conn() as conn:
        repos: dict[str, Any] = {}

        if repo:
            resolved = _validate_repo_name(repo)
            targets = [resolved]
            runs = db.get_sync_runs(conn)
            index_state_row = db.get_repo_index_state(conn, resolved)
            index_state = {resolved: index_state_row} if index_state_row else {}
        else:
            targets = settings.repos
            runs = db.get_sync_runs(conn)
            index_state = db.get_all_repo_index_state(conn)

        for repo in targets:
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
            clone_head = state.get("clone_head")
            indexed_head = state.get("indexed_head")
            if clone_head and indexed_head:
                info["index_stale"] = clone_head != indexed_head
            elif clone_head and not indexed_head:
                info["index_stale"] = True
            else:
                info["index_stale"] = False
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
