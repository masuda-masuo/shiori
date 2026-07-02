"""Translate all remaining Japanese docstrings in src/shiori/.
Reads exact source text via AST position and replaces with hardcoded English.
"""
import ast, os, re, sys

JP = re.compile(r'[\u3000-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

# ENGLISH TRANSLATIONS keyed by (filename, node_start_line, node_name)
# Each value is the replacement text for the full raw docstring (with quotes)
TRANSLATIONS = {}

def add(fn, start_line, name, raw_jp, raw_en):
    """Register a translation: raw_jp must match the exact source lines (with quotes)."""
    TRANSLATIONS[(fn, start_line, name)] = (raw_jp, raw_en)

# ─── chunking.py ───
# These two didn't match in transform_file
# The `_split_symbols` docstring 
# The `_find_sentence_boundary` docstring

# Actually, let me try a different approach: iterate through AST and replace directly

for fn in sorted(os.listdir('src/shiori')):
    if not fn.endswith('.py'):
        continue
    path = os.path.join('src/shiori', fn)
    with open(path) as f:
        text = f.read()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        print(f'{fn}: SKIP (syntax error)')
        continue
    
    lines = text.split('\n')
    modified = False
    
    for node in ast.walk(tree):
        if not isinstance(node, TYPES):
            continue
        doc = ast.get_docstring(node)
        if not doc or not JP.search(doc):
            continue
        
        # Find the docstring expression in the body
        body = node.body if hasattr(node, 'body') else []
        for stmt in body:
            if not isinstance(stmt, ast.Expr):
                continue
            val = stmt.value
            if isinstance(val, ast.Constant) and isinstance(val.value, str) and val.value == doc:
                first = stmt.lineno  # 1-indexed
                last = stmt.end_lineno
                # Get the raw text of the docstring including quotes
                raw_lines = lines[first-1:last]
                raw_text = '\n'.join(raw_lines)
                
                # Generate English translation based on Japanese content
                name = getattr(node, 'name', '<module>')
                en = _translate(doc, fn, name)
                if en is None:
                    continue
                
                # Replace the raw text with English version
                new_raw = _make_raw(en, raw_text)
                if new_raw is None:
                    print(f'{fn}:{first}: SKIP - could not determine quote style')
                    continue
                
                text = text.replace(raw_text, new_raw, 1)
                modified = True
                print(f'{fn}:{first} ({name}): OK')
                break
    
    if modified:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

def _detect_quote(raw):
    """Detect quote style from raw docstring."""
    stripped = raw.strip()
    if stripped.startswith('"""'):
        return '"""', '"""'
    elif stripped.startswith("'''"):
        return "'''", "'''"
    return None, None

def _make_raw(en_text, raw):
    """Create raw docstring with same quote style as raw."""
    open_q, close_q = _detect_quote(raw)
    if open_q is None:
        return None
    # Determine indentation
    indent = ''
    for ch in raw:
        if ch in ' \t':
            indent += ch
        else:
            break
    lines = en_text.split('\n')
    if len(lines) == 1:
        return f'{indent}{open_q}{lines[0]}{close_q}'
    else:
        inner = '\n'.join(f'{indent}{l}' if i > 0 else l for i, l in enumerate(lines))
        return f'{indent}{open_q}\n{inner}\n{indent}{close_q}'

def _translate(jp, fn, name):
    jp = jp.strip()
    
    # chunking.py
    if 'max_chars を超えない最も近い意味的境界を探す' in jp:
        return 'Find the nearest semantic boundary not exceeding max_chars.\n\nBy the time this function is called, sentence splitting via _SENTENCE_END_RE is complete.\nSince _SENTENCE_END_RE already uses terminal punctuation (。．！？!?.) for splitting,\nthis function encounters punctuation only in these cases:\n  1. Punctuation remaining within a sentence created by non-punctuation splits (\\n{2,})\n  2. Punctuation was consumed by a previous chunk, and current s has none\n       → falls back to comma (，，,)\nIn practice, the comma fallback is the primary role.\n\nPriority:\n1. Period/terminal marks (。．！？!?.): sentence end\n2. Commas (，，,): clause boundary\n3. Hard cut at max_chars if none found'
    
    # db.py - _run_alter_statements
    if '既存 DB に対する軽等な ALTER' in jp:
        return 'Lightweight ALTER for existing DB. CREATE TABLE IF NOT EXISTS does not add new columns.\n\nCalled from both migrate_light and migrate.'
    
    # db.py - get_pr_changes
    if 'PR の変更ファイルマップを取得する' in jp:
        return 'Get the PR changed files map (issue #54).\n\nReturns:\n    (files, head_sha) tuple.\n    files: file list (path / status / additions / deletions / changes / blob_url)\n    head_sha: PR head_sha (shared across all files). Present even with 0 files.'
    
    # db.py - bulk_insert_chunks
    if 'チャンクをバルク挿入する' in jp:
        return 'Insert chunks in bulk (executemany, issue #72).\n\nCaller is responsible for committing. Each row is a dict with the same keys as insert_chunk.\nembedding is passed as a list (vec_literal is applied internally).'
    
    # db.py - full schema
    if '完全なスキーマ作成' in jp:
        return 'Full schema creation (tables + all indexes). Used on the incremental path (issue #72).\n\nOn the bulk path, use migrate_light() + create_heavy_indexes() after load.'
    
    # github_auth.py - AppTokenProvider
    if 'AppTokenProvider' in name or ('installation access token' in jp and '発行' in jp):
        return 'Issue and cache GitHub App installation access tokens.\n\nget_token() re-issues if not yet obtained or within REFRESH_BEFORE seconds of expiry.\nEnsures tokens do not expire mid-ingest (CPU embedding can exceed 1 hour).'
    
    # github_sync.py - module
    if fn == 'github_sync.py' and name == '<module>':
        return 'Data fetching and sync (detailed design/01).\n\nDecisions:\n- docs: git clone/pull. Per-file content hash tracked in doc_files;\n  only changed files are re-chunked and re-embedded. Deleted file indexes are removed.\n- issue/PR: GitHub Issues/PRs REST API with etag-based conditional requests.\n  Syncs issue body, comments, and review comments.\n  Reviews are fetched via GET /repos/{owner}/{repo}/pulls/{number}/reviews.\n- code: File-system crawl with sha-based delta indexing (detailed design/10).'
    
    # github_sync.py - sync_code
    if 'リポジトリのソースコードを同期' in jp:
        return 'Sync repository source code, indexing only changed files (detailed design/10 Step 3).'
    
    # github_sync.py - ChunkBuffer class
    if 'チャンクを蓄積し、バッチ埋め込み' in jp:
        return 'Accumulate chunks for batch embed + bulk insert + coarse-grained commit.\n\nNot used on the incremental path; only on initial/rebuild (bulk path).'
    
    # github_sync.py - _auth_args
    if 'git の認証ヘッダを' in jp:
        return 'Return git auth headers as `-c http.extraHeader=...` arguments.\n\nEmbedding tokens in clone URLs persists them in plaintext in `.git/config`,\nand short-lived tokens leave expired tokens for subsequent fetches.\nInjecting headers every time avoids this. Returns empty list for anonymous access.'
    
    # github_sync.py - _GitHubAuth class
    if 'httpx の Auth フック' in jp:
        return 'httpx Auth hook. Obtains token from provider per request and injects it.\n\nEven during long ingests, the provider auto-renews mid-pagination so tokens never expire.'
    
    # github_sync.py - ChunkBuffer.add
    if 'チャンクをバッファに追加' in jp:
        return 'Add a chunk to the buffer. Auto-flushes when batch_size is reached.'
    
    # github_sync.py - ChunkBuffer.flush
    if 'バッファをフラッシュ' in jp:
        return 'Flush the buffer: batch embed → bulk insert → commit. Returns inserted count.'
    
    # github_sync.py - _git (the comment/docstring)
    if 'cwd が指定' in jp and 'safe.directory' in jp:
        # This is a code comment, not a docstring
        return None
    
    # github_sync.py - sync_code return
    if '同期・索引したファイル数' in jp:
        return None  # Already part of sync_code
    
    # github_sync.py - sync_issues
    if 'Issue/PR を GitHub API から取得' in jp:
        return None  # Already translated?
    
    # ingest.py - module
    if 'ingest ジョブ' in jp:
        return 'Ingest job (detailed design/01, 07).\n\nDecision: Sync is on-demand execution.\n    docker compose run --rm app python -m shiori ingest\nFor scheduled execution, trigger the same command from host cron etc.\nAuthentication is built via build_token_provider and shared across all repos (detailed design/09).\n\nProcess mutual exclusion (issue #6):\n    Uses PostgreSQL advisory lock (pg_try_advisory_lock) to prevent concurrent execution\n    with the serve process auto-sync or MCP tool ingest.\n    SYNC_LOCK_KEY matches mcp_server.py (0x5348494F = \\'SHIO\\').\n\nFreshness tracking (issue #22 / #33):\n    Records completion timestamp and route to sync_runs per repository.\n    Route is from environment variable SHIORI_INGEST_ROUTE (default \\'cli\\').\n    reindex.yml (self-hosted runner) sets \\'runner\\' to identify the execution route.\n\nSecurity (issue #63):\n    Validates specified repo against SHIORI_REPOS (allowlist); rejects unlisted repos.\n\nBulk path optimization (issue #72):\n    On initial or rebuild, creates heavy indexes (HNSW/pgroonga) in batch after loading,\n    batches embedding at file level, and bulk-inserts chunks.'
    
    # ingest.py - _is_bulk
    if 'バルク経路か判定' in jp:
        return 'Determine whether this is a bulk path: rebuild=True or chunks table is empty/absent.\n\nNew DB (chunks table not created) is also treated as bulk (issue #72).'
    
    # mcp_server.py - module
    if fn == 'mcp_server.py' and name == '<module>':
        return 'MCP server (detailed design/06).'
    
    # mcp_server.py - _is_bulk
    if 'バルク経路か判定する' in jp:
        return 'Determine whether this is a bulk path: rebuild=True or chunks table is empty/absent (issue #72).'
    
    # mcp_server.py - _do_sync
    if '差分同期の実体' in jp:
        return None  # Already translated?
    
    # mcp_server.py - _is_doc_extension
    if 'ファイル名がドキュメント拡張子か' in jp:
        return 'Check if filename has a document extension (case-insensitive).'
    
    # mcp_server.py - _is_excluded_file
    if 'ファイル名が除外拡張子か' in jp:
        return 'Check if filename has an excluded extension (case-insensitive).'
    
    # mcp_server.py - _match_extension
    if '拡張子が指定値にマッチするか' in jp:
        return 'Check if extension matches the specified value (case-insensitive, dot-optional).'
    
    # mcp_server.py - _walk_code_files
    if 'クローンを walk し' in jp:
        return 'Walk the clone and return a set of relative paths for code files.\n\n- Skips noise directories like .git / node_modules / .venv / __pycache__\n- Excludes .md / .mdx / .markdown (handled by doc_files table)\n- Excludes binary/asset/lock file extensions\n- When prefix is given, only returns paths under that prefix\n  (e.g. prefix="src" matches "src/main.py" but not "src2/main.py")\n- When extension is given, filters files by extension during walk (case-insensitive)'
    
    # mcp_server.py - _read_issue_single
    if '1 件の issue を取得' in jp:
        return 'Fetch a single issue (internal helper). Raises ValueError if not indexed.'
    
    # mcp_server.py - ingest tool
    if 'docs / issue/PR / code を GitHub から同期' in jp:
        return None  # Already translated?
    
    # mcp_server.py - status tool
    if '索引の鮮度と健全性を返す' in jp:
        return None  # Already translated?
    
    # mcp_server.py - _build_warnings
    if '索引の異常を検出' in jp:
        return 'Detect index anomalies and return a warning list (issue #31).'
    
    # search.py - module
    if fn == 'search.py' and name == '<module>':
        return 'Search (detailed design/05).\n\nDecisions:\n- `semantic_search` internally hybrid: fetches candidates via both pgvector similarity and\n  pgroonga keyword search, fuses via RRF (k=60), returns top-k. Backs the MCP tool\n  `shiori_search`, the primary entry point.\n- `keyword_search` is kept separate as a pgroonga (`&@~`)-based exact-match search.\n  For code chunks, searches both content and symbols columns with OR (issue #33).\n- Re-ranking model: not adopted in v1 (RRF only).\n- Always returns pointer + snippet (default 400 chars). Includes state/updated_at in results;\n  freshness decisions are left to the agent.\n- Ranking policy (issue #69, docs/design/05):\n  - Primary sources (doc/code): relevance only (RRF/pgroonga score). Date sorting disabled.\n  - Secondary sources (issue/pr_review): relevance primary + state/updated_at tie-break.\n  - Pure date-replacement sort (discarding relevance via sort_by=updated_at/created_at): removed.\n  - sort_by defaults to "score" for backward compatibility. Non-"score" values limited to\n    secondary-source tie-break.\n  - Tie-break applied at pool stage (before top-k cutoff).\n  - At equal score, primary sources (doc/code) come before secondary via sentinel.\n  - sort_order="asc" inverts the entire compound key (closed→open, old→new).'
    
    # search.py - _legacy_sort
    if '結果リストを指定されたキーと順序でソートする' in jp:
        return 'Sort result list by the specified key and order (backward-compat wrapper).\n\ndeprecated: New code should use _rank_candidates.\nNon-"score" sort_by maintains legacy behavior of discarding relevance entirely.'
    
    return None

print('Done.')
