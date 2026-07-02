"""Generate a standalone Python script that translates all Japanese docstrings.

For each file, it reads the source, finds Japanese docstrings via AST,
replaces them with hardcoded English translations (using exact substring matching),
and writes the result.
"""
import ast, re, os, textwrap

JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

# Mapping from (filename, node_name) -> English docstring (without quotes, without outer indent)
T = {}

def _(key, doc):
    T[key] = textwrap.dedent(doc).lstrip('\n')

# ---- chunking.py ----
_('chunking.py', '_find_breakpoint', """
Find the closest semantic boundary not exceeding max_chars.

At the point this function is called, sentence splitting by _SENTENCE_END_RE has already completed.
Punctuation marks（。．！？！?.) are already used by _SENTENCE_END_RE for splitting, so
this function finds punctuation in the following cases:
  1. Within text split by non-punctuation conditions (_SENTENCE_END_RE's \\n{2,}),
     where punctuation remains inside the segment.
  2. When punctuation was consumed by the previous chunk and the current s has none
       → fall back to clause delimiters（、，,）

In practice, **clause-delimiter fallback** is the main role.

Priority order:
1. Sentence-ending marks（。．！？！?.: end of sentence
2. Clause delimiters（、，,: clause boundary
3. Hard-cut at max_chars if none found
""")

# ---- db.py ----
_('db.py', '_run_alter_statements', """Idempotent ALTER for existing DB. CREATE TABLE IF NOT EXISTS does not add new columns.

Called by both migrate_light and migrate.
""")
_('db.py', 'migrate', """Full schema creation (tables + all indexes). Used in incremental path (issue #72).

Bulk path uses migrate_light() + create_heavy_indexes() after loading.
""")
_('db.py', 'record_sync_run', """Record a successful sync per repository and return the completion timestamp (DB's now()) (issue #22 / #33).

Executions skipped by advisory lock are NOT recorded (only successful syncs).
Timestamp uses DB now(): consistent across multiple paths and processes using a single DB clock.
""")
_('db.py', 'upsert_pr_changes', """Upsert PR change file map (issue #54). Deletes existing entries for the same PR first, then inserts new ones.
""")

# ---- github_auth.py ----
_('github_auth.py', 'AppTokenProvider', """
GitHub App installation token provider for fine-grained auth (detailed design/09).

Used by reindex.yml (self-hosted runner) and optionally for server-local ingestion.

Token generation:
  JWT (GitHub App private key PEM) → POST /app/installations/{id}/access_tokens
  → installation token (expires 1 hour). Cached until near expiry.

Compared to GITHUB_TOKEN (PAT): installation tokens are scoped to the specific installation,
not to a personal account, enabling least-privilege operation on repos across organizations.

Retry: HTTP 401 refreshes the token once automatically.
""")

# ---- search.py ----
_('search.py', '<module>', """
Search orchestration (hybrid: embedding + keyword). Detailed design/03.

This module is the external API for search: callers go through semantic_search or keyword_search.

Two-store design:
  - Embedding search (pgvector): cosine similarity, always query-time.
  - Keyword search (pgroonga): tokenized full-text search, index created at migration time.
    Falls back to pg_trgm (pg_trgm.word_similarity) when pgroonga extension is unavailable.
    This fallback is the primary reason for the additional pg_trgm extension in docker-compose.yml.

Hybrid search: RRF (Reciprocal Rank Fusion) to merge result sets (detailed design/03).
  - Merges embedding and keyword results with configurable weight.
  - Post-filter: language / source_type / repo / state / path_prefix.
  - Deduplication by (target_type, target_id).

Two execution modes for performance:
  - Simple path (1 chunk table): single-kNN query with pre-filtering.
    Used when all the following conditions hold:
      * 1 source_type filter at most
      * 1 repo filter at most
      * No language filter
    select_target_ids_simple handles this case.
  - Complex path (2 chunk table): kNN per (source_type, repo) combination → aggregation.
    Used in all other cases.
    select_target_ids_complex handles this case.
""")
_('search.py', '_rank_candidates', """Score candidates by embedding + keyword similarity and return ranked results (detailed design/03).

Embedding similarity: cosine distance (1 - cosine).
Keyword similarity: pgroonga_score (0-1) when available, else pg_trgm word_similarity (0-1).
RRF weight: 0.5 for embedding, 0.5 for keyword.
""")
_('search.py', '_sort_hits', """Sort hits by primary key order and deduplicate by (target_type, target_id).
Same hit order is returned by select_target_ids_simple/complex; this sort is just for stable ordering.
""")
_('search.py', 'keyword_search', """
Keyword search entry point. Executed as a combined search internally.

Supports the following filters:
  source_type / language / state / repo / path_prefix / updated_after / prog_lang
  top_k (default 20)

sort_by / sort_order parameters are accepted for backward compatibility but
ranking is always relevance-based (issue #69).
  Primary sources (doc/code): scored order.
  Secondary sources (issue/pr_review): scored order + state/updated_at tie-break.
  Pure date-based sort is NOT performed; using the default (score) is recommended.

Returns a list of Hit objects.
""")
_('search.py', 'semantic_search', """
Semantic search entry point. Executed as a hybrid search internally.

Identical filtering to keyword_search.
""")

# ---- mcp_server.py ----
_('mcp_server.py', '<module>', """
MCP server implementation. Entry point: main(), tool/fastmcp registration.

This module is long（~1100 lines）and structured as follows:
  1. Server setup & lifecycle (main, lifespan) — lines 1-90
  2. Internal helpers — lines 90-310
  3. Tool definitions — lines 310-1100
    (each tool has its own span)

Each tool function is typically less than 100 lines.
""")
_('mcp_server.py', '_is_bulk_path', 'Determine if this is a bulk path: rebuild=True or chunks table is empty/non-existent (issue #72).')
_('mcp_server.py', '_do_sync', 'Execute sync and ingest tasks sequentially. Returns True if all succeeded, False otherwise.\n\nMultiple repos are synced sequentially (no parallel execution).\nwrap_ingest_error catches and logs exceptions instead of propagating.\n')
_('mcp_server.py', '_is_doc_file', 'Check if filename has a document extension (case-insensitive).')
_('mcp_server.py', '_is_excluded_file', 'Check if filename has an excluded extension (case-insensitive).')
_('mcp_server.py', '_match_extension', 'Check if extension matches the given value (case-insensitive, with/without leading \'.\').')
_('mcp_server.py', '_walk_code_files', 'Walk code files on disk for a cloned repository. Returns a list of relative paths matching extensions in _CODE_EXTENSIONS.\nHidden files (dotfiles) and binary-extension files are excluded.\n')
_('mcp_server.py', 'semantic_search', 'Semantic search MCP tool. Calls search.semantic_search internally with fallback for missing args.')
_('mcp_server.py', 'keyword_search', 'Keyword search MCP tool. Calls search.keyword_search internally.')
_('mcp_server.py', 'list_tree', 'List indexed doc/code file paths, optionally filtered by path/source_type/extension.')
_('mcp_server.py', 'read_file', """
Read file content from the filesystem clone (not from the index).

Reads from the local clone (main branch fixed). Does not require an index.
PR head files should be read via read_pr_file or GitHub MCP.

Parameters match the MCP tool definition.
""")
_('mcp_server.py', '_read_issue_single', 'Fetch a single issue (internal helper). Raises ValueError if not indexed.')
_('mcp_server.py', 'read_issue', """
Read an issue/PR thread (body + all comments) ordered by timestamp.

Fetches from the index. Raises ValueError if not indexed.
exclude_noise_bots=True excludes bot comments outside SHIORI_INDEX_BOT_LOGINS (issue #44).
""")
_('mcp_server.py', 'pr_changes', """
PR change file map (metadata only). Returns a pointer: head_sha + file list.

Stored (metadata):
  - head_sha / path / status / additions / deletions / changes / blob_url
Not stored (content):
  - patch hunks → fetch via GitHub MCP pull_request_read(method='get_diff')
  - PR head files → fetch via shiori_read_pr_file or GitHub MCP
""")
_('mcp_server.py', 'read_pr_file', 'Read PR head file content (transparently via git fetch).\n\nFetches refs/pull/{N}/head temporarily, reads the file, and cleans up.\nDoes NOT modify the working tree.\n')
_('mcp_server.py', 'ingest', """
Ingest MCP tool. Syncs docs/issues/code for a repo (or all repos).

Wraps sync logic from github_sync.py.

rebuild=True: discards the existing index and rebuilds from scratch (issue #63).
  Requires SHIORI_ALLOW_REBUILD=true environment variable. Disabled by default in MCP tools (issue #63).
  Also allowed when chunks table is empty/non-existent.

repo: specific repo to sync, or None for all.
""")
_('mcp_server.py', '_build_warnings', 'Detect index anomalies and return warning list (issue #31).')
_('mcp_server.py', 'status', 'Index freshness and health status. Returns per-repo sync info and warnings.\n\nUsed by the LLM to check if the index is up-to-date before searching.\n')

# ---- github_sync.py ----
_('github_sync.py', '<module>', """
Data fetching and sync (detailed design/01).

Decisions:
- docs: git clone/pull. Per-file content hash tracked in doc_files;
  only changed files are re-chunked and re-embedded. Deleted file indexes are removed.
- issue/PR: REST API cross-repo endpoints + `since` (updated_at cursor) for incremental sync:
    - GET /repos/{o}/{r}/issues            (includes PRs, body)
    - GET /repos/{o}/{r}/issues/comments    (issue/PR comments)
    - GET /repos/{o}/{r}/pulls/comments     (review comments, with path/line/diff_hunk)
- Bot comments (user.type == "Bot" or login ends with "[bot]") are excluded from indexing.
  Logins listed in SHIORI_INDEX_BOT_LOGINS are allowlisted for indexing (issue #25).
  Raw data is stored in issue_items with is_bot=true (shown in read_issue).
- PR diffs themselves are not indexed. Review comments include diff_hunk as context.
- code shares the same clone as sync_docs; only files with changed sha are re-indexed (issue #33).
- PR change file maps are synced and stored as metadata only; content (patch) is delegated to GitHub MCP (issue #54).
- On initial/rebuild bulk path, ChunkBuffer batches embeddings across files,
  bulk-inserts chunks and coarsens commits (issue #72).
- _git_fetch_ref / _git_delete_ref are common primitives for fetching PR head files (issue #81).
  Used to read files from arbitrary refs without modifying the working tree.
Authentication goes through the TokenProvider abstraction (detailed design/09).
Git injects tokens via http.extraHeader; the API injects tokens per-request via httpx Auth hook.
""")
_('github_sync.py', 'ChunkBuffer', 'Buffered chunk inserter for bulk paths (initial/rebuild). Batches across files, bulk-inserts chunks, and coarsens commits (issue #72).')
_('github_sync.py', '_auth_args', 'Build authentication info as git extra args.\ngit -c http.extraHeader="Authorization: bearer <token>" ...\n\nIndependent of the TokenProvider implementation (env var / GitHub CLI / OIDC).\n')
_('github_sync.py', '_git_fetch_ref', 'Fetch refs/pull/{N}/head and return its SHA.\n\nDoes NOT modify the working tree. The returned SHA can be used with\ngit cat-file etc., and should be cleaned up via _git_delete_ref when done.\n\nRetry:\n    httpx network errors are retried up to _FETCH_RETRIES times.\n')
_('github_sync.py', '_GitHubAuth', 'httpx Auth hook. Sets the Authorization header on each request.')
_('github_sync.py', 'sync_docs', 'Sync documentation (Markdown).\n\nInternal steps:\n  1. git clone (first time) / git pull (subsequent)\n  2. Compute content hash for each file and compare with doc_files sha\n  3. Re-chunk and re-embed only changed files\n  4. Delete doc_files rows for deleted files\n  5. Mark completion via DocFilesRepository.sync_completed\n\nChunk embedding and doc_files sha update for multiple files are in the same DB transaction.\nAuthentication independent of TokenProvider.\n')
_('github_sync.py', 'sync_code', 'Sync code files (issue #33).\n\nUses the same clone as sync_docs; only files with changed sha are re-indexed.\n\nOnly runs when SHIORI_INDEX_CODE=true. Skipped when false/not set.\n')
_('github_sync.py', '_sync_pr_changes', 'Sync PR change file maps (issue #54).\n\nFetches the list of changed files for each PR from the GitHub API and upserts into pr_changes table.\nStored (metadata):\n - head_sha / path / status / additions / deletions / changes / blob_url\nNot stored (content):\n - Full patch hunks → fetch via GitHub MCP pull_request_read(method=\'get_diff\')\n - Full PR head files → fetch via shiori_read_pr_file or GitHub MCP\n')
_('github_sync.py', 'sync_issues', 'Sync issue/PR threads.\n\nIncremental sync via REST API cross-repo endpoints + `since` cursor:\n  - GET /repos/{o}/{r}/issues\n  - GET /repos/{o}/{r}/issues/comments\n  - GET /repos/{o}/{r}/pulls/comments\n\nFirst sync has no since (all items). Subsequent syncs use the previous updated_at as cursor.\nBot comments are stored with is_bot flag but excluded from indexing.\n')
_('github_sync.py', 'add', 'Add chunk to buffer. Auto-flushes when batch_size is reached.')
_('github_sync.py', 'flush', 'Flush buffer: batch embed → bulk insert → commit. Returns insertion count.')

# ---- ingest.py ----
_('ingest.py', '<module>', 'Ingest job (detailed design/01, 07).\n\nDecision: Sync is on-demand execution.\n    docker compose run --rm app python -m shiori ingest\nFor scheduled execution, trigger the same command from host cron etc.\nAuthentication is built via build_token_provider and shared across all repos (detailed design/09).\n\nProcess mutual exclusion (issue #6):\n    Uses PostgreSQL advisory lock (pg_try_advisory_lock) to prevent concurrent execution\n    with the serve process auto-sync or MCP tool ingest.\n    SYNC_LOCK_KEY matches mcp_server.py (0x5348494F = \'SHIO\').\n\nFreshness tracking (issue #22 / #33):\n    Records completion timestamp and route to sync_runs per repository.\n    Route is from environment variable SHIORI_INGEST_ROUTE (default \'cli\').\n    reindex.yml (self-hosted runner) sets \'runner\' to identify the execution route.\n\nSecurity (issue #63):\n    Validates specified repo against SHIORI_REPOS (allowlist); rejects unlisted repos.\n')
_('ingest.py', '_is_bulk_path', 'Determine if this is a bulk path: rebuild=True or chunks table does not exist (issue #72).')

# ---- Main translation logic ----
def translate_file(fpath):
    with open(fpath, encoding='utf-8') as f:
        text = f.read()
    original = text
    tree = ast.parse(text)
    lines = text.split('\n')
    fname = os.path.basename(fpath)

    for node in ast.walk(tree):
        if not isinstance(node, TYPES): continue
        name = getattr(node, 'name', '<module>')
        doc_raw = ast.get_docstring(node, clean=False)
        if doc_raw is None: continue
        # Check if raw docstring (cleaned version) has Japanese
        doc_clean = ast.get_docstring(node)
        if not doc_clean or not JP.search(doc_clean): continue
        key = (fname, name)
        if key not in T:
            print(f'  MISSING: {key}')
            continue
        new_doc = T[key]
        body = node.body if hasattr(node, 'body') else []
        found = False
        for stmt in body:
            if not isinstance(stmt, ast.Expr): continue
            val = stmt.value
            if not isinstance(val, ast.Constant) or not isinstance(val.value, str): continue
            # Compare clean docstring against cleaned version
            val_clean = ast.get_docstring(node)
            if val_clean and JP.search(val_clean) and doc_clean.strip() == val_clean.strip():
                raw_lines = lines[stmt.lineno-1:stmt.end_lineno]
                raw_text = '\n'.join(raw_lines)
                # Detect quote style from first line
                first = raw_lines[0]
                q = first[:3] if first[:3] in ('"""', "'''") else '"""'
                # Build replacement
                if new_doc.endswith('\n'):
                    replacement = q + '\n' + new_doc + q
                else:
                    replacement = q + new_doc + q
                if raw_text in text:
                    text = text.replace(raw_text, replacement, 1)
                    print(f'  {fname}:{name} OK')
                    found = True
                else:
                    print(f'  {fname}:{name} raw not in text')
                break
        if not found:
            print(f'  {fname}:{name} could not locate')

    if text != original:
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f'{fname}: SAVED')
    else:
        print(f'{fname}: no changes')

if __name__ == '__main__':
    src = os.path.join(os.path.dirname(__file__), 'src', 'shiori')
    for fn in sorted(os.listdir(src)):
        if not fn.endswith('.py'): continue
        if fn in ('config.py', 'embedding.py'): continue
        fpath = os.path.join(src, fn)
        print(f'=== {fn} ===')
        translate_file(fpath)
