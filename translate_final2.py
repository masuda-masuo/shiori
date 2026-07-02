"""Translate remaining Japanese docstrings — brute force approach (v2).

Strategy: for each file, for each remaining Japanese docstring,
we know its exact line range from AST analysis (start_line, end_line inclusive).
We read the raw text at that range, build an English replacement,
and use str.replace.
"""
import ast, re, os, textwrap

JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
HERE = os.path.dirname(__file__)

ENG = {}

def eng(fn, start, text_content):
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

eng('db.py', 134, 'Idempotent ALTER for existing DB. CREATE TABLE IF NOT EXISTS does not add new columns.\n\nCalled by both migrate_light and migrate.')
eng('db.py', 232, 'Full schema creation (tables + all indexes). Used in incremental path (issue #72).\n\nBulk path uses migrate_light() + create_heavy_indexes() after loading.')
eng('db.py', 275, "Record a successful sync per repository and return the completion timestamp (DB's now()) (issue #22 / #33).\n\nExecutions skipped by advisory lock are NOT recorded (only successful syncs).\nTimestamp uses DB now(): consistent across multiple paths and processes.")
eng('db.py', 297, 'Upsert PR change file map (issue #54). Deletes existing entries for the same PR first, then inserts new ones.')

eng('github_auth.py', 51, 'Issues and caches GitHub App installation access tokens.\n\nget_token() re-issues if not yet obtained or within REFRESH_BEFORE seconds of expiry.\nEnsures tokens do not expire mid-operation even during long ingests (CPU embedding can exceed 1 hour).')

eng('github_sync.py', 1, """
Data fetching and sync (detailed design/01).

Decisions:
- docs: git clone/pull. Per-file content hash tracked in doc_files;
  only changed files are re-chunked and re-embedded. Deleted file indexes removed.
- issue/PR: REST API cross-repo endpoints + `since` (updated_at cursor) incremental sync:
    - GET /repos/{o}/{r}/issues            (includes PRs, body)
    - GET /repos/{o}/{r}/issues/comments    (issue/PR comments)
    - GET /repos/{o}/{r}/pulls/comments     (review comments, path/line/diff_hunk)
- Bot comments (user.type=="Bot" or login ends with "[bot]") excluded from index.
  Logins in SHIORI_INDEX_BOT_LOGINS allowlisted (issue #25). Raw data in issue_items is_bot=true.
- PR diffs not indexed. Review comments include diff_hunk as context.
- code shares same clone as sync_docs; sha-delta re-indexes only changed files (issue #33).
- PR change file maps synced as metadata only; content (patch) delegated to GitHub MCP (issue #54).
- Initial/rebuild bulk path: ChunkBuffer batches embeddings across files,
  bulk-inserts chunks and coarsens commits (issue #72).
- _git_fetch_ref / _git_delete_ref: common primitives for PR head files (issue #81).
  Read files from arbitrary refs without modifying working tree.
Authentication via TokenProvider abstraction (detailed design/09).
Git injects tokens via http.extraHeader; API via httpx Auth hook per request.
""")
eng('github_sync.py', 49, 'Buffered chunk inserter for bulk paths (initial/rebuild).\nBatches across files, bulk-inserts chunks, coarsens commits (issue #72).')
eng('github_sync.py', 88, 'Build authentication info as git extra args.\ngit -c http.extraHeader="Authorization: bearer <token>" ...\n\nIndependent of TokenProvider implementation (env var / GitHub CLI / OIDC).')
eng('github_sync.py', 98, 'Fetch refs/pull/{N}/head and return its SHA.\n\nDoes NOT modify working tree. Use with git cat-file, clean up via _git_delete_ref.\n\nRetry: httpx network errors retried up to _FETCH_RETRIES times.')
eng('github_sync.py', 116, 'httpx Auth hook. Sets the Authorization header on each request.')
eng('github_sync.py', 148, 'Sync documentation (Markdown).\n\nInternal steps:\n  1. git clone (first) / git pull (subsequent)\n  2. Compute content hash per file, compare with doc_files sha\n  3. Re-chunk and re-embed only changed files\n  4. Delete doc_files rows for deleted files\n  5. Mark completion via DocFilesRepository.sync_completed\n\nChunk embedding and sha update in same DB transaction.\nAuthentication independent of TokenProvider.')
eng('github_sync.py', 192, 'Sync code files (issue #33).\n\nUses same clone as sync_docs; sha-delta re-indexes only changed files.\n\nOnly runs when SHIORI_INDEX_CODE=true. Skipped when false/not set.')
eng('github_sync.py', 206, 'Sync PR change file maps (issue #54).\n\nFetches changed files per PR from GitHub API, upserts into pr_changes.\nStored: head_sha / path / status / additions / deletions / changes / blob_url\nNot stored: patch hunks (via GitHub MCP) and PR head files (via shiori_read_pr_file).')
eng('github_sync.py', 232, 'Sync issue/PR threads.\n\nIncremental sync via REST API cross-repo endpoints + `since` cursor.\nFirst sync: no since (all items). Subsequent: previous updated_at as cursor.\nBot comments stored with is_bot flag, excluded from indexing.')
eng('github_sync.py', 126, 'Add chunk to buffer. Auto-flushes when batch_size is reached.')
eng('github_sync.py', 145, 'Flush buffer: batch embed → bulk insert → commit. Returns insertion count.')

eng('ingest.py', 1, 'Ingest job (detailed design/01, 07).\n\nDecision: Sync is on-demand execution.\n    docker compose run --rm app python -m shiori ingest\nFor scheduled execution, trigger the same command from host cron etc.\nAuthentication via build_token_provider shared across all repos (detailed design/09).\n\nProcess mutual exclusion (issue #6):\n    PostgreSQL advisory lock (pg_try_advisory_lock) prevents concurrent execution\n    with serve process auto-sync or MCP tool ingest.\n    SYNC_LOCK_KEY matches mcp_server.py (0x5348494F = \'SHIO\').\n\nFreshness tracking (issue #22 / #33):\n    Records completion timestamp and route to sync_runs per repository.\n    Route from SHIORI_INGEST_ROUTE env var (default \'cli\').\n    reindex.yml (self-hosted runner) sets \'runner\'.\n\nSecurity (issue #63):\n    Validates specified repo against SHIORI_REPOS (allowlist); rejects unlisted.')
eng('ingest.py', 26, "Determine if this is a bulk path: rebuild=True or chunks table doesn't exist (issue #72).")

eng('mcp_server.py', 1, """
MCP server implementation. Entry point: main(), tool/fastmcp registration.

This module is long (~1100 lines) and structured as follows:
  1. Server setup & lifecycle (main, lifespan) — lines 1-90
  2. Internal helpers (paths, extensions, sync) — lines 90-310
  3. Tool definitions — lines 310-1100 (each tool has its own span)

Each tool function is typically under 100 lines.
""")
eng('mcp_server.py', 123, 'Determine if this is a bulk path: rebuild=True or chunks table is empty/non-existent (issue #72).')
eng('mcp_server.py', 296, 'Execute sync and ingest tasks sequentially. Returns True if all succeeded.\nMultiple repos synced sequentially. wrap_ingest_error catches and logs exceptions.')
eng('mcp_server.py', 289, 'Check if filename has a document extension (case-insensitive).')
eng('mcp_server.py', 294, 'Check if filename has an excluded extension (case-insensitive).')
eng('mcp_server.py', 300, "Check if extension matches the given value (case-insensitive, with/without leading '.').")
eng('mcp_server.py', 306, 'Walk code files on disk for a cloned repository. Returns relative paths matching _CODE_EXTENSIONS.\nDotfiles and binary-extension files excluded.')
eng('mcp_server.py', 356, 'Semantic search MCP tool. Calls search.semantic_search internally with fallback for missing args.')
eng('mcp_server.py', 403, 'Keyword search MCP tool. Calls search.keyword_search internally.')
eng('mcp_server.py', 427, 'List indexed doc/code file paths, optionally filtered by path/source_type/extension.')
eng('mcp_server.py', 456, 'Read file content from the filesystem clone (not from the index).\n\nReads from local clone (main branch fixed). No index required.\nPR head files: use read_pr_file or GitHub MCP.')
eng('mcp_server.py', 576, 'Fetch a single issue (internal helper). Raises ValueError if not indexed.')
eng('mcp_server.py', 596, 'Read an issue/PR thread (body + all comments) ordered by timestamp.\n\nFetches from the index. Raises ValueError if not indexed.\nexclude_noise_bots=True excludes bots outside SHIORI_INDEX_BOT_LOGINS (issue #44).')
eng('mcp_server.py', 654, 'PR change file map (metadata only). Returns pointer: head_sha + file list.\n\nStored: head_sha / path / status / additions / deletions / changes / blob_url\nNot stored: patch hunks (via GitHub MCP) and PR head files (via shiori_read_pr_file).')
eng('mcp_server.py', 709, 'Read PR head file content (transparently via git fetch).\n\nFetches refs/pull/{N}/head temporarily, reads file, cleans up. Does NOT modify working tree.')
eng('mcp_server.py', 768, 'Ingest MCP tool. Syncs docs/issues/code for a repo (or all repos).\n\nWraps github_sync.py logic.\nrebuild=True: discard and rebuild index (requires SHIORI_ALLOW_REBUILD=true, issue #63).')
eng('mcp_server.py', 816, 'Detect index anomalies and return warning list (issue #31).')
eng('mcp_server.py', 834, 'Index freshness and health status. Returns per-repo sync info and warnings.\n\nUsed by LLM to check if index is up-to-date before searching.')

eng('search.py', 1, """
Search orchestration (hybrid: embedding + keyword). Detailed design/03.

This module is the external API for search.

Two-store design:
  - Embedding search (pgvector): cosine similarity, query-time only.
  - Keyword search (pgroonga): tokenized FTS, index at migration time.
    Falls back to pg_trgm.word_similarity when pgroonga unavailable.
    This is why docker-compose.yml includes pg_trgm.

Hybrid search via RRF (detailed design/03):
  - Merges embedding + keyword results with configurable weight.
  - Post-filter: language / source_type / repo / state / path_prefix.
  - Deduplication by (target_type, target_id).

Two execution modes:
  - Simple (1 chunk table): single kNN with pre-filtering.
    Conditions: <=1 source_type, <=1 repo, no language filter.
    select_target_ids_simple handles this.
  - Complex (2 chunk table): kNN per (source_type, repo) → aggregator.
    select_target_ids_complex handles this.
""")
eng('search.py', 25, 'Score candidates by embedding + keyword similarity and return ranked results (detailed design/03).\n\nEmbedding: cosine distance (1 - cosine).\nKeyword: pgroonga_score (0-1) / pg_trgm word_similarity (0-1).\nRRF weight: 0.5 embedding, 0.5 keyword.')
eng('search.py', 38, 'Sort by (source_type, source_id) and deduplicate by (target_type, target_id).')
eng('search.py', 56, 'Keyword search entry point. Hybrid search internally.\n\nFilters: source_type / language / state / repo / path_prefix / updated_after / prog_lang / top_k (default 20)\nsort_by/sort_order accepted for backward compat; ranking always relevance-based (issue #69).')
eng('search.py', 71, 'Semantic search entry point. Hybrid search internally.\n\nIdentical filtering to keyword_search.')

# ---------------------------------------------------------------------------
# Get line ranges for each Japanese-docstring node
# ---------------------------------------------------------------------------
def get_node_ranges(fpath):
    with open(fpath, encoding='utf-8') as f:
        text = f.read()
    tree = ast.parse(text)
    lines_list = text.split('\n')
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
            # verify content matches
            val_clean = val.value.strip() if val.value else ''
            if doc.strip() in val_clean or val_clean in doc.strip() or doc.strip() == val_clean:
                ranges[(stmt.lineno, name)] = stmt.end_lineno
                break
    return ranges

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
                print(f'  MISSING: {fn}:{name} line={start_line}')
                continue
            raw_lines = lines_list[start_line-1:end_line]
            raw_text = '\n'.join(raw_lines)
            if raw_text not in text:
                print(f'  {fn}:{name} raw text NOT FOUND in source!')
                continue
            q = raw_lines[0][:3]
            if q not in ('"""', "'''"):
                q = '"""'
            new_doc = ENG[key]
            if new_doc.endswith('\n'):
                replacement = q + '\n' + new_doc + q
            else:
                replacement = q + new_doc + q
            text = text.replace(raw_text, replacement, 1)
            print(f'  {fn}:{name} (line {start_line}) OK')

        if text != original:
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(text)
            print(f'{fn}: SAVED')
        else:
            print(f'{fn}: no changes')

if __name__ == '__main__':
    translate()
