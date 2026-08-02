from __future__ import annotations

from importlib.metadata import version as _pkg_version

from mcp.server.mcpserver import MCPServer

# host/port moved off the constructor in mcp 2.x: they are now run() kwargs
# (see mcp_server.run). instructions= must stay keyword -- v2 inserted
# title/description positional params before it.
mcp = MCPServer(
    "shiori",
    # serverInfo.version: mcp 2.x reports an empty string when unset (the
    # SDK's own version is no longer substituted), so wire the package
    # version through explicitly.
    version=_pkg_version("shiori"),
    instructions=(
        "Project-knowledge search MCP: one unified, cross-lingual (ja/en) index over GitHub "
        "repository knowledge — Markdown docs, source code, and issue/PR discussions — searchable "
        "in a single query and traversable across sources (issue -> fix PR -> changed files -> docs). "
        "Not a RAG server — shiori returns pointers + snippets and never generates answers; "
        "you decide what to fetch.\n"
        "\n"
        "Workflow: start with shiori_search (pointers + snippets), then fetch only the needed "
        "range via shiori_read_file / shiori_read_issue. For exact matches (proper nouns, API "
        "names, error codes, function names) use shiori_keyword_search. Check index freshness "
        "with shiori_status.\n"
        "\n"
        "repo parameter (every tool): \"owner/name\", or a short name when it uniquely matches "
        "one configured repo (e.g. \"shiori\" -> \"owner/shiori\"). When omitted, search tools "
        "search ALL indexed repos; single-target tools (read/grep/list_tree/report) infer the "
        "repo from cwd and otherwise fall back to the first configured repo. shiori_grep also "
        "accepts repo=\"*\" to grep every repo.\n"
        "\n"
        "Repo roles (shiori_status shows the role per repo): dev repos index code, so "
        "shiori_search(source_type='code') finds code chunks; ref repos keep code clone-only — "
        "use shiori_grep for their code. Issues/PRs/docs are indexed for both roles.\n"
        "\n"
        "Data sources: every tool's description states its own contract as a \"Data sources:\" line. "
        "The three stores: 1) search index (Postgres; filled by ingest, stale if host-level timers "
        "are off) — shiori_search / shiori_keyword_search / shiori_list_tree(doc); 2) clone on disk "
        "(refreshed on access) — shiori_read_file / shiori_grep / shiori_list_tree(code) / "
        "shiori_report; 3) GitHub API (live, per call) — shiori_read_issue / shiori_pr_diff / "
        "shiori_pr_review_comments / shiori_issue_links; shiori_read_pr_file / shiori_pr_changes "
        "fetch the PR head into the clone themselves.\n"
        "\n"
        "Full tool-contract map: docs/design/06_mcp_server_and_tool_design.md."
    ),
)
