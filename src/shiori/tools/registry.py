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
        'Search GitHub docs, code and issue/PR discussions (ja/en); returns '
        'pointers/snippets, not generated answers.\n'
        'Use shiori_search first; shiori_keyword_search for identifiers; fetch only needed '
        'file ranges/threads. Check index freshness with shiori_status.\n'
        'repo accepts owner/name or a unique short name. Omitted: search covers all indexed '
        'repos; read/grep/list_tree/report infer cwd, then first configured repo. grep '
        'repo="*" covers all repos.\n'
        'Roles: dev code is indexed; ref code is clone-only (use grep). Both index '
        'docs/issues/PRs.\n'
        "Each tool's Data sources line specifies freshness: index needs ingest; clone "
        'refreshes on access; GitHub API is live. PR file/changes fetch their refs '
        'directly.\n'
        'Full contracts: docs/design/06_mcp_server_and_tool_design.md.'
    ),
)
