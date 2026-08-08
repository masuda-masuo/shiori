from __future__ import annotations

from typing import Any

from .. import db
from ..github_auth import build_token_provider
from ..pipeline import _conn, settings
from .common import _validate_repo_name
from .registry import mcp

# Floor on the derived staleness threshold (seconds): a misconfigured
# near-zero timer interval must not make every repo look permanently stale.
_STALE_SECONDS_FLOOR = 300


def _expected_sync_interval_seconds(repo: str) -> int:
    """Return the EXPECTED host-timer cadence (seconds) for *repo*'s role.

    Dev repos (in settings.dev_repos) are covered by the ~15-minute timer
    (SHIORI_DEV_SYNC_INTERVAL_SECONDS); everything else is a ref repo,
    covered by the daily timer (SHIORI_REF_SYNC_INTERVAL_SECONDS). These
    values document the cadence of the host-level systemd timers (issue
    #347) -- no in-server component observes or enforces them.
    """
    if repo in settings.dev_repos:
        return settings.dev_sync_interval_seconds
    return settings.ref_sync_interval_seconds


def _stale_threshold_seconds(repo: str) -> int:
    """Role-aware staleness threshold for *repo*: 2x its expected timer
    cadence, floored at _STALE_SECONDS_FLOOR (issue #347).

    Supersedes the old single sync_interval_seconds-derived formula (issue
    #187): sync_interval_seconds configures the Phase-1 clone refresh
    debounce, not any index sync cadence, so deriving a staleness threshold
    from it reported a fictional cadence.
    """
    return max(_expected_sync_interval_seconds(repo) * 2, _STALE_SECONDS_FLOOR)


def _build_warnings(
    info: dict,
    chunk_counts: dict[str, int],
    items_in_db: int,
    cursors: dict[str, str | None],
    stale_threshold_seconds: int,
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
    if age is not None and age > stale_threshold_seconds:
        hours = age // 3600
        warnings.append(
            f"{hours} hours since last sync (threshold {stale_threshold_seconds}s). "
            "Index may be stale"
        )

    consecutive_failures = info.get("consecutive_failures") or 0
    if consecutive_failures > 0:
        warnings.append(
            f"{consecutive_failures} consecutive sync failures. "
            f"last_error: {info.get('last_error')}"
        )

    # Issue #377: a repo whose last processing event found work still
    # pending was cut short by the index-run time budget (or has never
    # fully indexed) -- there is NO finished_at for it.  Surfacing the
    # remaining-work counter makes the drain observable from status
    # without ad hoc SQL.
    pending = info.get("pending_count")
    if pending is not None and pending > 0:
        warnings.append(
            f"{pending} issue items still pending (last index run did not "
            "complete this repo; finished_at absent)"
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
    """Report index status for one or all configured repositories.
    Unlike other tools, omitting repo reports ALL configured repos.

    Data sources: DB metadata only (sync_run, index_state, chunk counts); no
    clone read, no GitHub API call.
    """
    try:
        provider = build_token_provider(settings)
        token_provider = provider.name
        token_provider_error = None
    except Exception as exc:  # noqa: BLE001 - status must never raise; reports "error" (issue #193)
        token_provider = "error"  # noqa: S105 - literal is "error", not a secret; name mirrors the status dict key
        token_provider_error = str(exc)

    with _conn() as conn:
        repos: dict[str, Any] = {}

        if repo:
            resolved = _validate_repo_name(repo)
            targets = [resolved]
            run_info = db.get_sync_run(conn, resolved)
            runs = {resolved: run_info} if run_info else {}
            index_state_row = db.get_repo_index_state(conn, resolved)
            index_state = {resolved: index_state_row} if index_state_row else {}
        else:
            targets = settings.repos
            runs = db.get_sync_runs(conn)
            index_state = db.get_all_repo_index_state(conn)

        for target_repo in targets:
            info = runs.get(target_repo) or {
                "last_synced_at": None,
                "age_seconds": None,
                "route": None,
                "docs_updated": None,
                "issues_indexed": None,
                "code_added": None,
                "last_attempt_at": None,
                "last_error": None,
                "consecutive_failures": 0,
                "pending_count": None,
                "last_progress_at": None,
            }
            state = index_state.get(target_repo, {})
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
            chunk_counts = db.get_chunk_counts(conn, target_repo)
            items_in_db = db.get_issue_item_count(conn, target_repo)
            cursors = db.get_cursors(conn, target_repo)
            info["chunks"] = chunk_counts
            info["code_chunks"] = chunk_counts.get("code", 0)
            info["items_in_db"] = items_in_db
            info["cursors"] = cursors
            info["role"] = "dev" if target_repo in settings.dev_repos else "ref"
            info["code_indexed"] = chunk_counts.get("code", 0) > 0
            info["token_provider_error"] = token_provider_error
            info["expected_sync_interval_seconds"] = _expected_sync_interval_seconds(
                target_repo
            )
            threshold = _stale_threshold_seconds(target_repo)
            warnings = _build_warnings(info, chunk_counts, items_in_db, cursors, threshold)
            info.pop("token_provider_error", None)
            info["warnings"] = warnings
            repos[target_repo] = info
    return {
        "repos": repos,
        "clone_refresh_debounce_seconds": settings.sync_interval_seconds,
        "sync_intervals": {
            "dev": settings.dev_sync_interval_seconds,
            "ref": settings.ref_sync_interval_seconds,
        },
        "token_provider": token_provider,
    }
