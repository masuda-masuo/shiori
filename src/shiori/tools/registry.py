from __future__ import annotations

from mcp.server.mcpserver import MCPServer

# host/port moved off the constructor in mcp 2.x: they are now run() kwargs
# (see mcp_server.run). instructions= must stay keyword -- v2 inserted
# title/description positional params before it.
mcp = MCPServer(
    "shiori",
    instructions=(
        "Project-knowledge search MCP: one unified, cross-lingual (ja/en) index over GitHub "
        "repository knowledge \u2014 Markdown docs, source code, and issue/PR discussions \u2014 searchable "
        "in a single query and traversable across sources (issue -> fix PR -> changed files -> docs). "
        "Not a RAG server \u2014 shiori returns pointers + snippets and never generates answers; "
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
        "via shiori_issue_links (issue #97) \u2014 useful for duplicate checks and tracing fixes. "
        "\n"
        "\u25a0 Repo roles (shiori_status shows role per repo)\n"
        "Each repo is either dev or ref:\n"
        "- dev (SHIORI_DEV_REPOS): code IS indexed. shiori_search(source_type='code') finds code chunks.\n"
        "- ref (not in SHIORI_DEV_REPOS): code is clone-only. Use shiori_grep for code; shiori_search still finds issues/PRs/docs.\n"
        "shiori_list_tree(source_type='code') works for both (walks clone on disk).\n"
        "\n"
        "■ Two-store model (information sources)\n"
        "shiori has 2 independent data sources; each tool's docstring states its own "
        "contract as a 'Data sources:' line -- this is the summary:\n"
        "1. Index (Postgres/pgvector/pgroonga)\n"
        "   - shiori_search / shiori_keyword_search: embedding + full-text search\n"
        "   - shiori_list_tree (source_type='doc'): indexed doc_files table\n"
        "   - Freshness comes from host-level timers (dev ~15min / ref daily) "
        "plus manual `ingest` -- there is no in-server pull-type sync.\n"
        "2. Clone (disk, pinned to main branch)\n"
        "   - shiori_read_file / shiori_grep / shiori_list_tree (source_type='code'): "
        "read the clone, refreshed on access (Phase 1: git fetch + reset --hard)\n"
        "   - shiori_report: reads the clone (refreshed the same way) plus the index "
        "for its api_reference template\n"
        "3. GitHub API (live, no cache)\n"
        "   - shiori_read_issue / shiori_pr_diff / shiori_pr_review_comments / "
        "shiori_issue_links: fetched directly from the GitHub API each call\n"
        "   - shiori_issue_links additionally reads the index (issue_items) as "
        "optional enrichment for target titles/inbound refs\n"
        "   - shiori_read_pr_file / shiori_pr_changes: their own git fetch of the PR "
        "head against the clone (not the Phase 1 refresh, not the GitHub REST API)\n"
        "See docs/design/06_mcp_server_and_tool_design.md for the full tool-contract map."
    ),
)
