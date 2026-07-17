from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..pipeline import settings

mcp = FastMCP(
    "shiori",
    host=settings.mcp_host,
    port=settings.mcp_port,
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
        "\u25a0 Two-store model (information sources)\n"
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
