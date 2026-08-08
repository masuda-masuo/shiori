"""MCP server implementation.
~1100 lines: setup (1-90), helpers (90-310), tools (310-1100). Each tool typically <100 lines.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from . import schema
from .dashboard import register_dashboard
from .pipeline import (
    _conn,
    _do_sync,
    _ensure_phase1,  # noqa: F401 — re-export for tests
    _get_embedder,  # noqa: F401 — re-export for dashboard
    settings,
)
from .tools.common import (
    _get_token_provider,  # noqa: F401 — re-export for tests
    _infer_repo_from_cwd,  # noqa: F401 — re-export for tests
    _make_filters,  # noqa: F401 — re-export for tests
    _resolve_repo,  # noqa: F401 — re-export for tests
    _resolve_repo_filter,  # noqa: F401 — re-export for dashboard
    _resolve_repos,  # noqa: F401 — re-export for tests
    _validate_repo_name,  # noqa: F401 — re-export for tests
)
from .tools.registry import mcp

log = logging.getLogger(__name__)

from .tools import (  # noqa: E402, F401 — import registers @mcp.tool as side effect
    search as _t_search,
)
from .tools.grep import grep_search  # noqa: F401, E402
from .tools.links import issue_links  # noqa: F401, E402
from .tools.list_tree import list_tree  # noqa: F401, E402
from .tools.pr import (  # noqa: F401, E402
    _compute_pr_diff,
    pr_changes,
    pr_diff,
    pr_review_comments,
)
from .tools.read import (  # noqa: F401, E402
    _read_issue_single,
    read_file,
    read_issue,
    read_pr_file,
)
from .tools.report import report  # noqa: F401, E402

# Re-export for backward-compatible imports
from .tools.search import keyword_search, semantic_search  # noqa: F401, E402
from .tools.status import (  # noqa: F401, E402
    _build_warnings,
    _stale_threshold_seconds,
    status,
)


def ingest(rebuild: bool = False, repo: str | None = None) -> dict[str, Any]:
    """Sync docs/issues/code from GitHub and update index (diff sync, typically seconds).
    Check index freshness with shiori_status first. Freshness is maintained by host-level
    systemd timers calling the CLI (issue #347, role-scoped --only-dev/--only-ref), not by
    this tool -- call it directly to sync now regardless of timer cadence.
    rebuild=True: discard and full rebuild (requires SHIORI_ALLOW_REBUILD=true; issue #63).
    Also treated as rebuild when chunks table is empty."""
    if rebuild and not settings.allow_rebuild:
        raise ValueError(
            "rebuild=True cannot be executed from the MCP tool. "
            "Use the CLI (python -m shiori ingest --rebuild) or "
            "set the environment variable SHIORI_ALLOW_REBUILD=true."
        )
    return _do_sync(repos=[repo] if repo else None, rebuild=rebuild, route="mcp")

register_dashboard(mcp)


def run(transport: Literal["stdio", "streamable-http"] = "streamable-http") -> None:
    with _conn() as conn:
        # migrate_light only (issue #352): a full migrate() here would build
        # HNSW/pgroonga on every server restart, which is an hours-long
        # operation if a reindex/rebuild drain is in progress and had
        # deliberately dropped the heavy indexes. Search still works without
        # them (just slower); _do_sync/run_index rebuild them once a drain
        # completes.
        schema.migrate_light(conn, settings)
    log.info(
        "shiori MCP server starting (%s); index freshness is maintained by "
        "host-level systemd timers (issue #347), not this process",
        transport,
    )
    # host/port are run() kwargs in mcp 2.x (moved off the constructor).
    if transport == "streamable-http":
        mcp.run(transport="streamable-http", host=settings.mcp_host, port=settings.mcp_port)
    else:
        # stdio has no binding kwargs in v2 -- passing host/port raises
        # TypeError on unrecognized transport kwargs.
        mcp.run(transport="stdio")
