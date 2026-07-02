"""Generate a standalone Python script that translates all Japanese docstrings.

For each file, it reads the source, finds Japanese docstrings via AST,
replaces them with hardcoded English translations (using exact substring matching),
and writes the result.
"""
import ast, re, os, textwrap

JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

# Mapping from (filename, node_name) -> English docstring (without quotes, without indent)
TRANSLATIONS = {
    # ---- chunking.py ----
    ('chunking.py', '_find_breakpoint'): textwrap.dedent("""\
    Find the closest semantic boundary not exceeding max_chars.

    At the point this function is called, sentence splitting by _SENTENCE_END_RE has already completed.
    Punctuation marks (。．！？！?.) are already used by _SENTENCE_END_RE for splitting, so
    this function finds punctuation in the following cases:
      1. Within text split by non-punctuation conditions (_SENTENCE_END_RE's \\\\n{2,}),
         where punctuation remains inside the segment.
      2. When punctuation was consumed by the previous chunk and the current s has none
           → fall back to clause delimiters（、，,）

    In practice, **clause-delimiter fallback** is the main role.

    Priority order:
    1. Sentence-ending marks（。．！？！?.: end of sentence
    2. Clause delimiters（、，,: clause boundary
    3. Hard-cut at max_chars if none found
    """),

    # ---- db.py ----
    ('db.py', '_run_alter_statements'): "Idempotent ALTER for existing DB. CREATE TABLE IF NOT EXISTS does not add new columns.\n\nCalled by both migrate_light and migrate.\n",
    ('db.py', 'migrate'): "Full schema creation (tables + all indexes). Used in incremental path (issue #72).\n\nBulk path uses migrate_light() + create_heavy_indexes() after loading.\n",
    ('db.py', 'record_sync_run'): "Record a successful sync per repository and return the completion timestamp (DB's now()) (issue #22 / #33).\n\nExecutions skipped by advisory lock are NOT recorded (only successful syncs).\nTimestamp uses DB now(): consistent across multiple paths and processes using a single DB clock.\n",
    ('db.py', 'upsert_pr_changes'): "Upsert PR change file map (issue #54). Deletes existing entries for the same PR first, then inserts new ones.\n",

    # ---- github_auth.py ----
    ('github_auth.py', 'AppTokenProvider'): textwrap.dedent("""\
    GitHub App installation token provider for fine-grained auth (detailed design/09).

    Used by reindex.yml (self-hosted runner) and optionally for server-local ingestion.

    Token generation:
      JWT (GitHub App private key PEM) → POST /app/installations/{id}/access_tokens
      → installation token (expires 1 hour). Cached until near expiry.

    Compared to GITHUB_TOKEN (PAT): installation tokens are scoped to the specific installation,
    not to a personal account, enabling least-privilege operation on repos across organizations.

    Retry: HTTP 401 refreshes the token once automatically.
    """),

    # ---- search.py ----
    ('search.py', '<module>'): textwrap.dedent("""\
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
    """),
    ('search.py', '_rank_candidates'): "Score candidates by embedding + keyword similarity and return ranked results (detailed design/03).\n\nEmbedding similarity: cosine distance (1 - cosine).\nKeyword similarity: pgroonga_score (0-1) when available, else pg_trgm word_similarity (0-1).\nRRF weight: 0.5 for embedding, 0.5 for keyword.\n",
    ('search.py', '_sort_hits'): "Sort hits by primary key order and deduplicate by (target_type, target_id).\nSame hit order is returned by select_target_ids_simple/complex; this sort is just for stable ordering.\n",
    ('search.py', 'keyword_search'): textwrap.dedent("""\
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
    """),
    ('search.py', 'semantic_search'): textwrap.dedent("""\
    Semantic search entry point. Executed as a hybrid search internally.

    Identical filtering to keyword_search.
    """),

    # ---- mcp_server.py ----
    ('mcp_server.py', '<module>'): textwrap.dedent("""\
    MCP server implementation. Entry point: main(), tool/fastmcp registration.

    This module is long（~1100 lines）and structured as follows:
      1. Server setup & lifecycle (main, lifespan) — lines 1-90
      2. Internal helpers — lines 90-310
      3. Tool definitions — lines 310-1100
        (each tool has its own span)

    Each tool function is typically less than 100 lines.
    """),
    ('mcp_server.py', '_do_sync'): "Execute sync and ingest tasks sequentially. Returns True if all succeeded, False otherwise.\n\nMultiple repos are synced sequentially (no parallel execution).\nwrap_ingest_error catches and logs exceptions instead of propagating.\n",
    ('mcp_server.py', '_walk_code_files'): "Walk code files on disk for a cloned repository. Returns a list of relative paths matching extensions in _CODE_EXTENSIONS.\nHidden files (dotfiles) and binary-extension files are excluded.\n",
    ('mcp_server.py', 'semantic_search'): "Semantic search MCP tool. Calls search.semantic_search internally with fallback for missing args.",
    ('mcp_server.py', 'keyword_search'): "Keyword search MCP tool. Calls search.keyword_search internally.",
    ('mcp_server.py', 'list_tree'): "List indexed doc/code file paths, optionally filtered by path/source_type/extension.",
    ('mcp_server.py', 'read_file'): textwrap.dedent("""\
    Read file content from the filesystem clone (not from the index).

    Reads from the local clone (main branch fixed). Does not require an index.
    PR head files should be read via read_pr_file or GitHub MCP.

    Parameters match the MCP tool definition.
    """),
    ('mcp_server.py', 'read_issue'): textwrap.dedent("""\
    Read an issue/PR thread (body + all comments) ordered by timestamp.

    Fetches from the index. Raises ValueError if not indexed.
    exclude_noise_bots=True excludes bot comments outside SHIORI_INDEX_BOT_LOGINS (issue #44).
    """),
    ('mcp_server.py', 'pr_changes'): textwrap.dedent("""\
    PR change file map (metadata only). Returns a pointer: head_sha + file list.

    Stored (metadata):
      - head_sha / path / status / additions / deletions / changes / blob_url
    Not stored (content):
      - patch hunks → fetch via GitHub MCP pull_request_read(method='get_diff')
      - PR head files → fetch via shiori_read_pr_file or GitHub MCP
    """),
    ('mcp_server.py', 'read_pr_file'): "Read PR head file content (transparently via git fetch).\n\nFetches refs/pull/{N}/head temporarily, reads the file, and cleans up.\nDoes NOT modify the working tree.\n",
    ('mcp_server.py', 'ingest'): textwrap.dedent("""\
    Ingest MCP tool. Syncs docs/issues/code for a repo (or all repos).

    Wraps sync logic from github_sync.py.

    rebuild=True: discards the existing index and rebuilds from scratch (issue #63).
      Requires SHIORI_ALLOW_REBUILD=true environment variable. Disabled by default in MCP tools (issue #63).
      Also allowed when chunks table is empty/non-existent.

    repo: specific repo to sync, or None for all.
    """),
    ('mcp_server.py', 'status'): "Index freshness and health status. Returns per-repo sync info and warnings.\n\nUsed by the LLM to check if the index is up-to-date before searching.\n",
}

# ---- Auto-translate module docstrings from actual files ----
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
        doc = ast.get_docstring(node)
        if not doc or not JP.search(doc): continue
        key = (fname, name)
        if key not in TRANSLATIONS:
            print(f'  MISSING TRANSLATION: {key}')
            continue
        new_doc = TRANSLATIONS[key]
        # Find the exact raw docstring text in the source
        body = node.body if hasattr(node, 'body') else []
        for stmt in body:
            if not isinstance(stmt, ast.Expr): continue
            val = stmt.value
            if not isinstance(val, ast.Constant) or not isinstance(val.value, str): continue
            if doc in val.value or val.value.strip() == doc.strip():
                # Get exact raw text
                raw_lines = lines[stmt.lineno-1:stmt.end_lineno]
                raw_text = '\n'.join(raw_lines)
                # Detect quote style
                first_line = raw_lines[0]
                if first_line.startswith('"""'):
                    q = '"""'
                elif first_line.startswith("'''"):
                    q = "'''"
                else:
                    # Fallback: detect from first three characters
                    q = first_line[:3]
                # Build replacement: q + new_doc + q
                if new_doc.endswith('\n'):
                    replacement = q + '\n' + new_doc + q
                else:
                    replacement = q + new_doc + q
                if raw_text in text:
                    text = text.replace(raw_text, replacement, 1)
                    print(f'  {fname}:{name} replaced (lines {stmt.lineno}-{stmt.end_lineno})')
                else:
                    print(f'  {fname}:{name} raw text NOT FOUND in source!')
                break
        else:
            print(f'  {fname}:{name} could not locate body statement')

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
        try:
            translate_file(fpath)
        except Exception as e:
            print(f'  ERROR: {e}')
