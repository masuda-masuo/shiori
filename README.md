# shiori 栞

**Project Knowledge Search MCP** — A local MCP server that enables AI agents to search across and navigate between project knowledge scattered throughout GitHub repositories (documents, source code, issues, and PR threads). Instead of feeding entire files into the AI's context window, it returns "pointers" to relevant locations.

> *栞 (shiori)* = Bookmark. The core design principle is that the search tool returns a bookmark, and the AI agent retrieves the full text only when it decides it is necessary.

Shiori is not a generator or context injector. It does not generate answers or automatically stuff contexts; it focuses purely on retrieval and knowledge navigation. The responsibility of reasoning and generation remains with the agent. For definitions and use cases, see [docs/design/13_product_definition_and_use_cases.md](docs/design/13_product_definition_and_use_cases.md).

---

## Why Shiori?

Answering a question about a project requires information spread across different types of sources. "What" and "How" are defined in documents and code on the `main` branch. However, the "Why" — the history of design decisions, rejected alternatives, known bugs, and workarounds — lives only in issue and PR threads.

While code and documents can be read using `grep` once a repository is cloned, issue and PR discussions exist only on GitHub. Standard GitHub search is neither semantic nor cross-lingual, meaning AI agents have no practical way to discover context buried in closed threads.

Shiori solves this by placing these three knowledge types (documents, code, and issues/PRs) in a **single index for cross-searching and linking**. A single query returns documents, code, and discussions in the same unified ranking. The AI can then cross boundaries by navigating from a hit issue to its resolving PR ([`shiori_issue_links`](#mcp-tools)), from the PR to modified files ([`shiori_pr_changes`](#mcp-tools)), and on to design specs and implementation. Cross-lingual search operates across languages, enabling queries in Japanese to find documents and issues in English (and vice versa).

Returning pointers (the *pointer-then-fetch* pattern) prevents search results from bloating the AI's context window.

---

## Purpose and Scope

Shiori's purpose is to search and navigate between **factual specs** (documents and code on the main branch) and the **records of intent** (issue/PR discussions).

Factual specs represent the most common queries ("what is the current state?"). However, since specs can be read using clones and grep, the primary value of Shiori is linking to issue/PR threads — records of "why" containing past (closed), present (active), and future/draft decisions.

Shiori does not host full files or diffs for ongoing PRs (which would duplicate GitHub). Instead, it returns **pointers** (file coordinates, line ranges, or URLs) to these files, delegating full-text retrieval of in-flight resources to servers like the GitHub MCP. Shiori serves as the read-only foundation for analysis and duplicate issue checking, while modifications (creating issues, posting comments) are delegated.

---

## Information Sources

| Source | Content | Character | Store | Purpose |
|---|---|---|---|---|
| **Documents** | Markdown on `main` | Factual & Current | Postgres + Disk Clone | What & How |
| **Source Code** | `.py` / `.ts` / `.go` on `main` | Factual & Current | Postgres + Disk Clone | Current implementation status |
| **Issues** | Description & Comments | Blended history | Postgres | Why (history, workarounds) |
| **Pull Requests** | Description & Review Comments | Blended history | Postgres | Why (design decisions) |
| **PR Changes** | File list + additions/deletions + head_sha / base_sha | In-flight metadata | Disk Clone (git fetch of PR head) | Start point for reviews |

Stores are divided into two categories:
*   **Database (PostgreSQL)**: Serves search requests. Returns pointers and snippets.
*   **Clones (Local Disk)**: Serves full-text reads via `shiori_read_file`, and PR change lists and diffs computed from the clone (live git fetch of the PR head) via `shiori_pr_changes` / `shiori_pr_diff`. Represents shallow checkouts of the `main` branch.

Bot comments (e.g. Dependabot) are excluded from the index to reduce noise. Specific bots posting on behalf of users can be allowlisted using the `SHIORI_INDEX_BOT_LOGINS` environment variable (comma-separated names, e.g. `app[bot]`).

---

## How It Works

1.  **Ingestion**: Four subcommands — `fetch` (API + git pull, no embeddings), `index` (chunk + embed from stored rows), `run` (both, the default), and `reindex` (re-chunk + re-embed from stored raw data, no GitHub re-fetch). Downloads docs and issues/PRs, splits them into chunks (documents are chunked by headings, discussions by comments with title context), and attaches metadata. Supports differential sync with page-level resumability.
2.  **Indexing**: Stores vector embeddings, full-text tokens, and metadata in PostgreSQL.
3.  **Search**: Runs hybrid search (vector similarity + full-text keyword search via Reciprocal Rank Fusion) combined with metadata filtering.
4.  **Delivery**: Exposes search APIs as MCP tools for the AI agent.

---

## Architecture

*   **Store**: PostgreSQL with `pgvector` (embeddings) + `pgroonga` (Japanese-capable full-text search with tokenization). Vector, keyword, and metadata layers are handled in a single database, with hybrid RRF fusion written in SQL.
*   **Embeddings**: Local cross-lingual model (defaults to `multilingual-e5-small`). Runs locally to protect data privacy.
*   **Runtime**: Docker Compose separating the DB and MCP server. DB volumes and model caches are persisted.

---

## MCP Tools

The 13 tools are classified into 4 layers based on user query intent:

### 1. Retrieval (Where is it written?)
*   `shiori_search`: Unified hybrid search (vector + keyword RRF). Strong at conceptual mapping, phrasing variations, and cross-lingual queries.
*   `shiori_keyword_search`: Exact match and identifier search. Designed for function names, APIs, and error codes.
*   `shiori_grep`: Line-level ripgrep on cloned repositories. Used as a Stage-2 search.

### 2. Inspection (What is written?)
*   `shiori_read_file`: Reads files from the main-branch clone (supports line ranges).
*   `shiori_read_issue`: Retrieves issues or PR timelines sequentially (supports batching).
*   `shiori_read_pr_file`: Reads files at a specific PR's head commit.
*   `shiori_list_tree`: Lists file paths for indexed documents and code.

### 3. Relationships & Diff (How is it connected? What changes?)
*   `shiori_issue_links`: Returns inbound and outbound links between issues and PRs.
*   `shiori_pr_changes`: Returns metadata of files changed in a PR.
*   `shiori_pr_diff`: Calculates and returns unified diffs for a PR (supports path scoping).
*   `shiori_pr_review_comments`: Lists review comments with line numbers.

### 4. Operations (Is the index fresh? What is the repository structure?)
*   `shiori_status`: Inspects index status, sync times, and warnings.
*   `shiori_report`: Generates structured reports (`stats`, `module_tree`, `symbol_index`, `api_reference`) from the clone and the search index.

---

## Browser Dashboard

The MCP server also exposes a human-facing browser dashboard on the same server and port as the MCP endpoint (`http://localhost:8765/`): JSON endpoints under `/api/` backed by the same functions as the MCP tools, plus the built single-page app served as static files at `/`.

*   `GET /api/repos` — configured repositories
*   `GET /api/search` — hybrid search (`type=semantic|keyword`, plus filters)
*   `GET /api/read_file` — file reads with line ranges
*   `GET /api/issue` — issue/PR timeline reads
*   `GET /api/report` — structured reports (`stats`, `module_tree`, `symbol_index`, `api_reference`)

The dashboard must be built first (`npm install && npm run build` in the `dashboard/` directory); until then a fallback page telling you to build it is served at `/`. See [docs/design/06_mcp_server_and_tool_design.md](docs/design/06_mcp_server_and_tool_design.md) for the design.

---

## Quick Start

```bash
cp .env.example .env   # Configure SHIORI_REPOS; see below for role annotations
docker compose up -d --build

# Initial ingestion (may take time to download the embedding model)
docker compose run --rm ingest --repo owner/repo

# Adding a large reference repo? Bound the backfill:
# ./scripts/ingest.sh run --backfill-since 2024-01-01 --repo owner/repo
```

Ingestion has four subcommands — `fetch` (API + git pull only), `index` (chunk + embed), `run` (both, default), and `reindex` (rebuild chunks while keeping fetched raw data, Issue #352). The MCP server is exposed at `http://localhost:8765/mcp` (Streamable HTTP).

See [docs/guides/setup.md](docs/guides/setup.md) for:
- **Reference (read-only) setup** — public repos, no token, one-shot bounded ingest
- **Development (writable) setup** — code indexing, PR review sync, GitHub App token

### Environment Variables

- **`SHIORI_REPOS`**: Comma-separated target repos (`owner/name`).
- **`SHIORI_DEV_REPOS`**: Comma-separated repos (`owner/name`) that have source code indexed and PR review comments synced.
- **`SHIORI_DOCS_ONLY_REPOS`**: Comma-separated repos (`owner/name`) whose issue trackers (issues, PRs, comments, reviews) are neither fetched nor indexed. Docs are still synced. This setting is independent of `SHIORI_DEV_REPOS` (a repo's code indexing status is decided solely by `SHIORI_DEV_REPOS`). Setting this variable for an already-indexed repo leaves its existing issue rows in place without deleting them.
- **`SHIORI_INDEX_BOT_LOGINS`**: Comma-separated bot logins to allowlist for indexing.

The [design doc](docs/design/01_data_ingestion_and_sync.md) covers the architecture: three cursor streams, per-repo PG advisory locks for parallel containers, dev-first ordering, and the ingest strategy.

### Removing Repositories from the Index
Removing a repository from `SHIORI_REPOS` does not delete its existing rows or disk clones. Delete it explicitly using `forget`:

```bash
docker compose run --rm app python -m shiori forget --repo owner/name
```
This removes database entries, counts deleted rows, and deletes local disk clones.

---

## Documentation Map

*   [docs/design/00_basic_design.md](docs/design/00_basic_design.md) — Architectural overview, design policies, and decision logs.
*   `docs/design/01_*.md` to `docs/design/12_*.md` — Topic-specific detailed design logs.
*   [docs/design/13_product_definition_and_use_cases.md](docs/design/13_product_definition_and_use_cases.md) — Definitions, tool catalogs, and use cases.
*   [docs/design/14_multi_source_abstraction.md](docs/design/14_multi_source_abstraction.md) — Abstract design for supporting non-GitHub sources.
*   [docs/design/15_token_supply_path.md](docs/design/15_token_supply_path.md) — Credential caching and refresh rules.
*   [docs/guides/setup.md](docs/guides/setup.md) — Onboarding and operational guides.
