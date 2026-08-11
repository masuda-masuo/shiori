from __future__ import annotations

import json
from typing import Any

from .. import db
from ..github_auth import build_token_provider
from ..pipeline import _conn, settings
from .common import _validate_repo_name
from .registry import mcp

# Floor on the derived staleness threshold (seconds): a misconfigured
# near-zero timer interval must not make every repo look permanently stale.
_STALE_SECONDS_FLOOR = 300

# Hard cap (chars) on the serialized `repos` payload of the no-repo call
# (issue #423).  A full per-repo record is ~870 chars on the live 62-repo
# fleet, so this admits roughly the top nine records before the remainder
# is reported by name in `summary.omitted_repo_names`.  The cap exists so
# the response stays inside an MCP client context window even when a
# fleet-wide condition -- token-provider outage (#193), sync-host failure
# aging every repo past its threshold -- makes every repo unhealthy at
# once: without it the no-repo response re-inflates to ~54k chars exactly
# when the operator is most likely to call it.
_NO_REPO_REPOS_CHAR_BUDGET = 8_000


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


def _is_healthy(info: dict) -> bool:
    """Whether an assembled per-repo record needs no operator attention.

    Unhealthy means any of: a warning was raised, the index is behind the
    on-disk clone (or absent entirely), syncs are failing, or an index run
    left issue items pending.  The no-repo call reports full records for
    unhealthy repos only (issue #423).
    """
    return not (
        info.get("warnings")
        or info.get("index_stale")
        or info.get("never_indexed")
        or (info.get("consecutive_failures") or 0) > 0
        or info.get("pending_count")
    )


def _summarize(all_repos: dict[str, Any]) -> dict[str, Any]:
    """Aggregate every per-repo record into the no-repo call's `summary`.

    Every count is derived from the same records the caller filters with
    _is_healthy, so the summary and the returned `repos` never disagree.
    """
    values = list(all_repos.values())
    unhealthy = sum(1 for info in values if not _is_healthy(info))

    oldest_name: str | None = None
    oldest_age = -1
    for name, info in all_repos.items():
        age = info.get("age_seconds")
        if age is not None and age > oldest_age:
            oldest_name, oldest_age = name, age

    return {
        "total_repos": len(values),
        "healthy_repos": len(values) - unhealthy,
        "unhealthy_repos": unhealthy,
        "unhealthy_counts": {
            "with_warnings": sum(1 for i in values if i.get("warnings")),
            "index_stale": sum(1 for i in values if i.get("index_stale")),
            "never_indexed": sum(1 for i in values if i.get("never_indexed")),
            "failing": sum(
                1 for i in values if (i.get("consecutive_failures") or 0) > 0
            ),
            "with_pending": sum(1 for i in values if i.get("pending_count")),
        },
        "pending_total": sum((i.get("pending_count") or 0) for i in values),
        "oldest_repo": (
            None
            if oldest_name is None
            else {
                "repo": oldest_name,
                "role": all_repos[oldest_name].get("role"),
                "age_seconds": oldest_age,
            }
        ),
        "note": (
            "repos lists full records for the most severe unhealthy repos "
            "up to a size budget; the rest are named in omitted_repo_names. "
            "Call shiori_status(repo=...) for any repo's full record"
        ),
    }


def _severity_key(name: str, info: dict) -> tuple[Any, ...]:
    """Order unhealthy repos for budgeted emission, most actionable first.

    Failing syncs outrank a never-indexed clone, which outranks a stale
    index, which outranks an unfinished pending drain, which outranks
    plain warnings; within a class the repo that synced longest ago comes
    first, with the name as the final tiebreaker for determinism.
    """
    return (
        0 if (info.get("consecutive_failures") or 0) > 0 else 1,
        0 if info.get("never_indexed") else 1,
        0 if info.get("index_stale") else 1,
        0 if info.get("pending_count") else 1,
        -(info.get("age_seconds") or 0),
        name,
    )


def _no_repo_response(
    all_repos: dict[str, Any],
    *,
    sync_intervals: dict[str, int],
    clone_refresh_debounce_seconds: int,
    token_provider: str,
) -> dict[str, Any]:
    """Build the complete no-repo response from assembled per-repo records.

    Pure: finished records and the top-level scalars in, the full payload
    out -- no DB, no connection, no mocks, which is what makes the
    degraded-fleet cases directly testable.  Health (_is_healthy) decides
    which repos are eligible for a full record; a hard char budget
    (_NO_REPO_REPOS_CHAR_BUDGET) decides how many of them are actually
    emitted, in severity order (_severity_key).  Unhealthy repos cut off
    by the budget are not dropped silently: `summary` reports the count
    (omitted_repos) and the names (omitted_repo_names) of everything not
    emitted (issue #423).
    """
    unhealthy = [
        (name, info) for name, info in all_repos.items() if not _is_healthy(info)
    ]
    unhealthy.sort(key=lambda pair: _severity_key(pair[0], pair[1]))

    selected: dict[str, Any] = {}
    omitted: list[str] = []
    for name, info in unhealthy:
        candidate = {**selected, name: info}
        if (
            len(json.dumps(candidate, ensure_ascii=False))
            <= _NO_REPO_REPOS_CHAR_BUDGET
        ):
            selected = candidate
        else:
            omitted.append(name)

    summary = _summarize(all_repos)
    summary["omitted_repos"] = len(omitted)
    summary["omitted_repo_names"] = omitted
    return {
        "repos": selected,
        "clone_refresh_debounce_seconds": clone_refresh_debounce_seconds,
        "sync_intervals": sync_intervals,
        "token_provider": token_provider,
        "summary": summary,
    }


@mcp.tool(name="shiori_status")
def status(repo: str | None = None) -> dict[str, Any]:
    """Report index status.  Omitting repo returns an aggregate summary (see
    below); passing repo="owner/name" returns that repo's full record.

    Omitting repo returns an AGGREGATE view, not every record: `summary`
    carries the fleet-wide numbers (total / healthy / unhealthy repo counts,
    a breakdown per unhealthy condition, the total pending item count and the
    oldest repo by age), and `repos` carries full per-repo records ONLY for
    repos that are not healthy -- warnings raised, index stale, never
    indexed, syncs failing, or items still pending.  Healthy repos are
    counted in `summary` and omitted from `repos`; pass repo="owner/name" to
    get one repo's full record either way (issue #423).

    The no-repo response is size-bounded even when the fleet degrades:
    unhealthy records are emitted in severity order (failing syncs first)
    until the serialized `repos` payload would exceed the
    _NO_REPO_REPOS_CHAR_BUDGET char budget, and every unhealthy repo cut
    off by that budget is named in `summary.omitted_repo_names` (count in
    `summary.omitted_repos`) rather than dropped silently -- so the
    response stays inside an MCP client context window even when a
    fleet-wide condition (e.g. a token-provider outage) makes every repo
    unhealthy at once.

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

    if not repo:
        # Issue #423: the all-repos response did not fit an MCP client
        # context window (53,836 chars at 62 repos) and 91% of real calls
        # omit repo.  The no-repo payload is a separate view built under a
        # hard char budget (_NO_REPO_REPOS_CHAR_BUDGET): healthy repos are
        # counted in `summary` only, and even unhealthy repos are emitted
        # only until the budget is spent, with the cut-off remainder named
        # in `summary` so nothing disappears silently.
        return _no_repo_response(
            repos,
            sync_intervals={
                "dev": settings.dev_sync_interval_seconds,
                "ref": settings.ref_sync_interval_seconds,
            },
            clone_refresh_debounce_seconds=settings.sync_interval_seconds,
            token_provider=token_provider,
        )
    # Repo-specified path: byte-identical to the pre-#423 object -- that
    # repo's full record, no summary key, same top-level key order.
    return {
        "repos": repos,
        "clone_refresh_debounce_seconds": settings.sync_interval_seconds,
        "sync_intervals": {
            "dev": settings.dev_sync_interval_seconds,
            "ref": settings.ref_sync_interval_seconds,
        },
        "token_provider": token_provider,
    }
