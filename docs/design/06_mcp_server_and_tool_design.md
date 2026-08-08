# Detailed Design: MCP Server & Tool Design

## 1. Purpose

Expose retrieval and inspection functionality as Model Context Protocol (MCP) tools, enabling AI agents to search and inspect data using the *pointer-then-fetch* workflow. (For categories and detailed developer use cases, see [Product Definition & Use Cases](13_product_definition_and_use_cases.md)).

---

## 2. Tool Reference Index

*   `shiori_search(query, filters?)`: Performs unified hybrid semantic + keyword search (RRF, k=60). Returns a list of matched chunk pointers and content snippets.
*   `shiori_keyword_search(query, filters?)`: Morphologically tokenized exact-match search. Used to locate precise identifier matches.
*   `shiori_list_tree(path?, source_type?, extension?)`: Lists indexed files. Filterable by `source_type` (`doc` or `code`) and file extension (e.g. `.py`, `.md`) (Issue #43).
*   `shiori_read_file(path, range?)`: Reads local files from the shallow repository clone (supports line range slicing).
*   `shiori_read_issue(number, repo?, exclude_noise_bots?)`: Retrieves full issue or PR thread timelines sequentially. Setting `exclude_noise_bots=true` filters out comments from non-allowlisted bots (Issue #44).
*   `shiori_pr_changes(number, repo?, include_diff?)`: Retrieves a list of modified files in a PR. Returns file path, status, and line counts. Setting `include_diff=true` also returns unified diff and stats. Computed from git clone directly (`git diff --name-status` + `--numstat`) — no longer depends on the `pr_changes` DB table. `blob_url` is omitted (not available from git alone) (Issue #259).
*   `shiori_pr_diff(number, path?, repo?)`: Computes and returns the unified Git diff of a PR without modifying the local active working tree (Issue #96). **Breaking change (v2)**: no longer returns `head_sha` / `base_sha` (removed the `pr_changes` DB dependency). The diff is now computed purely from the clone (Issue #259).
*   `shiori_pr_review_comments(number, repo?)`: Lists review comments with line numbers and file paths (Issue #96).
*   `shiori_issue_links(number, repo?)`: Analyzes issue descriptions and comments to identify cross-references (such as closes, duplicate, refs, or mentions), returning target title and state (Issue #97).
*   `shiori_read_pr_file(number, path, range?, repo?)`: Fetches the file content at the head commit of a PR by pulling a temporary ref (Issue #81).
*   `shiori_grep(pattern, repo?, path?, regex?, ignore_case?, max_results?)`: Performs line-level ripgrep search inside cloned repositories. Designed as a Stage-2 search. Setting `repo="*"` executes cross-repository searches (Issue #146, #151).
*   `shiori_status()`: Queries system health, sync state cursors, and database row allocations per repository (Issue #22, #31). Reports `auto_sync_running` thread health, token provider strategies, and warning logs (Issue #187, #196).
*   `shiori_report(template, repo?, path?, kind?, public_only?, max_results?, prog_lang?, max_chars?)`: Generates a structured report from the on-disk clone (`_ensure_phase1` refreshes it first; the `api_reference` template additionally reads the search index for cross-linking). Templates: `stats`, `module_tree`, `symbol_index`, `api_reference` (Issue #279).

> **v2.0 Deprecation**: The `shiori_ingest` MCP tool has been deprecated. Synchronization is managed via CLI (`python -m shiori ingest`) or background polling (`SHIORI_SYNC_INTERVAL_SECONDS`).

---

## 3. Status Warning Triggers (Issue #31, #35, #187)

The `warnings` list in `shiori_status` automatically identifies the following system warnings:

| Warning Condition | Meaning |
|---|---|
| `age_seconds` exceeds stale threshold | The index has not synced recently. The threshold is `max(sync_interval_seconds * 30, 300s)` when auto-sync is enabled, and 24 hours otherwise. |
| `consecutive_failures > 0` | Sync attempts are failing. Logs the `last_error` exception. |
| Token Provider initialization error | Token configurations are broken (reports `token_provider: "error"`, Issue #193). |
| `chunks["issue"] + chunks["pr_review"] < items_in_db // 2` | Matched chunks are abnormally low compared to database items. Indicates indexing drops. |
| Missing categories in sync state | Incremental ingestion is incomplete. |

---

## 4. Design Guidelines

*   **Explicit Tool Descriptions**: Since AI model routing accuracy decreases as tool counts grow, each tool description clearly details its purpose. `shiori_search` is explicitly marked as the primary entry point.
*   **Pointer Principle**: Tool results return pointers to avoid bloat. PR diff contents are read dynamically via `shiori_pr_changes` or `shiori_pr_diff` rather than indexed.
*   **PR File Read Isolation**: `shiori_read_pr_file` pulls PR branches to an isolated temporary git reference (`refs/shiori/tmp-{uuid}`) and deletes the ref on completion. This prevents concurrent read requests from causing race conditions or modifying the cloned workspace.

---

## 5. Decisions

*   `shiori_search` is the unified hybrid search tool, managing Reciprocal Rank Fusion (RRF) internally (Issue #40).
*   `shiori_keyword_search` is kept for exact identifier matching.
*   `shiori_read_pr_file` is created to fetch file contents at PR head without modifying the main branch (Issue #81).
*   `shiori_grep` supports cross-repository searches via `repo="*"` (Issue #151).
*   `shiori_grep` defaults `regex` to `True` to match developer habits. Fixed-string matching is supported by passing `regex=False` (Issue #152).

---

## 6. Tool-Contract Map (Issue #340)

The server `instructions` text is the Map: the search workflow, the shared `repo` parameter
semantics, and the store summary (search index / clone on disk / GitHub API) are documented
there once; tool docstrings carry no copy. Each tool's docstring is that tool's Contract:
its *precise* data-source contract as a one-line `Data sources:` sentence. mcp 2.x serves the
docstring verbatim as the description; keep the line at top level, never under an `Args:`
section (under SDK v1 everything from `Args:` onward was dropped from the visible
description, per Issue #550 -- the placement rule survives the SDK line either way).
`tests/test_tool_contracts.py` enumerates the live tool registry and fails if any tool is
missing that line. The table below is the category-level summary; it does not replace the
per-tool docstrings.

| Category | Tools | Needs |
|---|---|---|
| ① search | `shiori_search`, `shiori_keyword_search` | search index (chunks) -- embedding required |
| ②a GitHub REST API | `shiori_read_issue`, `shiori_pr_review_comments`, `shiori_issue_links` | GitHub REST API, live on every call; `issue_items` only as optional enrichment |
| ②b PR-head git fetch | `shiori_pr_changes`, `shiori_pr_diff`, `shiori_read_pr_file` | own `git fetch` of the PR head/base ref against the on-disk clone -- neither the Phase 1 refresh nor the REST API |
| ③ clone read | `shiori_read_file`, `shiori_grep`, `shiori_list_tree` | clone on disk (`_ensure_phase1`); exception: `shiori_list_tree(source_type='doc')` reads the `doc_files` index instead of walking the clone |
| ④ state | `shiori_status`, `shiori_report` | DB metadata (`shiori_report` also refreshes the clone, and reads the search index for its `api_reference` template) |

Frozen design decisions (ratified in Issue #340/#347; do not re-litigate without a new issue):

*   Category ②a stays API-direct. There is no caching layer for it, and none is planned;
    `issue_items` remains supplementary enrichment only (currently exercised by
    `shiori_issue_links` for target titles and inbound refs), never the primary source.
*   Neither ②a nor ②b calls `_ensure_phase1`. ②b's git fetch is its own contract,
    documented in each tool's own docstring; it starts from the clone but does not
    perform the Phase 1 refresh.
*   Category ③ (plus `shiori_report`) keep `_ensure_phase1`.

---

## 7. Web Dashboard (shared app, no second process)

`register_dashboard(mcp)` (`src/shiori/dashboard.py`) mounts a human-facing
browser dashboard on the **same** Starlette app and port as the MCP endpoint
rather than running a second process. It registers several
`@mcp.custom_route` JSON endpoints under `/api/`, and serves the built
single-page app as static files at `/` -- appended to the private
`_custom_starlette_routes` list as a `Mount`, since mcp 2.0.0 still offers no
public API for mounting a Starlette sub-app. When `dashboard_dist` (the
built SPA next to the package) is absent, a fallback 404 page telling the
operator to run `npm install && npm run build` in the `dashboard/` directory
is served at `/` instead.

| Route | Backing function |
|---|---|
| `GET /api/repos` | `settings.repos` (configured repository list) |
| `GET /api/search` | `search.semantic_search` / `search.keyword_search` via `_conn`, `_get_embedder`, `_resolve_repo_filter` |
| `GET /api/read_file` | `mcp_server.read_file` |
| `GET /api/issue` | `mcp_server.read_issue` |
| `GET /api/report` | `mcp_server.report` |

The `/api/` routes reuse the same underlying functions as the MCP tools
instead of duplicating logic: `mcp_server.py` re-exports `_get_embedder` and
`_resolve_repo_filter` with `# noqa: F401 — re-export for dashboard` comments
for exactly this purpose, and the tool functions themselves are imported
directly. Query parameters map onto the tools' keyword arguments; missing
required parameters return HTTP 400 and tool errors surface as HTTP 500.
