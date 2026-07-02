"""Translate remaining Japanese docstrings — brute force approach (v3).

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
    ENG[(fn, start)] = text_content

# ---------------------------------------------------------------------------
# English translations — keep indentation INDEPENDENT (4-space indent added later)
# ---------------------------------------------------------------------------

eng('chunking.py', 147, textwrap.dedent("""\
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
"""))

eng('db.py', 134, "Idempotent ALTER for existing DB. CREATE TABLE IF NOT EXISTS does not add new columns.\n\nCalled by both migrate_light and migrate.\n")
eng('db.py', 232, "Full schema creation (tables + all indexes). Used in incremental path (issue #72).\n\nBulk path uses migrate_light() + create_heavy_indexes() after loading.\n")
eng('db.py', 275, "Record a successful sync per repository and return the completion timestamp (DB's now()) (issue #22 / #33).\n\nExecutions skipped by advisory lock are NOT recorded (only successful syncs).\nTimestamp uses DB now(): consistent across multiple paths and processes using a single DB clock.\n")
eng('db.py', 297, "Upsert PR change file map (issue #54). Deletes existing entries for the same PR first, then inserts new ones.\n")

eng('github_auth.py', 51, textwrap.dedent("""\
Issues and caches GitHub App installation access tokens.

get_token() re-issues if not yet obtained or within REFRESH_BEFORE seconds of expiry.
Ensures tokens do not expire mid-operation even during long ingests (CPU embedding can exceed 1 hour).
"""))

eng('github_sync.py', 1, textwrap.dedent("""\
Data fetching and sync (detailed design/01).

Decisions:
- docs: git clone/pull. Per-file content hash tracked in doc_files;
  only changed files are re-chunked and re-embedded. Deleted file indexes removed.
- issue/PR: REST API cross-repo endpoints + `since` (updated_at cursor) incremental sync:
    - GET /repos/{o}/{r}/issues            (includes PRs, body)
    - GET /repos/{o}/{r}/issues/comments    (issue/PR comments)
    - GET /repos/{o}/{r}/pulls/comments     (review comments, path/line/diff_hunk)
- Bot comments (user.type=="Bot" or login ends with "[bot]") excluded from index.
  Logins in SHIORI_INDEX_BOT_LOGINS allowlisted (issue #25).
  Raw data in issue_items is_bot=true.
- PR diffs not indexed. Review comments include diff_hunk as context.
- code shares same clone as sync_docs; sha-delta re-indexes only changed files (issue #33).
- PR change file maps synced as metadata only; content (patch) delegated to GitHub MCP (issue #54).
- Initial/rebuild bulk path: ChunkBuffer batches embeddings across files,
  bulk-inserts chunks and coarsens commits (issue #72).
- _git_fetch_ref / _git_delete_ref: common primitives for PR head files (issue #81).
  Read files from arbitrary refs without modifying working tree.
Authentication via TokenProvider abstraction (detailed design/09).
Git injects tokens via http.extraHeader; API via httpx Auth hook per request.
"""))
eng('github_sync.py', 49, "Buffered chunk inserter for bulk paths (initial/rebuild).\nBatches across files, bulk-inserts chunks, coarsens commits (issue #72).\n")
eng('github_sync.py', 88, "Build authentication info as git extra args.\ngit -c http.extraHeader=\"Authorization: bearer <token>\" ...\n\nIndependent of TokenProvider implementation (env var / GitHub CLI / OIDC).\n")
eng('github_sync.py', 98, "Fetch refs/pull/{N}/head and return its SHA.\n\nDoes NOT modify working tree. Use with git cat-file, clean up via _git_delete_ref.\n\nRetry: httpx network errors retried up to _FETCH_RETRIES times.\n")
eng('github_sync.py', 116, "httpx Auth hook. Sets the Authorization header on each request.")
eng('github_sync.py', 148, textwrap.dedent("""\
Sync documentation (Markdown).

Internal steps:
  1. git clone (first) / git pull (subsequent)
  2. Compute content hash per file, compare with doc_files sha
  3. Re-chunk and re-embed only changed files
  4. Delete doc_files rows for deleted files
  5. Mark completion via DocFilesRepository.sync_completed

Chunk embedding and sha update in same DB transaction.
Authentication independent of TokenProvider.
"""))
eng('github_sync.py', 192, "Sync code files (issue #33).\n\nUses same clone as sync_docs; sha-delta re-indexes only changed files.\n\nOnly runs when SHIORI_INDEX_CODE=true. Skipped when false/not set.\n")
eng('github_sync.py', 206, textwrap.dedent("""\
Sync PR change file maps (issue #54).

Fetches changed files per PR from GitHub API, upserts into pr_changes.
Stored: head_sha / path / status / additions / deletions / changes / blob_url
Not stored: patch hunks (via GitHub MCP) and PR head files (via shiori_read_pr_file).
"""))
eng('github_sync.py', 232, textwrap.dedent("""\
Sync issue/PR threads.

Incremental sync via REST API cross-repo endpoints + `since` cursor.
First sync: no since (all items). Subsequent: previous updated_at as cursor.
Bot comments stored with is_bot flag, excluded from indexing.
"""))
eng('github_sync.py', 126, "Add chunk to buffer. Auto-flushes when batch_size is reached.")
eng('github_sync.py', 145, "Flush buffer: batch embed → bulk insert → commit. Returns insertion count.")

eng('ingest.py', 1, textwrap.dedent("""\
Ingest job (detailed design/01, 07).

Decision: Sync is on-demand execution.
    docker compose run --rm app python -m shiori ingest
For scheduled execution, trigger the same command from host cron etc.
Authentication via build_token_provider shared across all repos (detailed design/09).

Process mutual exclusion (issue #6):
    Uses PostgreSQL advisory lock (pg_try_advisory_lock) to prevent concurrent execution
    with the serve process auto-sync or MCP tool ingest.
    SYNC_LOCK_KEY matches mcp_server.py (0x5348494F = 'SHIO').

Freshness tracking (issue #22 / #33):
    Records completion timestamp and route to sync_runs per repository.
    Route is from SHIORI_INGEST_ROUTE env var (default 'cli').
    reindex.yml (self-hosted runner) sets 'runner'.

Security (issue #63):
    Validates specified repo against SHIORI_REPOS (allowlist); rejects unlisted repos.
"""))
eng('ingest.py', 26, "Determine if this is a bulk path: rebuild=True or chunks table doesn't exist (issue #72).")

eng('mcp_server.py', 1, textwrap.dedent("""\
MCP server implementation. Entry point: main(), tool/fastmcp registration.

This module is long (~1100 lines) and structured as follows:
  1. Server setup & lifecycle (main, lifespan) — lines 1-90
  2. Internal helpers (paths, extensions, sync) — lines 90-310
  3. Tool definitions — lines 310-1100 (each tool has its own span)

Each tool function is typically under 100 lines.
"""))
eng('mcp_server.py', 123, "Determine if this is a bulk path: rebuild=True or chunks table is empty/non-existent (issue #72).")
eng('mcp_server.py', 296, "Execute sync and ingest tasks sequentially. Returns True if all succeeded.\nMultiple repos synced sequentially. wrap_ingest_error catches and logs exceptions.\n")
eng('mcp_server.py', 289, "Check if filename has a document extension (case-insensitive).")
eng('mcp_server.py', 294, "Check if filename has an excluded extension (case-insensitive).")
eng('mcp_server.py', 300, "Check if extension matches the given value (case-insensitive, with/without leading '.').")
eng('mcp_server.py', 306, "Walk code files on disk for a cloned repository. Returns relative paths matching _CODE_EXTENSIONS.\nDotfiles and binary-extension files excluded.\n")
eng('mcp_server.py', 356, "Semantic search MCP tool. Calls search.semantic_search internally with fallback for missing args.")
eng('mcp_server.py', 403, "Keyword search MCP tool. Calls search.keyword_search internally.")
eng('mcp_server.py', 427, "List indexed doc/code file paths, optionally filtered by path/source_type/extension.")
eng('mcp_server.py', 456, "Read file content from the filesystem clone (not from the index).\n\nReads from local clone (main branch fixed). No index required.\nPR head files: use read_pr_file or GitHub MCP.")
eng('mcp_server.py', 576, "Fetch a single issue (internal helper). Raises ValueError if not indexed.")
eng('mcp_server.py', 596, "Read an issue/PR thread (body + all comments) ordered by timestamp.\n\nFetches from the index. Raises ValueError if not indexed.\nexclude_noise_bots=True excludes bots outside SHIORI_INDEX_BOT_LOGINS (issue #44).\n")
eng('mcp_server.py', 654, "PR change file map (metadata only). Returns pointer: head_sha + file list.\n\nStored: head_sha / path / status / additions / deletions / changes / blob_url\nNot stored: patch hunks (via GitHub MCP) and PR head files (via shiori_read_pr_file).\n")
eng('mcp_server.py', 709, "Read PR head file content (transparently via git fetch).\n\nFetches refs/pull/{N}/head temporarily, reads file, cleans up.\nDoes NOT modify working tree.\n")
eng('mcp_server.py', 768, "Ingest MCP tool. Syncs docs/issues/code for a repo (or all repos).\n\nWraps github_sync.py logic.\nrebuild=True: discard and rebuild index (requires SHIORI_ALLOW_REBUILD=true, issue #63).\n")
eng('mcp_server.py', 816, "Detect index anomalies and return warning list (issue #31).")
eng('mcp_server.py', 834, "Index freshness and health status. Returns per-repo sync info and warnings.\n\nUsed by LLM to check if index is up-to-date before searching.\n")

eng('search.py', 1, textwrap.dedent("""\
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
"""))
eng('search.py', 25, "Score candidates by embedding + keyword similarity and return ranked results (detailed design/03).\n\nEmbedding: cosine distance (1 - cosine).\nKeyword: pgroonga_score (0-1) / pg_trgm word_similarity (0-1).\nRRF weight: 0.5 embedding, 0.5 keyword.\n")
eng('search.py', 38, "Sort by (source_type, source_id) and deduplicate by (target_type, target_id).\n")
eng('search.py', 56, "Keyword search entry point. Hybrid search internally.\n\nFilters: source_type / language / state / repo / path_prefix / updated_after / prog_lang / top_k (default 20)\nsort_by/sort_order accepted for backward compat; ranking always relevance-based (issue #69).\n")
eng('search.py', 71, "Semantic search entry point. Hybrid search internally.\n\nIdentical filtering to keyword_search.\n")

# ---------------------------------------------------------------------------
def get_node_ranges(fpath):
    with open(fpath, encoding='utf-8') as f:
        text = f.read()
    tree = ast.parse(text)
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
            v = val.value.strip()
            if doc.strip() in v or v in doc.strip() or doc.strip() == v:
                ranges[(stmt.lineno, name)] = stmt.end_lineno
                break
    return ranges

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
            # Detect quote style and indentation
            q = raw_lines[0][:3]
            if q not in ('"""', "'''"):
                q = '"""'
            indent = raw_lines[0][:len(raw_lines[0]) - len(raw_lines[0].lstrip())]
            new_doc = ENG[key]
            # If docstring text (after indent) starts with q, remove it
            content = raw_lines[0].strip()
            stripped_doc = new_doc
            # Add indentation to each line of new_doc
            doc_lines = stripped_doc.split('\n')
            indented_lines = []
            for i, dl in enumerate(doc_lines):
                if i == 0:
                    indented_lines.append(indent + q + dl)
                else:
                    if dl.strip():
                        indented_lines.append(indent + dl)
                    else:
                        indented_lines.append('')
            # Close quotes
            indented_lines[-1] = indented_lines[-1] + q
            replacement = '\n'.join(indented_lines)
            if raw_text not in text:
                print(f'  {fn}:{name} raw text vanished!')
                continue
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
