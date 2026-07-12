# Detailed Design: Data Ingestion & Sync

## 1. Purpose

Fetch documents and issue/PR threads from GitHub repositories, store them locally, and maintain up-to-date search indexes.

---

## 2. Ingestion Paths

*   **Documents (Markdown)**: Pulled via `git clone` or `git pull`. Shallow clones (`depth=1`) are checked out on disk to extract `.md` files.
*   **Issues, PRs, and Comments**: Downloaded via GitHub REST/GraphQL APIs.
*   **PR Review Submissions**: Fetched via the reviews API (`/pulls/{number}/reviews`) to capture approval and changes-requested summaries and state (APPROVED, CHANGES_REQUESTED, COMMENTED) which are not part of standard comment lists (Issue #103).
*   **Private Repositories**: Authenticated using personal access tokens (PATs) or GitHub Apps.
*   **Rate Limits**: GitHub API rate limits and pagination boundaries are handled gracefully.

---

## 3. Synchronization Strategy

*   **Documents**: Synced via periodic Git fetch/pull (lower frequency).
*   **Issues/PRs**: Synced incrementally using `updated_at` timestamps to fetch only modified entries.
*   **Execution**: Run as a standalone ingestion job (`ingest`), either manually or via timers.

---

## 4. Metadata Mapping
All chunks are tagged during ingestion with the following metadata columns:
*   `repo`, `path`, `source_type` (`doc`, `issue`, `pr_review`, `code`), `language` (`ja`/`en`), `state` (`open`/`closed`), `author`, `created_at`, `updated_at`, issue/PR number,見出しパス (heading path for docs), and file/line numbers (for reviews).

---

## 5. Ingestion Decisions (v1.0)

*   **MCP Tool Integration**: Initially, ingestion was only a CLI command. Issue #6 updated this to expose ingestion as the `shiori_ingest(rebuild?)` MCP tool. To prevent timeouts, the tool relies on incremental diff sync by default.
*   **Differential Sync Mechanics**:
    *   *Docs*: Computes the SHA-256 hash of each file and compares it against `doc_files`. Only files with modified hashes are re-chunked and re-indexed. Stale records of deleted files are purged.
    *   *Issues/PRs*: Queries three endpoints (`/issues`, `/issues/comments`, `/pulls/comments`) sorted by `updated` with a `since=` cursor. Review comments are queried per active PR. The latest sync timestamp is stored in `sync_state`.
*   **Noise and Bot Filtering**: Comments posted by GitHub bots (type = `Bot` or name ending in `[bot]`) are excluded from search indexing by default. However, raw records are kept in `issue_items` with `is_bot=true` to display in thread replays via `shiori_read_issue`. Specific bots can be allowlisted using `SHIORI_INDEX_BOT_LOGINS` (e.g. `my-app[bot]`).
*   **Control Characters**: Filter out NUL (0x00) and control characters (0x00-0x1F except tab/newline) from GitHub payloads to prevent database insert errors (Issue #73).
*   **PR Diffs**: We do not index entire code diffs. We index descriptions and review comments. If a comment contains a `diff_hunk`, the hunk is appended to the comment text so that code tokens are discoverable via search.
*   **Closed States**: Issue state is mapped as either `open` or `closed` in v1.0. Determining if a PR has been merged requires extra API requests and is deferred.
*   **Allowlist Validation (Issue #63)**: `shiori_ingest` and CLI ingest command validate that requested repositories are present in `SHIORI_REPOS`. Non-listed repos are rejected.
*   **Rebuild Gate (Issue #63)**: Running `rebuild=True` via the MCP tool is disabled by default. It requires explicitly setting `SHIORI_ALLOW_REBUILD=true` in the environment. The CLI (`python -m shiori ingest --rebuild`) has no restrictions.

---

## 6. Ingestion Optimization (Bulk Load, Issue #72)

For initial indexing runs or `--rebuild` triggers, the server applies the following optimizations:

*   **Deferred Index Creation**: Database tables are created with standard B-tree indexes only (`migrate_light()`). High-overhead indexes — pgvector HNSW and pgroonga indexes — are dropped or deferred. Once all chunks are loaded, these heavy indexes are built in one pass (`create_heavy_indexes()`), bypassing row-by-row index updates.
*   **Batch Embeddings**: Chunks are collected in a `ChunkBuffer` and passed in batches (e.g. size 32 or 100) to `embed_passages` to prevent GPU/CPU thrashing.
*   **Bulk Database Insert**: Upserts are executed in chunks via `executemany` to minimize database transaction commits.

For incremental synchronization runs, the database retains all indexes (`migrate()`) and writes records sequentially.

---

## 7. Topology Recommendations (Public vs. Private)

### Standard Configuration (GitHub App + In-Process Polling)
*   Inject the GitHub App private key into the server.
*   Enable automated background sync via `SHIORI_SYNC_INTERVAL_SECONDS` (recommended: 10 seconds).
*   Concurrently running sync jobs are serialized using database-level advisory locks.

### Public-Only Repository Configuration
For public repos, a simplified credential setup is supported:
*   Configure a personal access token (`GITHUB_TOKEN`) with read-only scopes.
*   Enable sync polling (`SHIORI_SYNC_INTERVAL_SECONDS=30`).
*   GitHub App configs and daemon services are not required.

---

## 8. Open Issues
*   Fetching merge statuses to boost concluded discussions.
*   Transitioning REST queries to GraphQL to reduce API roundtrips.
*   Parallelizing initial PR change retrievals (Issue #72).

---

## 9. Related Documents
*   [Clone Management & Integration](12_clone_management_and_integration.md) — Directory rules, shallow clone policies, and Git checkout boundaries.
