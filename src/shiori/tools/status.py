from __future__ import annotations

import json
from typing import Any

import pydantic_core
from mcp_types import CallToolResult, JSONRPCResponse, TextContent

from .. import db
from ..github_auth import build_token_provider
from ..pipeline import _conn, settings
from .common import _validate_repo_name
from .registry import mcp

# Floor on the derived staleness threshold (seconds): a misconfigured
# near-zero timer interval must not make every repo look permanently stale.
_STALE_SECONDS_FLOOR = 300

# Nominal ceiling (chars) on the no-repo call's FastMCP-wrapped tool
# result (issue #423, measured after convert_result wrapping since
# #431).  The budget is compared against _wrapped_result_dumps -- the
# response the MCP client actually receives (pretty-printed text content
# plus a structured content copy, inside the streamable-HTTP JSON-RPC
# envelope) -- not against the inner `repos` dict, whose ~7.7k chars
# reached the client as a ~20k-char response (the old measurement named
# one unit and spent another).  On the CJK degraded-fleet fixture one
# full unhealthy record wraps to ~3.3k chars and the loop's first
# candidate (which also names the other 61 unhealthy repos as omitted)
# to ~4.9k, so 8,000 admits the top two-to-five records before the
# remainder is named in `summary.omitted_repo_names`.
#
# `last_error` is DUPLICATED at the status surface: the record's own
# `last_error` field AND the `consecutive_failures > 0` warning copy
# both quote it (issue #433).  It is an unbounded production string
# (record_sync_attempt truncates it with a char slice, src/shiori/db.py,
# so a 2000-char CJK error survives into the record field), and with the
# warning copy the duplication used to re-inflate one record with a
# 2000-char CJK last_error to ~50k chars -- the #433 finding, the same
# size class as the #423 incident (53,836) -- re-inflating exactly when
# the operator most needs the tool.  The status surface therefore
# truncates `last_error` to _STATUS_LAST_ERROR_CHAR_CAP (cap=250) and
# applies the SAME truncated form to BOTH copies, so the duplication
# cost is bounded: the canonical degraded-fleet floor -- the payload a
# fully degraded fleet actually emits, i.e. ONE worst-case record
# (2000-char CJK last_error, consecutive_failures > 0) PLUS the other 61
# unhealthy repos named in `summary.omitted_repo_names` -- wraps to
# ~11,009 chars (measured with _wrapped_result_dumps over the 62-repo
# CJK oversized fixture through the real status() path), under the
# 12,000 acceptance bound (#433 criterion 2).  The degenerate single-repo
# payload (one record, ZERO omitted names) is smaller (~9,400 chars) and
# is NOT what criterion 2 binds on; the prior round's "~11,888" figure
# was measured on exactly that degenerate substrate and was falsely
# attributed to criterion 2.  The DB keeps the full (2000-capped) error;
# only the status surface truncates.  Errors at or under the cap pass
# through byte-identical.
#
# The budget loop still applies a per-call effective ceiling
# (_no_repo_budget): this constant, raised to the measured wrapped size
# of the payload containing only the most severe record when that alone
# cannot fit -- so at least one full unhealthy record is always emitted
# (issue #431, criterion 7).  The ceiling does NOT guarantee the
# response fits an MCP client context window when a record's error is
# large: by design (issue #433) truncation bounds the per-record cost
# rather than guaranteeing a window fit, and the floor guarantees the
# most severe record is emitted even when its wrapped form alone
# exceeds the nominal budget.  Without the floor a fit-or-skip loop
# would emit ZERO full records on a fully degraded fleet -- exactly when
# the operator needs them most.
_NO_REPO_REPOS_CHAR_BUDGET = 8_000

# Status-surface cap (chars) on a record's `last_error` when it enters
# the shiori_status payload (issue #433).  `last_error` is quoted twice
# at the status surface -- the record's `last_error` field AND the
# `consecutive_failures > 0` warning copy -- so the cap is applied to
# the SAME value used for both copies (see _truncate_last_error_for_status)
# to bound the duplication.  Chosen by measurement against the canonical
# degraded-fleet floor -- NOT the degenerate single-repo payload -- using
# the existing 62-repo CJK oversized fixture through the real status()
# path (issue #433).  The floor is the payload a fully degraded fleet
# emits: one worst-case record (2000-char CJK last_error,
# consecutive_failures > 0) PLUS the other 61 unhealthy repos named in
# `summary.omitted_repo_names`.  250 is the largest round cap in the
# 200-400 band that keeps that floored wrapped payload <= 12,000 chars:
# cap=250 -> ~11,009 chars; cap=280 -> ~11,729 (only ~2% margin, so 250
# is chosen for ~8% margin against fleet-shape variance such as longer
# repo names in omitted_repo_names).  cap=300 already exceeds it
# (~12,209); 400 ~14,609.  The DB keeps the full (2000-capped) error;
# only the status surface truncates (criterion 1).
_STATUS_LAST_ERROR_CHAR_CAP = 250


def _truncate_last_error_for_status(value: str | None) -> str | None:
    """Truncate `last_error` for the status payload, identically for both copies.

    Returns *value* unchanged when it is None or already at or under
    _STATUS_LAST_ERROR_CHAR_CAP; otherwise the leading cap chars followed
    by a marker that names the stored length, e.g.
    "... (truncated, 2000 chars stored)".  "stored", not "total": the DB
    write path itself slices at 2000 chars (src/shiori/db.py), so the true
    production-original length is unknowable here -- the marker names the
    stored length it truncated from.  The same return value is used
    for the record's `last_error` field and the consecutive_failures
    warning copy so the two stay consistent (issue #433, criterion 1).
    The DB write path stores the full (2000-capped) error and is
    untouched by this truncation.
    """
    if value is None or len(value) <= _STATUS_LAST_ERROR_CHAR_CAP:
        return value
    return (
        value[:_STATUS_LAST_ERROR_CHAR_CAP]
        + f"\u2026 (truncated, {len(value)} chars stored)"
    )


def _wire_dumps(value: Any) -> str:
    """Serialize *value* with the streamable-HTTP transport's body dumps.

    The installed transport writes its JSON-RPC body with
    ``json.dumps(body, separators=(",", ":"))``
    (mcp/server/_streamable_http_modern.py) -- compact separators and
    ``ensure_ascii`` left at its ``True`` default, so every non-ASCII char
    serializes as a 6-char ``\\uXXXX`` escape (issue #425).  This is the
    serializer applied to the transport envelope; it is NOT the whole wire
    story for a tool call: FastMCP's convert_result wrapping turns a dict
    return into pretty-printed text content plus a structured content
    copy, and that wrapped object is what actually leaves the process.
    The char budget is therefore measured with _wrapped_result_dumps,
    which calls this function for the final envelope dumps.
    """
    return json.dumps(value, separators=(",", ":"))


def _wrapped_result_dumps(value: dict[str, Any]) -> str:
    """Serialize *value* as the MCP client actually receives a tool result.

    Replicates the installed FastMCP convert_result wrapping plus the
    streamable-HTTP JSON-RPC envelope (mcp 2.0.0 in this container):

    - convert_result wraps a ``dict[str, Any]`` tool return in
      ``CallToolResult(content=[TextContent(text=pydantic_core.to_json(
      value, indent=2))], structuredContent=<the same dict>)``, so the
      payload rides the wire twice -- as pretty-printed text (indent=2)
      and as the structured copy;
    - the transport (mcp/server/_streamable_http_modern.py) compact-dumps
      the JSON-RPC envelope around that result.

    Built from the installed ``mcp_types`` models and ``pydantic_core``
    rather than a hand-written shape, so the measurement cannot drift
    from the transport the way the inner ``repos`` measurement did.  The
    measured unit is the convert_result object inside a fixed-size
    JSON-RPC envelope (jsonrpc/id); per-request stamps the transport may
    add (e.g. the modern-protocol serverInfo ``_meta``) are a fixed few
    hundred chars and are not included.
    """
    text = pydantic_core.to_json(value, fallback=str, indent=2).decode()
    call_tool_result = CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=value,
    )
    result = call_tool_result.model_dump(
        by_alias=True, mode="json", exclude_none=True
    )
    envelope = JSONRPCResponse(jsonrpc="2.0", id=1, result=result)
    body = envelope.model_dump(mode="json", by_alias=True, exclude_none=True)
    return _wire_dumps(body)


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


def _no_repo_candidate_response(
    all_repos: dict[str, Any],
    candidate: dict[str, Any],
    omitted_names: list[str],
    *,
    clone_refresh_debounce_seconds: int,
    sync_intervals: dict[str, int],
    token_provider: str,
    chunk_counts_source: str = "cached",
) -> dict[str, Any]:
    """Build the no-repo payload *candidate* would finish as.

    The summary counts every repo but names *omitted_names* -- the
    not-yet-admitted unhealthy repos -- as omitted, so measuring this
    dict is measuring the candidate's pessimistic final state (issue
    #431).  Each call builds a fresh summary (_summarize returns a new
    dict), so callers may reuse this freely.
    """
    summary = _summarize(all_repos)
    summary["omitted_repos"] = len(omitted_names)
    summary["omitted_repo_names"] = omitted_names
    summary["chunk_counts_source"] = chunk_counts_source
    return {
        "repos": candidate,
        "clone_refresh_debounce_seconds": clone_refresh_debounce_seconds,
        "sync_intervals": sync_intervals,
        "token_provider": token_provider,
        "summary": summary,
    }


def _no_repo_budget(
    unhealthy: list[tuple[str, dict[str, Any]]],
    *,
    all_repos: dict[str, Any],
    clone_refresh_debounce_seconds: int,
    sync_intervals: dict[str, int],
    token_provider: str,
) -> int:
    """Effective char ceiling for this call: the nominal budget, raised
    when even the payload containing only the most severe unhealthy
    record cannot fit it.

    `last_error` is duplicated at the status surface: the record's
    `last_error` field AND the `consecutive_failures > 0` warning copy
    both quote it (issue #433).  The status surface truncates it to
    _STATUS_LAST_ERROR_CHAR_CAP (cap=250, applied to both copies), so the
    canonical degraded-fleet floor -- one worst-case record (2000-char
    CJK last_error) PLUS every other unhealthy repo named in
    `summary.omitted_repo_names` -- wraps to ~11,009 chars (measured via
    _wrapped_result_dumps over the 62-repo CJK oversized fixture through
    the real status() path) instead of the pre-#433 ~50k.  A fit-or-skip
    loop would
    still emit ZERO full records on a fully degraded fleet without the
    floor -- exactly when the operator needs them most (issue #431,
    criterion 7) -- so the ceiling for this call is floored at the
    measured wrapped size of the payload containing only the most severe
    record (every other unhealthy repo named as omitted); the budget
    loop's first candidate IS that payload, so at least one full record
    always fits.  The constant stays the nominal ceiling everywhere else.
    The response is NOT guaranteed to fit an MCP client context window
    when a record's error is large: by design, truncation bounds the
    per-record cost rather than guaranteeing a window fit (#433).
    """
    if not unhealthy:
        return _NO_REPO_REPOS_CHAR_BUDGET
    name, info = unhealthy[0]
    one_record = _no_repo_candidate_response(
        all_repos,
        {name: info},
        [n for n, _ in unhealthy[1:]],
        clone_refresh_debounce_seconds=clone_refresh_debounce_seconds,
        sync_intervals=sync_intervals,
        token_provider=token_provider,
    )
    return max(_NO_REPO_REPOS_CHAR_BUDGET, len(_wrapped_result_dumps(one_record)))


def _no_repo_response(
    all_repos: dict[str, Any],
    *,
    sync_intervals: dict[str, int],
    clone_refresh_debounce_seconds: int,
    token_provider: str,
    chunk_counts_source: str = "cached",
) -> dict[str, Any]:
    """Assemble the no-repo view: `summary` plus full records for unhealthy repos.

    Filters *all_repos* for unhealthy records, sorts them by severity
    (never_indexed > index_stale > sync failure > pending_count > age),
    and admits them into `repos` in severity order until the char budget is
    spent.  The budget check measures candidate payload size directly using
    _wrapped_result_dumps, so the budget is enforced on the EXACT JSON
    string that will be served to the MCP client -- because the
    client-visible response is the wrapped one, not the inner dict
    (issue #431).  The applied ceiling is _no_repo_budget: the nominal
    _NO_REPO_REPOS_CHAR_BUDGET, raised to fit the most severe record
    alone when its wrapped form cannot fit, so at least one full
    unhealthy record is always emitted (criterion 7).  Each candidate is
    measured in its pessimistic final state (the candidate admitted,
    every not-yet-admitted unhealthy repo named as omitted), so the
    emitted payload is exactly a candidate that fit.  Unhealthy repos
    cut off by the budget are not dropped silently: `summary` reports
    the count
    (omitted_repos) and the names (omitted_repo_names) of everything not
    emitted (issue #423).
    """
    unhealthy = [
        (name, info) for name, info in all_repos.items() if not _is_healthy(info)
    ]
    unhealthy.sort(key=lambda pair: _severity_key(pair[0], pair[1]))

    # Effective ceiling for this call: at least one full record must
    # always fit, even when the most severe record's wrapped form alone
    # exceeds the nominal budget.  `last_error` is duplicated and
    # truncated at the status surface (cap=250, issue #433), so the
    # canonical floor -- one worst-case record PLUS every other unhealthy
    # repo named in `summary.omitted_repo_names` -- wraps to ~11,009
    # chars (measured via _wrapped_result_dumps over the 62-repo CJK
    # oversized fixture through the real status() path), not the pre-#433
    # ~50k; the floor is its measured payload, which is exactly the
    # loop's first candidate, so the first iteration always admits
    # (issue #431, criterion 7).
    budget = _no_repo_budget(
        unhealthy,
        all_repos=all_repos,
        clone_refresh_debounce_seconds=clone_refresh_debounce_seconds,
        sync_intervals=sync_intervals,
        token_provider=token_provider,
    )

    selected: dict[str, Any] = {}
    omitted: list[str] = []
    for name, info in unhealthy:
        candidate = {**selected, name: info}
        # Measure the payload this candidate would finish as: itself
        # admitted and every not-yet-admitted unhealthy repo named as
        # omitted.  A later admission only replaces a name in that list
        # with a larger full record, and the last candidate that fit IS
        # the emitted payload, so this keeps the emitted wrapped result
        # inside the budget (issue #431).
        presumed_omitted = [n for n, _ in unhealthy if n not in candidate]
        candidate_response = _no_repo_candidate_response(
            all_repos,
            candidate,
            presumed_omitted,
            clone_refresh_debounce_seconds=clone_refresh_debounce_seconds,
            sync_intervals=sync_intervals,
            token_provider=token_provider,
            chunk_counts_source=chunk_counts_source,
        )
        if len(_wrapped_result_dumps(candidate_response)) <= budget:
            selected = candidate
        else:
            omitted.append(name)

    summary = _summarize(all_repos)
    summary["omitted_repos"] = len(omitted)
    summary["omitted_repo_names"] = omitted
    summary["chunk_counts_source"] = chunk_counts_source
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
    until the FastMCP-wrapped tool result -- the pretty-printed text
    content plus the structured content copy that the MCP client actually
    receives -- would exceed the effective char budget for that call
    (the _NO_REPO_REPOS_CHAR_BUDGET ceiling, raised only when the most
    severe record's wrapped form alone cannot fit it, so at least one
    full record is always emitted).  Every unhealthy repo cut off by the
    budget is named in `summary.omitted_repo_names` (count in
    `summary.omitted_repos`) rather than dropped silently.  The response
    is NOT guaranteed to fit an MCP client context window when a record's
    error is large: by design (issue #433) `last_error` is truncated at
    the status surface so the worst-case single record stays near the
    nominal 8,000-char budget, and the floor still guarantees the most
    severe record is emitted even when its wrapped form alone exceeds
    the nominal budget.

    Data sources: DB metadata only (sync_run, index_state; chunk counts are read from
    repo_chunk_counts, refreshed at the end of each index run); no clone read, no GitHub API call.
    """
    try:
        provider = build_token_provider(settings)
        token_provider = provider.name
        token_provider_error = None
    except Exception as exc:  # noqa: BLE001 - status must never raise; reports "error" (issue #193)
        token_provider = "error"  # noqa: S105 - literal is "error", not a secret; name mirrors the status dict key
        token_provider_error = str(exc)

    with _conn() as conn:
        all_repos: dict[str, Any] = {}
        cached_hits = 0
        live_hits = 0
        # Filled only on the no-repo path (one GROUP BY, issue #438); the
        # single-repo path keeps the per-repo count below.
        all_issue_item_counts: dict[str, int] = {}

        if repo:
            resolved = _validate_repo_name(repo)
            targets = [resolved]
            run_info = db.get_sync_run(conn, resolved)
            runs = {resolved: run_info} if run_info else {}
            index_state_row = db.get_repo_index_state(conn, resolved)
            index_state = {resolved: index_state_row} if index_state_row else {}
            all_cached_chunk_counts: dict[str, dict[str, int]] = {}
        else:
            targets = settings.repos
            runs = db.get_sync_runs(conn)
            index_state = db.get_all_repo_index_state(conn)
            all_cached_chunk_counts = db.get_all_chunk_counts(conn)
            all_issue_item_counts = db.get_all_issue_item_counts(conn)

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
            if not repo:
                cached_counts = all_cached_chunk_counts.get(target_repo)
                if cached_counts is not None:
                    chunk_counts = cached_counts
                    cached_hits += 1
                else:
                    chunk_counts = db.get_chunk_counts(conn, target_repo)
                    db.refresh_chunk_counts(conn, target_repo)
                    live_hits += 1
            else:
                chunk_counts = db.get_chunk_counts(conn, target_repo)
            if not repo:
                items_in_db = all_issue_item_counts.get(target_repo, 0)
            else:
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
            # Truncate last_error at the status surface (issue #433): the
            # SAME truncated value feeds both the record's `last_error`
            # field and the consecutive_failures warning copy below, so the
            # duplication cost is bounded (criterion 1).
            info["last_error"] = _truncate_last_error_for_status(info.get("last_error"))
            warnings = _build_warnings(info, chunk_counts, items_in_db, cursors, threshold)
            info.pop("token_provider_error", None)
            info["warnings"] = warnings
            all_repos[target_repo] = info

    if not repo:
        if live_hits == 0:
            chunk_counts_source = "cached"
        elif cached_hits == 0:
            chunk_counts_source = "live"
        else:
            chunk_counts_source = "mixed"

        # Issue #423: the all-repos response did not fit an MCP client
        # context window (53,836 chars at 62 repos) and 91% of real calls
        # omit repo.  The no-repo payload is a separate view built under a
        # char budget (the nominal _NO_REPO_REPOS_CHAR_BUDGET as a
        # per-call effective ceiling, _no_repo_budget) measured on the
        # FastMCP-wrapped tool result (#431): healthy repos are counted in
        # `summary` only, and even unhealthy repos are emitted only until
        # the budget is spent, with the cut-off remainder named in
        # `summary` so nothing disappears silently.
        return _no_repo_response(
            all_repos,
            sync_intervals={
                "dev": settings.dev_sync_interval_seconds,
                "ref": settings.ref_sync_interval_seconds,
            },
            clone_refresh_debounce_seconds=settings.sync_interval_seconds,
            token_provider=token_provider,
            chunk_counts_source=chunk_counts_source,
        )
    # Repo-specified path: byte-identical to the pre-#423 object -- that
    # repo's full record, no summary key, same top-level key order.
    return {
        "repos": all_repos,
        "clone_refresh_debounce_seconds": settings.sync_interval_seconds,
        "sync_intervals": {
            "dev": settings.dev_sync_interval_seconds,
            "ref": settings.ref_sync_interval_seconds,
        },
        "token_provider": token_provider,
    }
