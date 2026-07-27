# Detailed Design: Data Ingestion & Sync

## 1. Purpose

Fetch documents and issue/PR threads from GitHub repositories, store them locally, and maintain up-to-date search indexes.

---

## 2. Ingestion Paths

- **Documents (Markdown, code)**: Pulled via `git clone` or `git pull`. Shallow clones (`depth=1`) are checked out on disk to extract `.md` files and source code.
- **Issues, PRs, and Comments**: Downloaded via GitHub REST API (`/issues`, `/issues/comments`, `/pulls/comments`).
- **PR Review Submissions**: Fetched via the reviews API (`/pulls/{number}/reviews`) to capture approval and changes-requested summaries and state (APPROVED, CHANGES_REQUESTED, COMMENTED) which are not part of standard comment lists (Issue #103).
- **Private Repositories**: Authenticated using personal access tokens (PATs) or GitHub Apps. Token is always minted host-side; the private key never enters the container.
- **Rate Limits**: GitHub API rate limits and pagination boundaries are handled gracefully.

---

## 3. Subcommand Split: fetch / index / run (Issue #305)

Ingestion is split into three subcommands at the CLI level (`python -m shiori ingest <subcommand>`):

| Subcommand | Phase | What it does | Side effects |
|---|---|---|---|
| `fetch` | Fetch | API calls (issues/comments/reviews) + `git pull` for doc/code clones. Populates `issue_items` and `doc_files` on disk. | No chunk/embed. No writes to `chunks` table. |
| `index` | Index | Reads `issue_items` / `doc_files` from disk+DB, chunks text, computes embeddings, writes to `chunks` table. | Idempotent — re-running produces the same chunks. |
| `run` (default) | Combined | `fetch` + `index` sequentially. | Backward-compatible: `shiori ingest` (no subcommand) behaves identically to `shiori ingest run`. |

This split lets operators run the fast fetch phase (API calls only) separately from the CPU/GPU-heavy embedding phase. For example, fetch can be run in parallel across repos while index runs sequentially.

The `--repo` flag limits operation to specific repos within `SHIORI_REPOS`:

```bash
python -m shiori ingest fetch --repo owner/dev-repo
```

---

## 4. Synchronization Strategy — Three Cursor Streams

The issue/PR sync uses **three independent cursor streams** stored in the `sync_state` table:

| Cursor name | API endpoint | Items covered |
|---|---|---|
| `issues` | `/repos/{owner}/{name}/issues` | Issue/PR body rows (`kind=issue` or `kind=pr`) |
| `issue_comments` | `/repos/{owner}/{name}/issues/comments` | Comments on issues and PRs (`kind=comment`) |
| `pr_review_comments` | `/repos/{owner}/{name}/pulls/comments` | Inline PR review comments with path/line/diff_hunk (`kind=pr_review_comment`) |

Each stream queries with `sort=updated&direction=asc` and a `since=<cursor>` parameter, so only items modified since the last sync are fetched.

### Page-level resumability

After **each page** is fetched, the cursor is advanced to `page[-1]["updated_at"]` and committed to the database. This means an interrupted sync resumes from the last fully-fetched page rather than retrying from the beginning.

### Cursor initialization

When a cursor is `NULL` (first fetch for a repo), it is either:
- Left as `NULL` for full-history fetch (dev repos), or
- Seeded to `backfill_since` for bounded backfill (ref repos, see §8).

### One-time dormant open body pass

After cursors are seeded, `_fetch_dormant_open_bodies()` runs a single `state=open` pass (no `since` filter) that upserts **body rows only** (`comment_id=0`). This catches issues/PRs whose `updated_at` predates the seed date and would otherwise be invisible to the `since`-filtered stream. See §8.

---

## 5. Per-Repo PostgreSQL Advisory Lock (Issues #307, #310)

Shiori uses **2-argument PostgreSQL advisory locks** for mutual exclusion:

```sql
pg_try_advisory_lock(0x5348494F, hashtext(repo))
```

- `classid = 0x5348494F` (ASCII for "SHIO"): fixed namespace shared across all sync operations.
- `objid = hashtext(repo)`: PostgreSQL hash of the `"owner/name"` string.

This design allows:
- **Different repos to sync concurrently** in separate processes (or containers) without blocking each other.
- **Same repo to be mutually exclusive** — a second process attempting to sync the same repo gets a skip (non-blocking `TRY` lock).
- **Parallel containers**: run `python -m shiori ingest fetch --repo owner/dev-repo` in one terminal while a multi-hour ref backfill runs in another. The old kill-and-restart workflow is replaced by per-repo isolation.

The lock is acquired per-repo in both `run_fetch` and `run_index` phases. `run_ingest` acquires it once per repo for the index phase (after releasing the fetch-phase lock).

### Lock scope notes

| Scope | Mechanism | Purpose |
|---|---|---|
| Intra-process (same repo, threads) | `threading.Lock` (`_sync_lock`) | Prevents concurrent sync of same repo within one process |
| Inter-process (same repo) | PG advisory lock (2-arg) | Prevents concurrent sync across containers/processes |
| Inter-process (different repos) | No lock (by design) | Different repos can sync concurrently |

---

## 6. Dev-first Target Ordering

Repos listed in `SHIORI_DEV_REPOS` are always processed **before** reference repos. Within each group, the original `SHIORI_REPOS` order is preserved (stable sort).

This ensures that:
- During `ingest run`, dev repos get indexed first even if a large ref repo dominates total runtime.
- `fetch` returns quickly for the repos an agent is actively working on.
- If the process is interrupted mid-sync, dev repos are likely already done.

Implementation: `_order_repos_dev_first(targets, dev_repos)` in `src/shiori/ingest.py`.

---

## 7. PR Review Submissions Fetch

PR review submissions (APPROVED, CHANGES_REQUESTED, COMMENTED) are fetched via `/repos/{owner}/{name}/pulls/{number}/reviews` (Issue #103). These are distinct from inline review comments (which come via the `/pulls/comments` stream).

### PR number collection

PR numbers are collected **during the body loop** (the `issues` endpoint stream, which includes PRs). This avoids a second pass over the `issues` endpoint after the cursor has advanced (fix for Issue #314 — the original design re-queried `issues` with the already-advanced cursor, missing most PRs).

```python
pr_numbers: list[int] = []
for page in issues_stream:
    for item in page:
        if "pull_request" in item:
            pr_numbers.append(item["number"])
    ...
# After body stream completes:
if not skip_reviews and pr_numbers:
    _fetch_pr_reviews_parallel(settings, repo, provider, pr_numbers)
```

### Parallel fetch

PR review fetching is parallelized with `ThreadPoolExecutor` (Issue #308, #311). PRs are split into chunks, one per worker; each worker shares one `httpx.Client` and one DB connection across its chunk. Max workers: 10 (`MAX_PR_REVIEW_WORKERS`). Per-PR failures are logged but do not abort the batch.

### Ref-repo skip

PR review submissions are **not fetched for reference repos** (see §10). The `skip_pr_reviews` parameter in `fetch_issues()` defaults to `True` for repos not in `SHIORI_DEV_REPOS`. This prevents an expensive multi-API-call per PR for repos that do not need review-level search.

---

## 8. Bounded Backfill (Issue #315)

Adding a large reference repo (e.g. `golang/go` with 50K+ issues) triggers a full-history fetch by default, which can take hours. Bounded backfill limits the initial fetch window.

### Cursor seeding

When a repo has no existing cursors (`sync_state` rows are `NULL`), and `backfill_since` is provided, all three cursors (`issues`, `issue_comments`, `pr_review_comments`) are seeded to that date:

```python
if backfill_since and get_cursor(conn, repo, "issues") is None:
    for kind in ("issues", "issue_comments", "pr_review_comments"):
        set_cursor(conn, repo, kind, backfill_since)
```

After seeding, the sync only fetches items updated **on or after** the seed date.

### Seed value resolution

`_resolve_backfill_since()` determines the effective seed per repo:

1. **CLI `--backfill-since YYYY-MM-DD`** — takes precedence for all target repos.
2. **`SHIORI_REF_BACKFILL_SINCE`** env var — applies only to repos **not** in `SHIORI_DEV_REPOS`.
3. **`None`** — dev repos get full history (no seed). Ref repos with no env var also get full history.

### One-time dormant open body pass

After cursors are seeded, a single `state=open` pass (no `since` filter) fetches body rows for issues/PRs that were created before the seed date and remain open. This ensures that even dormant open discussions (important reference material) are indexed.

This pass runs exactly once per repo, on the first fetch after seeding (`was_seeded = True` flag).

---

## 9. Incremental Issue Indexing via `indexed_at` (Issue #318)

**Status: design being implemented.**

Currently, `index_issues()` reads **all** `issue_items` for a repo and re-chunks everything. This is wasteful for repos with thousands of dormant items when only a handful were updated.

The planned improvement adds an `indexed_at` column to `issue_items`:

- On a successful index pass, each item's `indexed_at` is set to the current timestamp.
- Subsequent index runs query only items where `indexed_at IS NULL` or `updated_at > indexed_at`.
- Re-indexing an item first deletes its existing chunks by `chunk_key`, so edits replace cleanly. (Items deleted on GitHub are not pruned — unchanged from today.)
- `indexed_at` is only committed together with (or after) the item's chunks, so a killed index run resumes where it left off instead of redoing all embeddings.

This makes the index phase O(changed items) rather than O(total items).

---

## 10. Repo Roles: Dev vs Reference (Issue #303)

Repositories are divided into two roles based on how they are consumed:

### Development repos (`SHIORI_DEV_REPOS`)

| Aspect | Setting |
|---|---|
| **Code indexing** | Full tree-sitter chunking of source files |
| **PR review fetch** | Per-PR review submissions fetched in parallel |
| **Backfill** | Full history (no seed bound) |
| **Sync frequency** | On demand during active development (freshness matters) |
| **Example** | `masuda-masuo/shiori`, `masuda-masuo/sunaba` |

Development repos are the repos an AI agent actively clones, edits, tests, and creates PRs for. They are synced frequently so the index reflects current code structure, open PRs, and recent issues.

### Reference repos (in `SHIORI_REPOS` but not in `SHIORI_DEV_REPOS`)

| Aspect | Setting |
|---|---|
| **Code indexing** | Clone-only (no tree-sitter in DB). Grep-able via `shiori_grep`. |
| **PR review fetch** | Skipped (`skip_pr_reviews=True`) |
| **Backfill** | Bounded by `SHIORI_REF_BACKFILL_SINCE` or `--backfill-since` |
| **Sync frequency** | One-shot initial ingest, then frozen. Re-sync only when explicitly triggered. |
| **Example** | `astral-sh/ruff`, `golang/go`, `BurntSushi/ripgrep` |

Reference repos are external OSS projects or archived design docs that serve as **knowledge corpus** — the agent searches them for patterns, conventions, and failure-mode precedents but never modifies them.

### Distinction in the config

```env
# Both repos are in the target list
SHIORI_REPOS=masuda-masuo/shiori,astral-sh/ruff

# Only shiori gets code indexing + PR reviews
SHIORI_DEV_REPOS=masuda-masuo/shiori
```

See `docs/guides/setup.md` for onboarding instructions for each role.

---

## 11. Ingest Strategy (Issue #268)

### Current strategy: on-demand pull-type ingest

As of the architecture rewrite (Issues #305–#316), Shiori uses **on-demand (pull-type) ingest** — there is no periodic timer, no background sync loop, and no webhook listener:

| Trigger | Mechanism | When |
|---|---|---|
| Human operator | `./scripts/ingest.sh` (or direct CLI) | When freshness is needed |
| AI agent (MCP) | `shiori_ingest` tool | When the agent detects stale state |
| Startup | MCP server calls `_trigger_phase2` | Fire-and-forget on server start |

#### Why no periodic automation

Periodic timers (systemd timer, cron) were deliberately **not adopted**. The reasoning:

1. **Embedding is expensive**: Running the embedding model on every repo every hour burns CPU/GPU cycles. For reference repos (frozen after one-shot ingest), periodic sync is pure waste.
2. **Dev repos need freshness, not frequency**: A timer would sync even when no change has occurred. On-demand triggers let operators sync exactly when needed.
3. **Parallel containers replace kill-and-restart**: The old problem (cannot sync a small repo without waiting for a large backfill) is solved by per-repo locks (§5), not by automation.

The question of automation (timer, webhook) is explicitly **deferred** and remains tracked in Issue #268.

### Future options (tracked in #268)

| Option | Description |
|---|---|
| **A. systemd timer / cron** | `scripts/ingest.sh` scheduled hourly/daily. Simple but wasteful for frozen ref repos. |
| **B. Sync-before-search** | Wait for unfinished sync to complete before serving a search query. Risks high latency during large backfills. |
| **C. Role-based sync strategy** | Dev repos sync on every search trigger; ref repos never auto-sync. Partial implementation exists (dev-first ordering, ref-repo skip). |
| **D. GitHub Push webhook** | Receive push/issue events via webhook, ingest only the affected repo. Real-time but adds operational complexity. |

---

## 12. Bulk Path Optimization (Issue #72)

For initial indexing or `--rebuild`, the server applies optimizations:

- **Deferred Index Creation**: Database tables are created with standard B-tree indexes only (`migrate_light()`). High-overhead indexes (pgvector HNSW, pgroonga) are dropped or deferred. Once all chunks are loaded, these heavy indexes are built in one pass (`create_heavy_indexes()`).
- **Batch Embeddings**: Chunks are collected in a `ChunkBuffer` (flush threshold: 500 items) and embedded in batches to prevent GPU/CPU thrashing.
- **Bulk Database Insert**: Upserts are executed in chunks via `executemany`.

For incremental syncs, the database retains all indexes (`migrate()`) and writes records sequentially.

---

## 13. Metadata Mapping

All chunks are tagged with:
`repo`, `path`, `source_type` (`doc`, `issue`, `pr_review`, `code`), `language`, `state` (`open`/`closed`), `author`, `created_at`, `updated_at`, issue/PR number, heading path (for docs), and file/line numbers (for reviews).

---

## 14. Noise and Bot Filtering

Comments by GitHub bots (`type=Bot` or name ending in `[bot]`) are excluded from indexing by default but kept in `issue_items` with `is_bot=true` for thread replay via `shiori_read_issue`. Specific bots can be allowlisted via `SHIORI_INDEX_BOT_LOGINS`.

---

## 15. Allowlist Validation (Issue #63)

`--repo` arguments are validated against the `SHIORI_REPOS` allowlist. Non-listed repos are rejected with an error. This prevents typo-driven ingestion of unintended repositories.

---

## 16. Rebuild Gate (Issue #63)

`rebuild=True` via the MCP tool requires `SHIORI_ALLOW_REBUILD=true`. The CLI (`python -m shiori ingest --rebuild --repo owner/repo`) has no restriction.

---

## 17. Bulk Reindex Mode: Rebuild Chunks While Preserving Raw Data (Issue #352)

`shiori ingest reindex [--repo owner/name ...]` rebuilds the `chunks` table
(re-chunk + re-embed) without discarding fetched raw data or forcing a
re-fetch from GitHub. It exists because the only previous way to rebuild
`chunks` was `--rebuild`, which also truncates `issue_items`, `sync_state`,
`sync_runs`, and `repo_index_state` -- destroying fetch cursors and forcing a
full, rate-limited re-fetch even when the raw data on disk/DB was still
perfectly good.

### What is preserved vs rebuilt

| Table | reindex | `--rebuild` |
|---|---|---|
| `chunks` | Cleared (unscoped: `TRUNCATE`; scoped: `DELETE ... WHERE repo = ANY(...)`) | Truncated |
| `doc_files` | Cleared (path+sha cache only -- content is the on-disk clone) | Truncated |
| `issue_items` | Kept. Only `indexed_at` reset to `NULL` | Truncated |
| `sync_state` (fetch cursors) | Untouched | Truncated |
| `sync_runs`, `repo_index_state` | Untouched | Truncated |

Resetting `issue_items.indexed_at` matters because of §9: once `indexed_at`
tracking landed (issue #318), `index_issues()` only re-embeds rows where
`indexed_at IS NULL OR updated_at > indexed_at`. Deleting chunks alone would
re-index nothing for issues; clearing `doc_files` has the equivalent effect
for docs/code, since a matching `content_sha` is what makes `index_docs` /
`index_code` skip a file.

`--repo` scopes the clear to specific repos; omitting it reindexes every
repo in `SHIORI_REPOS` (unscoped `TRUNCATE`).

### Bulk/drain lifecycle: heavy-index absence is the marker

After clearing state, `reindex` drops the heavy indexes (HNSW, pgroonga) via
`schema.drop_heavy_indexes()` and runs the existing `index` phase. `_is_bulk_path()`
(§12) is extended to also return `True` whenever the HNSW index
(`chunks_embedding_hnsw`) is absent, not only when `chunks` is empty/missing
or `rebuild=True`. This makes "heavy indexes dropped" the persistent,
DB-derived marker of a drain in progress: no new state table is needed, and
every `ingest index` / `ingest run` invocation (CLI or MCP) stays on the
deferred-index bulk path for as long as the marker holds. Only a run that
completes successfully **and covered every configured repo**
(`_bulk_covers_all_repos()`) rebuilds the heavy indexes once, at the end
(`create_heavy_indexes()`). A scoped bulk run -- say, refreshing one dev repo
while a large ref drain is in progress -- defers them and logs that it did,
so it can neither trigger an hours-long index build over partial data nor
flip later invocations off the fast bulk path.

### Resuming a killed reindex

A reindex killed mid-drain (container restart, OOM, etc.) is resumed with
`shiori ingest index --all` -- there is no separate resume mechanism (the
plain `index` subcommand requires `--repo` by design, issue #338, so `--all`
is the explicit unscoped opt-in). Because
heavy-index absence is itself the bulk-path marker, `index` picks the drain
back up automatically; chunk inserts are `ON CONFLICT (chunk_key,
chunk_index) DO UPDATE` (`src/shiori/db.py`), so replaying already-indexed
items is a safe no-op.

### Avoiding heavy-index resurrection mid-drain

Two other code paths used to call the full `schema.migrate()` (which
includes `create_heavy_indexes()`) unconditionally, which would rebuild the
heavy indexes mid-drain the moment either ran:

- `run_fetch` -- it only ever writes `issue_items`/`sync_state` and never
  needs heavy indexes, so it now calls `schema.migrate_light()`.
- MCP server startup (`mcp_server.run()`) -- a server restart during a
  multi-hour drain used to trigger an hours-long HNSW rebuild at startup. It
  now also calls `migrate_light()`; search still works without the heavy
  indexes (just slower via sequential scan / no pgroonga), which is
  acceptable for the duration of a drain.

### Heavy-index build knobs and the /dev/shm trap

pgvector's HNSW index build supports `PARALLEL` workers, which allocate
roughly `maintenance_work_mem` of dynamic shared memory (DSM) per worker in
`/dev/shm`. Docker's default `/dev/shm` size is 64MB, which a parallel build
easily exceeds, failing with `could not resize shared memory segment ...
No space left on device`. Two settings on `create_heavy_indexes()` control
this:

- `SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS` (default `0`): applied via `SET
  max_parallel_maintenance_workers` before the `CREATE INDEX`. `0` forces a
  serial build, which uses only backend-private memory and never touches
  `/dev/shm` -- this is why it is the default rather than PostgreSQL's own
  default.
- `SHIORI_MAINTENANCE_WORK_MEM` (default unset): applied via `SET
  maintenance_work_mem` only when configured; otherwise the PostgreSQL
  server default is left alone.

Measured effect of the heavy indexes on ingest throughput: 1,141 chunks/min
with HNSW present vs 8,140 chunks/min without (7.1x) -- the reason the bulk
path defers index creation at all (§12).

### Caveat: heavy indexes are global

The HNSW/pgroonga indexes cover the whole `chunks` table, not a single repo.
During a drain -- including a scoped `reindex --repo X` -- search quality is
degraded for **every** repo, not just the one being reindexed, until the
drain completes and the heavy indexes are rebuilt.

---

## 18. Related Documents

- [Clone Management & Integration](12_clone_management_and_integration.md) — Directory rules, shallow clone policies, and Git checkout boundaries.
- [Setup Guide](../guides/setup.md) — Onboarding instructions for dev and reference setups.
