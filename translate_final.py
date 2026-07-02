"""Translate remaining Japanese docstrings — brute force approach.

Strategy: for each file, for each remaining Japanese docstring,
we know its exact line range from AST analysis (start_line, end_line inclusive).
We read the raw text at that range, build an English replacement,
and use str.replace.
"""
import ast, re, os, textwrap

JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
HERE = os.path.dirname(__file__)

# ---------------------------------------------------------------------------
# English documentations keyed by (filename, start_line)
# These were manually translated from the Japanese originals.
# ---------------------------------------------------------------------------
ENG = {}

def eng(fn, start, text_content):
    """Register English translation for file `fn` starting at line `start`."""
    ENG[(fn, start)] = textwrap.dedent(text_content).strip('\n')

eng('chunking.py', 147, """
Find the closest semantic boundary not exceeding max_chars.

At the point this function is called, sentence splitting by _SENTENCE_END_RE
has already completed. Punctuation marks（。．！？！?.) are already used by
_SENTENCE_END_RE for splitting, so this function finds punctuation in the
following cases:
  1. Within text split by non-punctuation conditions (\\\\n{2,}), where
     punctuation remains inside the segment.
  2. When punctuation was consumed by the previous chunk and the current s
     has none → fall back to clause delimiters（、，,）

In practice, **clause-delimiter fallback** is the main role.

Priority order:
1. Sentence-ending marks（。．！？！?.: end of sentence
2. Clause delimiters（、，,: clause boundary
3. Hard-cut at max_chars if none found
""")

eng('db.py', 134, """Idempotent ALTER for existing DB. CREATE TABLE IF NOT EXISTS does not add new columns.

Called by both migrate_light and migrate.
""")
eng('db.py', 232, """Full schema creation (tables + all indexes). Used in incremental path (issue #72).

Bulk path uses migrate_light() + create_heavy_indexes() after loading.
""")
eng('db.py', 275, """Record a successful sync per repository and return the completion timestamp (DB's now()) (issue #22 / 33).

Executions skipped by advisory lock are NOT recorded (only successful syncs).
Timestamp uses DB now(): consistent across multiple paths and processes using a single DB clock.
""")
eng('db.py', 297, """Upsert PR change file map (issue #54). Deletes existing entries for the same PR first, then inserts new ones.
""")

eng('github_auth.py', 1, """
GitHub App installation token provider for fine-grained auth (detailed design/09).

Used by reindex.yml (self-hosted runner) and optionally for server-local ingestion.

Token generation:
  JWT (GitHub App private key PEM) → POST /app/installations/{id}/access_tokens
  → installation token (expires 1 hour). Cached until near expiry.

Compared to GITHUB_TOKEN (PAT): installation tokens are scoped to the specific
installation, not to a personal account, enabling least-privilege operation on
repos across organizations.

Retry: HTTP 401 refreshes the token once automatically.
""")

eng('github_sync.py', 1, """
Data fetching and sync (detailed design/01).

Decisions:
- docs: git clone/pull. Per-file content hash tracked in doc_files;
  only changed files are re-chunked and re-embedded.
  Deleted file indexes are removed.
- issue/PR: REST API cross-repo endpoints + `since` (updated_at cursor)
  for incremental sync:
    - GET /repos/{o}/{r}/issues            (includes PRs, body)
    - GET /repos/{o}/{r}/issues/comments    (issue/PR comments)
    - GET /repos/{o}/{r}/pulls/comments     (review comments, with path/line/diff_hunk)
- Bot comments (user.type == "Bot" or login ends with "[bot]")
  are excluded from indexing.
  Logins listed in SHIORI_INDEX_BOT_LOGINS are allowlisted (issue #25).
  Raw data stored in issue_items with is_bot=true (shown in read_issue).
- PR diffs are not indexed. Review comments include diff_hunk as context.
- code shares the same clone as sync_docs; only files with changed sha are
  re-indexed (issue #33).
- PR change file maps synced as metadata only; content (patch) delegated to
  GitHub MCP (issue #54).
- On initial/rebuild bulk path, ChunkBuffer batches embeddings across files,
  bulk-inserts chunks and coarsens commits (issue #72).
- _git_fetch_ref / _git_delete_ref: common primitives for fetching PR head
  files (issue #81); used without modifying the working tree.
Authentication via TokenProvider abstraction (detailed design/09).
Git injects tokens via http.extraHeader; API via httpx Auth hook per request.
""")

eng('github_sync.py', 49, """Buffered chunk inserter for bulk paths (initial/rebuild).
Batches across files, bulk-inserts chunks, coarsens commits (issue #72).
""")
eng('github_sync.py', 88, """Build authentication info as git extra args.
git -c http.extraHeader="Authorization: bearer <token>" ...

Independent of TokenProvider implementation (env var / GitHub CLI / OIDC).
""")
eng('github_sync.py', 98, """Fetch refs/pull/{N}/head and return its SHA.

Does NOT modify the working tree. The returned SHA can be used with
git cat-file etc., cleaned up via _git_delete_ref when done.

Retry:
    httpx network errors are retried up to _FETCH_RETRIES times.
""")
eng('github_sync.py', 116, """httpx Auth hook. Sets the Authorization header on each request.""")
eng('github_sync.py', 148, """Sync documentation (Markdown).

Internal steps:
  1. git clone (first time) / git pull (subsequent)
  2. Compute content hash per file and compare with doc_files sha
  3. Re-chunk and re-embed only changed files
  4. Delete doc_files rows for deleted files
  5. Mark completion via DocFilesRepository.sync_completed

Chunk embedding and doc_files sha update are in the same DB transaction.
Authentication independent of TokenProvider.
""")
eng('github_sync.py', 192, """Sync code files (issue #33).

Uses same clone as sync_docs; only files with changed sha are re-indexed.

Only runs when SHIORI_INDEX_CODE=true. Skipped when false/not set.
""")
eng('github_sync.py', 206, """Sync PR change file maps (issue #54).

Fetches changed file list for each PR from GitHub API and upserts into pr_changes.
Stored (metadata):
 - head_sha / path / status / additions / deletions / changes / blob_url
Not stored (content):
 - Full patch hunks → via GitHub MCP pull_request_read(method='get_diff')
 - Full PR head files → via shiori_read_pr_file or GitHub MCP
""")
eng('github_sync.py', 232, """Sync issue/PR threads.

Incremental sync via REST API cross-repo endpoints + `since` cursor:
  - GET /repos/{o}/{r}/issues
  - GET /repos/{o}/{r}/issues/comments
  - GET /repos/{o}/{r}/pulls/comments

First sync has no since (all items). Subsequent syncs use previous updated_at
as cursor. Bot comments are stored with is_bot flag but excluded from indexing.
""")
eng('github_sync.py', 126, 'Add chunk to buffer. Auto-flushes when batch_size is reached.')
eng('github_sync.py', 145, 'Flush buffer: batch embed → bulk insert → commit. Returns insertion count.')

eng('ingest.py', 1, """Ingest job (detailed design/01, 07).

Decision: Sync is on-demand execution.
    docker compose run --rm app python -m shiori ingest
For scheduled execution, trigger from host cron etc.
Authentication via build_token_provider shared across all repos (detailed design/09).

Process mutual exclusion (issue #6):
    Uses PostgreSQL advisory lock (pg_try_advisory_lock) to prevent concurrent
    execution with serve process auto-sync or MCP tool ingest.
    SYNC_LOCK_KEY matches mcp_server.py (0x5348494F = 'SHIO').

Freshness tracking (issue #22 / #33):
    Records completion timestamp and route to sync_runs per repository.
    Route from SHIORI_INGEST_ROUTE env var (default 'cli').
    reindex.yml (self-hosted runner) sets 'runner'.

Security (issue #63):
    Validates specified repo against SHIORI_REPOS (allowlist); rejects unlisted.
""")
eng('ingest.py', 26, """Determine if this is a bulk path: rebuild=True or chunks table doesn't exist (issue #72).""")

eng('mcp_server.py', 1, """
MCP server implementation. Entry point: main(), tool/fastmcp registration.

This module is long（~1100 lines）and structured as follows:
  1. Server setup & lifecycle (main, lifespan) — lines 1-90
  2. Internal helpers (paths, extensions, sync) — lines 90-310
  3. Tool definitions — lines 310-1100
    (each tool has its own span)

Each tool function is typically under 100 lines.
""")
eng('mcp_server.py', 123, 'Determine if this is a bulk path: rebuild=True or chunks table is empty/non-existent (issue #72).')
eng('mcp_server.py', 296, 'Execute sync and ingest tasks sequentially. Returns True if all succeeded, False otherwise.\nMultiple repos synced sequentially. wrap_ingest_error catches and logs exceptions.\n')
eng('mcp_server.py', 289, 'Check if filename has a document extension (case-insensitive).')
eng('mcp_server.py', 294, 'Check if filename has an excluded extension (case-insensitive).')
eng('mcp_server.py', 300, "Check if extension matches the given value (case-insensitive, with/without leading '.').")
eng('mcp_server.py', 306, 'Walk code files on disk for a cloned repository. Returns relative paths matching _CODE_EXTENSIONS.\nDotfiles and binary-extension files excluded.\n')
eng('mcp_server.py', 356, 'Semantic search MCP tool. Calls search.semantic_search internally with fallback for missing args.')
eng('mcp_server.py', 403, 'Keyword search MCP tool. Calls search.keyword_search internally.')
eng('mcp_server.py', 427, 'List indexed doc/code file paths, optionally filtered by path/source_type/extension.')
eng('mcp_server.py', 456, 'Read file content from the filesystem clone (not from the index).\n\nReads from local clone (main branch fixed). Does not require index.\nPR head files via read_pr_file or GitHub MCP.\n')
eng('mcp_server.py', 576, 'Fetch a single issue (internal helper). Raises ValueError if not indexed.')
eng('mcp_server.py', 596, 'Read an issue/PR thread (body + all comments) ordered by timestamp.\n\nFetches from the index. Raises ValueError if not indexed.\nexclude_noise_bots=True excludes bots outside SHIORI_INDEX_BOT_LOGINS (issue #44).\n')
eng('mcp_server.py', 654, 'PR change file map (metadata only). Returns pointer: head_sha + file list.\n\nStored: head_sha / path / status / additions / deletions / changes / blob_url\nNot stored: patch hunks (via GitHub MCP) and PR head files (via shiori_read_pr_file).\n')
eng('mcp_server.py', 709, 'Read PR head file content (transparently via git fetch).\n\nFetches refs/pull/{N}/head temporarily, reads file, cleans up.\nDoes NOT modify working tree.\n')
eng('mcp_server.py', 768, 'Ingest MCP tool. Syncs docs/issues/code for a repo (or all repos).\n\nWraps github_sync.py logic.\nrebuild=True: discard and rebuild (requires SHIORI_ALLOW_REBUILD=true, issue #63).\nrepo: specific repo or None for all.\n')
eng('mcp_server.py', 816, 'Detect index anomalies and return warning list (issue #31).')
eng('mcp_server.py', 834, 'Index freshness and health status. Returns per-repo sync info and warnings.\n\nUsed by LLM to check if index is up-to-date before searching.\n')

eng('search.py', 1, """
Search orchestration (hybrid: embedding + keyword). Detailed design/03.

This module is the external API for search.

Two-store design:
  - Embedding search (pgvector): cosine similarity, query-time only.
  - Keyword search (pgroonga): tokenized FTS, index at migration time.
    Falls back to pg_trgm.word_similarity when pgroonga unavailable.
    This is the primary reason for pg_trgm in docker-compose.yml.

Hybrid search via RRF (detailed design/03):
  - Merges embedding + keyword results with configurable weight.
  - Post-filter: language / source_type / repo / state / path_prefix.
  - Deduplication by (target_type, target_id).

Two execution modes:
  - Simple (1 chunk table): single kNN with pre-filtering.
    Conditions: ≤1 source_type, ≤1 repo, no language filter.
    Handled by select_target_ids_simple.
  - Complex (2 chunk table): kNN per (source_type, repo) → aggregator.
    Handled by select_target_ids_complex.
""")
eng('search.py', 25, 'Score candidates by embedding + keyword similarity and return ranked results (detailed design/03).\n\nEmbedding: cosine distance (1 - cosine).\nKeyword: pgroonga_score (0-1) / pg_trgm word_similarity (0-1) fallback.\nRRF weight: 0.5 embedding, 0.5 keyword.\n')
eng('search.py', 38, 'Sort by (source_type, source_id) and deduplicate by (target_type, target_id).\n')
eng('search.py', 56, 'Keyword search entry point. Hybrid search internally.\n\nFilters: source_type / language / state / repo / path_prefix / updated_after / prog_lang / top_k (default 20)\nsort_by/sort_order accepted for backward compat but ranking always relevance-based (issue #69).\n')
eng('search.py', 71, 'Semantic search entry point. Hybrid search internally.\n\nIdentical filtering to keyword_search.\n')

# ---------------------------------------------------------------------------
# Determine exact line ranges for each node
# ---------------------------------------------------------------------------
def get_node_ranges(fpath):
    """Return dict {(line_start, name): line_end} for each Japanese-docstring node."""
    with open(fpath, encoding='utf-8') as f:
        text = f.read()
    tree = ast.parse(text)
    lines = text.split('\n')
    fname = os.path.basename(fpath)
    ranges = {}
    for node in ast.walk(tree):
        if not isinstance(node, TYPES): continue
        name = getattr(node, 'name', '<module>')
        doc = ast.get_docstring(node)
        if not doc or not JP.search(doc): continue
        body = node.body if hasattr(node, 'body') else []
        for stmt in body:
            if not isinstance(stmt, ast.Expr): continue
            val = stmt.value
            if not isinstance(val, ast.Constant) or not isinstance(val.value, str): continue
            # Check it's a docstring by proximity check
            if stmt.lineno == node.lineno + 1 or stmt.lineno == node.lineno:
                ranges[(stmt.lineno, name)] = stmt.end_lineno
                break
    return ranges

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def translate():
    src = os.path.join(HERE, 'src', 'shiori')
    for fn in sorted(os.listdir(src)):
        if not fn.endswith('.py') or fn in ('config.py', 'embedding.py'): continue
        fpath = os.path.join(src, fn)
        print(f'=== {fn} ===')
        with open(fpath, encoding='utf-8') as f:
            text = f.read()
        original = text
        ranges = get_node_ranges(fpath)
        lines_list = text.split('\n')

        for (start_line, name), end_line in sorted(ranges.items()):
            key = (fn, start_line)
            if key not in ENG:
                print(f'  MISSING: {fn}:{name} start={start_line}')
                continue
            raw_lines = lines_list[start_line-1:end_line]
            raw_text = '\n'.join(raw_lines)
            if raw_text not in text:
                print(f'  {fn}:{name} raw text not found!')
                continue
            # Detect quote style
            q = raw_lines[0][:3]
            if q not in ('"""', "'''"):
                q = '"""'
            new_doc = ENG[key]
            if new_doc.endswith('\n'):
                replacement = q + '\n' + new_doc + q
            else:
                replacement = q + new_doc + q
            text = text.replace(raw_text, replacement, 1)
            print(f'  {fn}:{name} (line {start_line}) replaced')

        if text != original:
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(text)
            print(f'{fn}: SAVED')
        else:
            print(f'{fn}: no changes')

if __name__ == '__main__':
    translate()
