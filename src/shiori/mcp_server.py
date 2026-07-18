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
    _get_embedder,  # noqa: F401 — re-export for dashboard
    settings,
)
from .tools.registry import mcp
from .tools.common import (
    _get_token_provider,  # noqa: F401 — re-export for tests
    _validate_repo_name,  # noqa: F401 — re-export for tests
    _infer_repo_from_cwd,  # noqa: F401 — re-export for tests
    _resolve_repo,  # noqa: F401 — re-export for tests
    _resolve_repo_filter,  # noqa: F401 — re-export for dashboard
    _resolve_repos,  # noqa: F401 — re-export for tests
    _make_filters,  # noqa: F401 — re-export for tests
)

log = logging.getLogger(__name__)

from .tools import (  # noqa: E402, F401 — import registers @mcp.tool as side effect
    search as _t_search, list_tree as _t_list_tree, read as _t_read,
    pr as _t_pr, grep as _t_grep, report as _t_report, links as _t_links,
    status as _t_status,
)
# Re-export for backward-compatible imports
from .tools.search import semantic_search, keyword_search  # noqa: F401, E402
from .tools.list_tree import list_tree                      # noqa: F401, E402
from .tools.read import read_file, read_issue, read_pr_file, _read_issue_single  # noqa: F401, E402
from .tools.pr import pr_changes, pr_diff, pr_review_comments, _compute_pr_diff  # noqa: F401, E402
from .tools.grep import grep_search                          # noqa: F401, E402
from .tools.report import report                             # noqa: F401, E402
from .tools.links import issue_links                         # noqa: F401, E402
from .tools.status import status, _build_warnings, _stale_threshold_seconds  # noqa: F401, E402


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

register_dashboard(mcp)


def run(transport: Literal["stdio", "sse", "streamable-http"] = "streamable-http") -> None:
    with _conn() as conn:
        schema.migrate(conn, settings)
    log.info("shiori MCP server starting (%s), pull-type sync (#236)", transport)
    mcp.run(transport=transport)
