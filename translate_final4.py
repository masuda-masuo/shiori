"""Translate Japanese docstrings using AST + position-based raw text extraction."""
import ast, re, os, textwrap

JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

# =====================================================================
# English translations keyed by (fn, first line of cleaned docstring)
# =====================================================================
EN = {}

def reg(fn, en_body):
    """Register translation; key extracted from source file's actual first line."""
    # We store it under a sentinel and will register properly later
    _PENDING.append((fn, textwrap.dedent(en_body)))

_PENDING = []

# ---- chunking.py ----
reg('chunking.py', 'Chunk splitting (detailed design/02).\n\nDecisions:\n- docs: Split by heading, preserve heading paths in metadata.\n  Long sections use character-based splitting (default 1200 chars), prioritizing sentence boundaries.\n- issue/PR: Each comment is a natural unit, with `[title]` prepended as context prefix.\n- Language detected heuristically per chunk (effectively per file/comment) (ja/en).\n- code: Split by function/method/class via tree-sitter (map type: signature + docstring).\n  Unsupported languages fall back to _split_long_text (detailed design/10).\n')
reg('chunking.py', 'Find the closest semantic boundary not exceeding max_chars.\n\nAt this point, sentence splitting by _SENTENCE_END_RE has completed.\nHandles punctuation embedded within segments or consumed by previous chunks.\nFalls back to clause delimiters. Priority: sentence end marks → clause delimiters → hard cut.\n')
reg('chunking.py', 'Split identifiers at snake_case / camelCase / PascalCase boundaries and return space-separated lowercase.\n')
reg('chunking.py', 'Chunk source code by function/method/class (detailed design/10 Step 2).\n')

# ---- db.py ----
reg('db.py', 'DB connection and schema (detailed design/04).\ndocs/issue/pr_review/code share a single DB.\npgvector for embedding queries.\npgroonga for JP/EN full-text search (TokenMecab/Mecab preferred).\n')
reg('db.py', 'CREATE EXTENSION IF NOT EXISTS vector;')
reg('db.py', 'Create tables, constraints, and btree indexes only. HNSW created separately after load.')
reg('db.py', 'Create pgroonga indexes. Prefers TokenMecab; falls back to TokenBigram.')
reg('db.py', 'Create HNSW and pgroonga indexes (issue #72). Avoided during bulk load.')
reg('db.py', 'Drop HNSW and pgroonga indexes (issue #72). Temporarily dropped during bulk load for performance.')
reg('db.py', 'Full schema creation (tables + all indexes). Used in incremental path (issue #72).\nBulk path uses migrate_light() + create_heavy_indexes() after loading.\n')
reg('db.py', "Record sync success per repo and return completion timestamp (DB's now()) (issue #22 / #33).\nSkipped executions (advisory lock) not recorded. Uses DB now() for cross-path consistency.\n")
reg('db.py', 'Latest sync record per repo. age_seconds based on DB clock.')
reg('db.py', 'Chunk count by source_type (issue #31).')
reg('db.py', 'Total issue_item rows (includes bots; issue #31).')
reg('db.py', 'Fetch PR change file map (issue #54).')
reg('db.py', 'Upsert PR change file map (issue #54). Deletes existing entries for the same PR before insert.\n')
reg('db.py', 'Get stored PR head_sha for change detection (issue #54).')
reg('db.py', 'Bulk insert chunks via executemany (issue #72).')
reg('db.py', 'Idempotent ALTER for existing DB. CREATE TABLE IF NOT EXISTS does not add new columns.')
reg('db.py', 'Create tables, constraints, and btree indexes only. Skip HNSW/pgroonga (issue #72).')
reg('db.py', 'Create HNSW and pgroonga indexes (issue #72).')
reg('db.py', 'Drop HNSW and pgroonga indexes (issue #72).')
reg('db.py', 'Bulk insert chunks (executemany; issue #72).\nCaller handles commit.')

# ---- github_auth.py ----
reg('github_auth.py', 'GitHub authentication (detailed design/09).\nUnifies PAT and GitHub App installation tokens under TokenProvider abstraction.\nDecisions:\n- App preferred, then GITHUB_TOKEN, then anonymous (public repos only).\n- Installation token expires in 1 hour; refreshes 5 min before expiry.\n- Private key only passed to ingest process (not MCP server; see detailed design/07, 09).\n')
reg('github_auth.py', 'Abstract token supplier. get_token() returning None means anonymous (no auth).')
reg('github_auth.py', 'No authentication. Public repos only (strict rate limits).')
reg('github_auth.py', 'Static token, e.g. long-lived PAT.')
reg('github_auth.py', 'Issues and caches GitHub App installation access tokens.\nget_token() re-issues if not obtained or within REFRESH_BEFORE seconds of expiry.\nEnsures tokens survive long ingests (CPU embedding can exceed 1 hour).\n')
reg('github_auth.py', 'Build TokenProvider from environment variables.\nPriority:\n1. SHIORI_GITHUB_APP_ID + _KEY + _INSTALLATION_ID → AppTokenProvider\n2. GITHUB_TOKEN → StaticTokenProvider\n3. Neither → AnonymousProvider\n')
reg('github_auth.py', 'Generate JWT (RS256) for App authentication.\ni at set 60s in past to absorb clock skew. exp is 9 min (under 10-min limit).\n')
reg('github_auth.py', 'Select appropriate TokenProvider from Settings. Priority: App > PAT > anonymous.')
reg('github_auth.py', 'Generate JWT (RS256) for App authentication.\niat set 60s in past. exp is 9 min (under 10-min limit).')

# ---- github_sync.py ----
reg('github_sync.py', 'Data fetching and sync (detailed design/01).\n\nDecisions:\n- docs: git clone/pull; changed files re-chunked/re-embedded; deleted files removed from index.\n- issue/PR: REST API cross-repo endpoints + `since` (updated_at cursor) incremental sync.\n- Bot comments excluded; allowlist via SHIORI_INDEX_BOT_LOGINS (issue #25).\n- PR diffs not indexed; review comments include diff_hunk as context.\n- code: shares same clone; sha-delta re-indexes only changed files (issue #33).\n- PR change file maps: metadata only; content delegated to GitHub MCP (issue #54).\n- Bulk path: ChunkBuffer batches across files, bulk-inserts chunks, coarsens commits (issue #72).\n- _git_fetch_ref / _git_delete_ref: PR head file primitives (issue #81).\nAuth via TokenProvider (detailed design/09); git via http.extraHeader; API via httpx Auth hook.\n')
reg('github_sync.py', 'Allow indexing even for bot comments if login is in allowlist (issue #25).')
reg('github_sync.py', 'Accumulate chunks, batch-embed, bulk-insert, coarse-commit for high throughput.\nIncremental path unused; initial/rebuild only (detailed design/01, 02, 10).\n')
reg('github_sync.py', 'Mask auth credentials embedded in URLs (x-access-token:...@ etc.).')
reg('github_sync.py', 'Return git auth header as `-c http.extraHeader=...` args.\nToken clipped from clone URL.')
reg('github_sync.py', 'Shallow-fetch a ref and return a temp ref name (issue #81).\ntmp_ref=None skips fetch. Returns SHA of fetched ref.')
reg('github_sync.py', 'Delete a temporary ref (issue #81). No-op if not found.')
reg('github_sync.py', 'httpx Auth hook. Gets token from provider per request.\nRefreshes token near expiry to survive long ingests.')
reg('github_sync.py', 'Sync repo Markdown; index only changed files. Returns update count.\nWhen buffer specified (bulk path), uses ChunkBuffer for batch embedding.')
reg('github_sync.py', 'Sync source code; index only changed files (detailed design/10 Step 3).\nShares clone with sync_docs.')
reg('github_sync.py', 'Check if path matches excluded glob patterns.')
reg('github_sync.py', 'Sync PR change file maps (issue #54).\nGET /repos/{repo}/pulls/{issue_number}/files')
reg('github_sync.py', 'Incremental sync of issues/PRs/comments/reviews.\nWhen buffer specified (bulk path), uses ChunkBuffer for batch embedding.')
reg('github_sync.py', 'Add chunk to buffer. Auto-flushes at batch_size.')
reg('github_sync.py', 'Flush buffer: batch embed → bulk insert → commit. Returns insert count.')
reg('github_sync.py', 'Normalize control characters from GitHub API text (issue #73).\n')
reg('github_sync.py', 'Propagate issue_items state changes to chunks (issue #56).\n')
reg('github_sync.py', 'Paginate all pages via Link header.')
reg('github_sync.py', 'Determine if the relative path is a code file that should be indexed.\n')

# ---- ingest.py ----
reg('ingest.py', 'Ingest job (detailed design/01, 07).\nOn-demand: docker compose run --rm app python -m shiori ingest.\nAuth via build_token_provider shared across all repos (detailed design/09).\n\nProcess mutual exclusion (issue #6): PostgreSQL advisory lock prevents concurrent execution with serve auto-sync or MCP ingest.\n\nFreshness tracking (issue #22 / #33): Records completion to sync_runs per repo. Route via SHIORI_INGEST_ROUTE (default \'cli\').\n\nSecurity (issue #63): Validates repo against SHIORI_REPOS allowlist.\n')
reg('ingest.py', 'Determine if bulk path: rebuild=True or chunks table empty/missing (issue #72).')

# ---- mcp_server.py ----
reg('mcp_server.py', 'MCP server implementation.\n~1100 lines: setup (1-90), helpers (90-310), tools (310-1100). Each tool typically <100 lines.\n')
reg('mcp_server.py', 'Incremental sync body. Called by both ingest tool and auto-sync loop.\nProcess-level exclusion via _sync_lock (threading.Lock).')
reg('mcp_server.py', 'Check if filename has a document extension (case-insensitive).')
reg('mcp_server.py', 'Check if filename has an excluded extension (case-insensitive).')
reg('mcp_server.py', 'Check if extension matches given value (case-insensitive, with/without leading dot).')
reg('mcp_server.py', 'Walk clone and return code file relative paths.\nSkips .git/node_modules/.venv, binary extensions.\nOnly extensions in _CODE_EXTENSIONS.')
reg('mcp_server.py', 'Semantic search (entry). Strong for paraphrasing, concept, cross-lingual queries.\nHybrid with keyword search internally.')
reg('mcp_server.py', 'Keyword search (Japanese tokenize). Strong for exact matches: function names, API names, error codes, config keys.\nUsually called via semantic_search.')
reg('mcp_server.py', 'List indexed doc/code file paths. Filter by path/source_type/extension.\nUnderstand repo structure and locate files.')
reg('mcp_server.py', 'Read full file (or range) from clone (not index).\nPR head files via read_pr_file or GitHub MCP.')
reg('mcp_server.py', 'Fetch single issue (internal helper). Raises ValueError if not indexed.')
reg('mcp_server.py', 'Fetch full issue/PR thread chronologically (body + comments + review).\nBot comments included (identifiable via is_bot).')
reg('mcp_server.py', 'PR change file map (metadata pointer; issue #54, #100).\nStored: head_sha / path / status / additions / deletions / changes / blob_url\nNot stored: patch hunks (via GitHub MCP) and PR head files (via shiori_read_pr_file).')
reg('mcp_server.py', 'Read PR head file content (or range). Delegated from read_file with PR-specific fetch.')
reg('mcp_server.py', 'Sync docs/issues/code from GitHub and update index (entry).\nrebuild=True: discard and full rebuild (requires SHIORI_ALLOW_REBUILD=true; issue #63).\nAlso treated as rebuild when chunks table is empty.')
reg('mcp_server.py', 'Detect index anomalies and return warning list (issue #31).')
reg('mcp_server.py', 'Index freshness and health. Per-repo: last_synced_at, age_seconds, route, counts, items, cursor, warnings.')
reg('mcp_server.py', 'Determine bulk path: rebuild=True or chunks empty/missing (issue #72).')

# ---- search.py ----
reg('search.py', 'Search orchestration (hybrid: embedding + keyword; detailed design/03).\nTwo-store: pgvector (embedding, cosine similarity) + pgroonga (FTS, falls back to pg_trgm).\nHybrid: RRF fusion with configurable weights. Post-filter: language/source_type/repo/state/path_prefix.\nTwo modes: simple (1 table, single kNN) / complex (2 tables, kNN per combo → agg).')
reg('search.py', 'Score by embedding + keyword similarity; return ranked results (detailed design/03).\nEmbedding: cosine distance (1-cosine). Keyword: pgroonga_score/pg_trgm.\nRRF: 0.5 embedding + 0.5 keyword.')
reg('search.py', 'Sort by PK, dedup by (target_type, target_id). Stable ordering for simple/complex paths.')
reg('search.py', 'Keyword search entry (hybrid internally). Supports standard filters.\nsort_by/sort_order for backward compat; ranking always relevance-based (issue #69).\nReturns list of Hit objects.')
reg('search.py', 'Semantic search entry (hybrid internally). Identical filtering to keyword_search.')

# =================================================================
# Step 1: extract actual first lines from source files
# =================================================================
src_dir = os.path.join(os.path.dirname(__file__), 'src', 'shiori')
skipped = {'config.py', 'embedding.py', '__init__.py', '__main__.py'}

actual = {}  # (fn, name) -> first_line

for fn in sorted(os.listdir(src_dir)):
    if not fn.endswith('.py') or fn in skipped:
        continue
    fpath = os.path.join(src_dir, fn)
    with open(fpath, encoding='utf-8') as f:
        text = f.read()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, TYPES):
            continue
        name = getattr(node, 'name', '<module>')
        doc = ast.get_docstring(node)
        if not doc or not JP.search(doc):
            continue
        first_line = doc.strip().split('\n')[0][:60]
        actual[(fn, name)] = first_line

print(f'Found {len(actual)} JP docstrings')

# Step 2: read all pending translations, key by (fn, first_line from source)
# Since reg() just appends to _PENDING, we need to pair with actual first lines
pending_iter = iter(_PENDING)
# We need to build a mapping: for each JP docstring in order, assign the next pending translation
# Order: walk order from ast.walk. But reg() was called in a specific order.
# Honestly, the simplest approach: manually match.

# Let's just print all actual first lines for manual matching
for (fn, name), fl in sorted(actual.items()):
    print(f"  {fn}:{name}: {fl!r}")
