# AGENTS.md — Agent Instructions for the Shiori Project

## Core Rule Before Using MCP Tools

**When using an unfamiliar MCP tool, you must search for the tool's documentation in Shiori first.**
Do not guess how a tool works. Run `shiori_search` with the tool name plus keywords like "usage" or "workflow" to verify the standard pattern from the README or design documents before taking action.

---

## Shiori (shiori MCP)

Shiori is a project knowledge search MCP server. Its definition and use cases are detailed in [docs/design/13_product_definition_and_use_cases.md](docs/design/13_product_definition_and_use_cases.md).

### Search (Where is it written?)

*   `shiori_search`: The primary hybrid search entry point. Use this first.
*   `shiori_keyword_search`: Exact match search for function names, API names, error codes, etc.
*   `shiori_grep`: Run line-level grep on repository clones (Stage-2 search after narrowing down files; use `repo="*"` to grep across all repos).

### Read (What is written?)

*   `shiori_read_issue`: Retrieve the entire timeline thread of an issue or pull request.
*   `shiori_read_file`: Read a local cloned file (supports line ranges).
*   `shiori_read_pr_file`: Read a file at a specific PR's head commit.
*   `shiori_list_tree`: Browse repository file structures (filterable by `source_type` or `extension`).

### Relationships & Changes (What is linked? What changes?)

*   `shiori_issue_links`: Returns inbound and outbound links between issues and PRs (such as closes, duplicate, refs, or mentions).
*   `shiori_pr_changes`: Retrieve the map of modified files in a PR.
*   `shiori_pr_diff`: Retrieve the unified diff for a PR.
*   `shiori_pr_review_comments`: Retrieve review comments (with paths and line numbers) for a PR.

### Operations

*   `shiori_status`: Check indexing status, freshness, and warnings (unnecessary if auto-sync is enabled).

---

## Sunaba (sunaba MCP)

**Standard Pattern: Execute via `run_container_and_exec` for single-shot operations**

```python
run_container_and_exec(
    image="python@sha256:...",       # Optional (defaults to default image)
    clone_repo="owner/repo",         # Copies a pre-cloned repo from Shiori (sub-second copy, no network needed)
    clone_dest="/app",               # Clone destination (defaults to /tmp/repo)
    commands=[
        "cd /app && pip install -e '.[dev]'",
        "cd /app && pytest tests/ -v"
    ],
    allow_network=True,              # Required to run pip install
    inject_vcs_token=True            # Required to authenticate private repositories
)
```

*   Specifying `clone_repo` copies Shiori's local pre-cloned repository using `cp -r` (taking less than a second, bypassing the network). See `docs/design/12_clone_management_and_integration.md`.
*   If `clone_repo` is omitted, run a standard `git clone` with `allow_network=True` and `inject_vcs_token=True`.
*   If cloning hangs, set `GIT_TERMINAL_PROMPT=0` to check for interactive prompts.
*   The default Docker image pre-installs `ripgrep`, `ast-grep`, and `fd` for code search.
*   Use `sandbox_initialize` and `sandbox_exec` only for long-lived, multi-turn sessions.

---

## GitHub MCP

*   Create PRs: `github_create_pull_request`
*   Modify files: `github_create_or_update_file`, `github_push_files`
*   Manage issues: `github_issue_read`, `github_issue_write`

---

## Project-Specific Instructions

*   Run tests: `PYTHONPATH=src python3 -m pytest tests/ -v`
*   Production DB dependencies: Requires PostgreSQL running (starts via Docker Compose).
*   Target Repositories: `masuda-masuo/shiori`, `masuda-masuo/sunaba`
*   Update index via CLI: Run `python -m shiori ingest --repo owner/repo` (the `shiori_ingest` MCP tool is deprecated).
