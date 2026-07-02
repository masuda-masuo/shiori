"""Translate Japanese docstrings — position-based matching.

Finds Japanese docstrings via regex in order of appearance,
replaces each with the corresponding English translation from pre-defined list.
"""
import re, os, textwrap

DOC_RE = re.compile(r'''(?P<q>"""|\'\'\')(?P<body>(?:\\.|[^\\])*?)(?P=q)''', re.DOTALL)
JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')
HERE = os.path.dirname(__file__)
SKIP = {'config.py', 'embedding.py', '__init__.py', '__main__.py'}

def tr(content):
    """Return a dedented translation text."""
    return textwrap.dedent(content)

# Translation lists per file — nth JP docstring maps to nth English translation
TRANSLATIONS = {}

def set_tr(fn, docs):
    TRANSLATIONS[fn] = docs

set_tr('chunking.py', [
    tr("""Chunk splitting (detailed design/02).

Decisions:
- docs: Split by heading, preserve heading paths in metadata.
  Long sections use character-based splitting (default 1200 chars), prioritizing sentence boundaries.
- issue/PR: Each comment is a natural unit, with `[title]` prepended as context prefix.
- Language detected heuristically per chunk (effectively per file/comment) (ja/en).
- code: Split by function/method/class via tree-sitter (map type: signature + docstring).
  Unsupported languages fall back to _split_long_text (detailed design/10).
"""),
    tr("""Split by symbols (function/method/class) via tree-sitter.

Supported languages and their tree-sitter query files:
  Python (.py), TypeScript (.ts, .tsx), Go (.go), Rust (.rs)
Query file name: queries/{language}-symbols.scm
(detailed design/10).
"""),
    tr("""Convert code chunks to documentable units.

Converts named blocks from source code into map-type chunks.
"""),
])

set_tr('db.py', [
    tr("""Database schema and queries (detailed design/02). See query comments for table/column descriptions. Only design decisions recorded here.

Decisions:
- Chunks use 2-table structure (doc and issue) distinguished by target_type.
- pgvector index: HNSW (higher accuracy and faster build than IVFFlat)
- pgroonga index: Japanese tokenization (TokenNgram) + English tokenization (TokenBigram)
- Full-text search fallback: pg_trgm (for environments without pgroonga)
"""),
    tr("""Create pgvector extension.
"""),
    tr("""Create tables, constraints, and btree indexes only. HNSW and pgroonga indexes are NOT created here.
"""),
    tr("""Create pgroonga indexes. Prefers TokenMecab; falls back to TokenNgram if unavailable.
"""),
    tr("""Create HNSW and pgroonga indexes (issue #72).
"""),
    tr("""Drop HNSW and pgroonga indexes (issue #72).
"""),
    tr("""Latest sync record per repository. age_seconds is elapsed seconds based on DB clock.
"""),
    tr("""Chunk count by source_type (issue #31).
"""),
    tr("""Total issue_item count (includes bots; issue #31).
"""),
    tr("""Get PR change file map (issue #54).

Returns a list of pr_changes rows for the given PR number.
"""),
    tr("""Upsert PR change file map (issue #54).

Deletes existing entries for the same PR first, then inserts new ones.
"""),
    tr("""Get stored head_sha for a PR (for change detection; issue #54).
"""),
    tr("""Bulk insert chunks (executemany; issue #72).
"""),
])

set_tr('github_sync.py', [
    tr("""Data fetching and sync (detailed design/01).

Decisions:
- docs: git clone/pull. Per-file content hash tracked in doc_files; only changed files re-chunked and re-embedded.
- issue/PR: REST API cross-repo endpoints + `since` (updated_at cursor) incremental sync.
- Bot comments excluded from index (allowlist via SHIORI_INDEX_BOT_LOGINS; issue #25).
- PR diffs not indexed; review comments include diff_hunk as context.
- code shares same clone as sync_docs (issue #33).
- PR change file maps are metadata only (issue #54).
- Bulk path: ChunkBuffer batches across files (issue #72).
- _git_fetch_ref / _git_delete_ref: common primitives for PR head files (issue #81).
Authentication via TokenProvider abstraction (detailed design/09).
"""),
    tr("""Buffered chunk inserter for bulk paths (initial/rebuild). Batches across files, bulk-inserts chunks, coarsens commits (issue #72).
"""),
    tr("""Build git authentication headers (-c http.extraHeader=...). Works with any TokenProvider implementation (env var / GitHub CLI / OIDC).
"""),
    tr("""Shallow-fetch a given ref and return the SHA via a temporary ref name (issue #81).
"""),
    tr("""httpx Auth hook. Fetches token from provider per request.
"""),
    tr("""Repository Markdown sync. Syncs repos, indexes only what changed. Returns updated file count.
"""),
    tr("""Sync source code for a repository; index only changed files (detailed design/10 Step 2).
"""),
    tr("""Sync PR change file maps (issue #54).

Fetches changed files per PR via GitHub API.
"""),
    tr("""Incremental sync of issues/PRs/comments/reviews.
"""),
    tr("""Add chunk to buffer. Auto-flushes when batch_size is reached.
"""),
    tr("""Flush buffer: batch embed, bulk insert, and coarse-grained commit for high throughput.
"""),
])

set_tr('ingest.py', [
    tr("""Ingest job (detailed design/01, 07).

Decision: Sync is on-demand execution.
     docker compose run --rm app python -m shiori ingest
For scheduled execution, trigger from host cron etc.
Authentication via build_token_provider (detailed design/09).

Process mutual exclusion (issue #6):
    Uses PostgreSQL advisory lock (pg_try_advisory_lock) to prevent concurrent execution
    with serve process auto-sync or MCP tool ingest.
    SYNC_LOCK_KEY matches mcp_server.py (0x5348494F = 'SHIO').

Freshness tracking (issue #22 / #33):
    Records completion timestamp and route to sync_runs per repository.
    Route from SHIORI_INGEST_ROUTE (default 'cli').

Security (issue #63):
    Validates repo against SHIORI_REPOS (allowlist); rejects unlisted.
"""),
    tr("""Determine if this is a bulk path: rebuild=True or chunks table doesn't exist (issue #72).
"""),
])

set_tr('mcp_server.py', [
    tr("""MCP server implementation. Entry point: main(), tool/fastmcp registration.

This module is long (~1100 lines) and structured as follows:
  1. Server setup & lifecycle (main, lifespan) — lines 1-90
  2. Internal helpers (paths, extensions, sync, search) — lines 90-310
  3. Tool definitions — lines 310-1100 (each tool its own span)

Each tool function is typically under 100 lines.
"""),
    tr("""Determine if this is a bulk path: rebuild=True or chunks table is empty/non-existent (issue #72).
"""),
    tr("""Incremental sync body. Called by both the ingest tool and auto-sync loop.

Calls sync_issues / sync_docs / sync_code from github_sync.py sequentially for each repo.
wrap_ingest_error catches and logs exceptions.
"""),
    tr("""Check if filename has a document extension (case-insensitive).
"""),
    tr("""Check if filename has an excluded extension (case-insensitive).
"""),
    tr("""Check if extension matches given value (case-insensitive, with/without leading '.').
"""),
    tr("""Walk the clone and return set of code file relative paths.

    - Skips dotfiles and files with binary extensions.
    - Filters by _CODE_EXTENSIONS set.
"""),
    tr("""Semantic search (entry tool). Handles paraphrasing, concept, and cross-lingual queries. Internal hybrid search.
"""),
    tr("""Keyword search (entry tool). Strong for exact matches: function names, API names, error codes, config keys.
"""),
    tr("""List indexed doc/code file paths. Filter by path/source_type/extension.
"""),
    tr("""Read file content (or start_line-end_line range). Reads from local clone, not index.

Internally reads from clone; PR head files via read_pr_file or GitHub MCP.
"""),
    tr("""Fetch a single issue (internal helper). Raises ValueError if not indexed.
"""),
    tr("""Fetch full issue/PR thread (body + comments + review comments) in chronological order.
"""),
    tr("""PR change file map (metadata pointer tool; issue #54). Returns head_sha and file list.

Stored: head_sha / path / status / additions / deletions / changes / blob_url.
Not stored: patch hunks (via GitHub MCP) and PR head files (via shiori_read_pr_file or GitHub MCP).
"""),
    tr("""Read PR head file content (or start_line-end_line range) transparently via git fetch.
"""),
    tr("""Sync docs/issues/code from GitHub and update index (entry tool).

rebuild=True: Discard existing index and full rebuild (issue #63; requires SHIORI_ALLOW_REBUILD=true).
"""),
    tr("""Index freshness and health. Returns per-repo sync info with warning list (issue #31).
"""),
    tr("""Return per-repo latest sync info and warnings.
"""),
])

set_tr('search.py', [
    tr("""Search orchestration (hybrid: embedding + keyword; detailed design/03).

Entry point for semantic_search / keyword_search.

Two-store design: pgvector embedding + pgroonga (jp/en) full-text search.
Fallback to pg_trgm when pgroonga unavailable (primary reason for pg_trgm in docker-compose.yml).

Hybrid search: RRF merge with configurable weights (0.5/0.5 default).
Post-filter: source_type / language / repo / state / path_prefix / updated_after / prog_lang.

Two execution modes depending on filter complexity (simple/complex).
"""),
    tr("""Score by embedding + keyword similarity and return ranked hits.

Uses cosine distance (1-cosine) for embedding and pgroonga_score / pg_trgm word_similarity (0-1) for keyword.
RRF weight: 0.5 embedding + 0.5 keyword.
Sorts by score descending, then deduplicates by (target_type, target_id).
"""),
    tr("""Sort and deduplicate hits by (target_type, target_id). Returns hits in insertion order.
"""),
    tr("""Keyword search entry (performs hybrid search internally).

Supports: source_type / language / state / repo / path_prefix / updated_after / prog_lang / top_k (default 20).
sort_by/sort_order accepted for backward compat; ranking always relevance-based (issue #69).
"""),
    tr("""Semantic search entry (performs hybrid search internally).

Identical filtering to keyword_search.
"""),
])

set_tr('github_auth.py', [
    tr("""Issues and caches GitHub App installation access tokens.

get_token() re-issues if not yet obtained or within REFRESH_BEFORE seconds of expiry.
Ensures tokens don't expire mid-operation even during long ingests (CPU embedding can exceed 1 hour).
"""),
])

# =================================================================
for fn, (text, jp_docs) in sorted(all_files().items()):
    if fn not in TRANSLATIONS:
        print(f'MISSING TRANSLATIONS: {fn}')
        continue
    translations = TRANSLATIONS[fn]
    if len(translations) != len(jp_docs):
        print(f'COUNT MISMATCH: {fn}: {len(jp_docs)} JP, {len(translations)} EN')
        continue
    
    # Apply in reverse order to preserve positions
    for i in range(len(jp_docs) - 1, -1, -1):
        start, end, q, body, raw = jp_docs[i]
        # Find indentation
        line_start = text.rfind('\n', 0, start)
        indent = text[line_start + 1:start] if line_start >= 0 else ''
        new_body = translations[i]
        en_lines = new_body.split('\n')
        result_lines = []
        for j, line in enumerate(en_lines):
            if j == 0:
                result_lines.append(indent + q + line)
            elif line.strip():
                result_lines.append(indent + line)
            else:
                result_lines.append('')
        result_lines[-1] = result_lines[-1] + q
        replacement = '\n'.join(result_lines)
        if replacement != raw:
            text = text[:start] + replacement + text[end:]
    
    with open(fpath, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f'{fn}: saved ({len(jp_docs)} replaced)')
