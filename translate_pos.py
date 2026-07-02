"""Translate Japanese docstrings to English — position-based matching.

For each file, finds JP docstrings via regex (in order of appearance).
Replaces each with pre-defined English translation.
Applies in reverse order to preserve positions.
"""
import re, os, textwrap

DOC_RE = re.compile(r'''(?P<q>"""|\'\'\')(?P<body>(?:\\.|[^\\])*?)(?P=q)''', re.DOTALL)
JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')

def tr(s):
    return textwrap.dedent(s)

def collect_jp_docs(text):
    """Return list of (start, end, q, raw) for each JP docstring in order."""
    docs = []
    for m in DOC_RE.finditer(text):
        if JP.search(m.group('body')):
            docs.append((m.start(), m.end(), m.group('q'), m.group(0)))
    return docs

def make_replacement(raw, q, indent, new_body):
    lines = new_body.split('\n')
    result = []
    for i, line in enumerate(lines):
        if i == 0:
            result.append(indent + q + line)
        elif line.strip():
            result.append(indent + line)
        else:
            result.append('')
    result[-1] += q
    return '\n'.join(result)

def get_indent(text, pos):
    line_start = text.rfind('\n', 0, pos)
    if line_start < 0:
        return ''
    return text[line_start + 1:pos]

# =====================================================================
# Translations (in order of JP docstring appearance per file)
# =====================================================================
SRC = {}  # fn -> [translation strings]

SRC['chunking.py'] = [
    tr("""Chunk splitting (detailed design/02).

Decisions:
- docs: Split by heading, preserve heading paths in metadata.
  Long sections use character-based splitting (default 1200 chars), prioritizing sentence boundaries.
- issue/PR: Each comment is a natural unit, with `[title]` prepended as context prefix.
- Language detected heuristically per chunk (effectively per file/comment) (ja/en).
- code: Split by function/method/class via tree-sitter (map type: signature + docstring).
  Unsupported languages fall back to _split_long_text (detailed design/10).
"""),
    tr("""Identify symbols in snake_case / camelCase / PascalCase.
"""),
    tr("""Split source code into function/method/class-level symbols (detailed design/10 Step 3).
"""),
]

SRC['db.py'] = [
    tr("""Database connection and schema. Design decisions (detailed design/04):
- docs / issue: share a single DB.
- pgvector used for embedding queries.
- pgroonga used for Japanese/English full-text search (TokenMecab/Mecab preferred).
"""),
    tr("""CREATE EXTENSION IF NOT EXISTS vector;
"""),
    tr("""Create tables, constraints, and btree indexes only (no HNSW or pgroonga).
"""),
    tr("""Create pgroonga indexes. Prefers TokenMecab; falls back to TokenNgram.
"""),
    tr("""Create HNSW and pgroonga indexes (issue #72). Avoided during bulk load.
"""),
    tr("""Drop HNSW and pgroonga indexes (issue #72). Temporarily during bulk load for performance.
"""),
    tr("""Latest sync record per repository. age_seconds based on DB clock.
"""),
    tr("""Chunk count by source_type (issue #31).
"""),
    tr("""Total issue_item rows (includes bots; issue #31).
"""),
    tr("""Fetch PR change file map (issue #54).
"""),
    tr("""Upsert PR change file map (issue #54).
"""),
    tr("""Fetch stored head_sha for change detection (issue #54).
"""),
    tr("""Bulk insert chunks via executemany (issue #72).
"""),
]

SRC['github_sync.py'] = [
    tr("""Data fetching and sync (detailed design/01).

Decisions:
- docs: git clone/pull. Per-file content hash; only changed files re-indexed.
- issue/PR: REST API cross-repo endpoints + `since` cursor for incremental sync.
- Bot comments excluded from index (allowlist via SHIORI_INDEX_BOT_LOGINS; issue #25).
- PR diffs not indexed; review comments include diff_hunk as context.
- code shares same clone as sync_docs (issue #33).
- PR change file maps are metadata only (issue #54).
- Bulk path: ChunkBuffer batches across files (issue #72).
- _git_fetch_ref / _git_delete_ref: PR head file primitives (issue #81).
Authentication via TokenProvider abstraction (detailed design/09).
"""),
    tr("""Accumulate chunks, batch-embed, bulk-insert, coarse-commit for high throughput (detailed design/01, 02, 10).
"""),
    tr("""Git auth header as `-c http.extraHeader=...` args.
"""),
    tr("""Shallow fetch a given ref and return a temp ref name (issue #81).
"""),
    tr("""httpx Auth hook. Gets token from provider per request.
"""),
    tr("""Sync repository Markdown. Returns number of updated files.
"""),
    tr("""Sync source code (detailed design/10 Step 2).
"""),
    tr("""Sync PR change file maps (issue #54).
"""),
    tr("""Incremental sync of issues/PRs/comments/reviews.
"""),
    tr("""Add chunk to buffer; auto-flush at batch_size.
"""),
    tr("""Flush buffer: batch embed + bulk insert + commit.
"""),
]

SRC['ingest.py'] = [
    tr("""Ingest job (detailed design/01, 07).

On-demand execution via docker compose run. Authentication via build_token_provider.
Advisory lock prevents concurrent execution (issue #6).
Freshness tracking via sync_runs (issue #22 / #33).
Repo validation against SHIORI_REPOS allowlist (issue #63).
"""),
    tr("""Determine if bulk path: rebuild=True or chunks table doesn't exist (issue #72).
"""),
]

SRC['mcp_server.py'] = [
    tr("""MCP server implementation. Entry point: main(), tool/fastmcp registration.

Long module (~1100 lines):
  1. Server setup & lifecycle — lines 1-90
  2. Internal helpers (paths, extensions, sync, search) — lines 90-310
  3. Tool definitions — lines 310-1100 (each tool under 100 lines)
"""),
    tr("""Determine bulk path: rebuild=True or chunks table empty/non-existent (issue #72).
"""),
    tr("""Incremental sync body. Called by ingest tool and auto-sync loop.

Sequentially calls sync_issues / sync_docs / sync_code for each repo.
wrap_ingest_error catches and logs exceptions.
"""),
    tr("""Check if filename has document extension (case-insensitive).
"""),
    tr("""Check if filename has excluded extension (case-insensitive).
"""),
    tr("""Check if extension matches value (case-insensitive, with/without leading dot).
"""),
    tr("""Walk clone and return set of code file relative paths.

Skips dotfiles, binary extensions; filters by _CODE_EXTENSIONS.
"""),
    tr("""Semantic search (entry tool). Handles paraphrasing, concept, cross-lingual queries.
"""),
    tr("""Keyword search (entry tool). Strong for exact matches: function names, API names, error codes, config keys, etc.
"""),
    tr("""List indexed doc/code file paths. Filter by path/source_type/extension. Validates extension arg.
"""),
    tr("""Read file content (or range) from clone. Not from index.
"""),
    tr("""Fetch single issue (internal). ValueError if not indexed.
"""),
    tr("""Fetch full issue/PR thread chronologically (body + comments + review comments).
"""),
    tr("""PR change file map (metadata pointer; issue #54). Returns head_sha and file list.
"""),
    tr("""Read PR head file (or range) via git fetch. No working tree modification.
"""),
    tr("""Sync docs/issues/code and update index. rebuild=True triggers full rebuild.
"""),
    tr("""Index freshness and health. Returns per-repo warnings (issue #31).
"""),
    tr("""Per-repo latest sync info with warning list.
"""),
]

SRC['search.py'] = [
    tr("""Search orchestration (hybrid: embedding + keyword; detailed design/03).

Entry point for semantic_search / keyword_search.

Two-store design: pgvector embedding + pgroonga (jp/en) full-text search.
Fallback to pg_trgm when pgroonga unavailable.
Hybrid search: RRF with 0.5/0.5 weights.
Post-filter: source_type / language / repo / state / path_prefix / updated_after / prog_lang.
Two execution modes: simple (1 table) / complex (2 tables).
"""),
    tr("""Score by embedding + keyword similarity, return ranked hits.

Embedding: cosine distance (1-cosine). Keyword: pgroonga_score (0-1) or pg_trgm word_similarity (0-1).
RRF: 0.5 embedding + 0.5 keyword. Sorted by score desc, dedup by (target_type, target_id).
"""),
    tr("""Sort and dedup by (target_type, target_id). Returns in insertion order.
"""),
    tr("""Keyword search entry (hybrid internally). Supports all standard filters.
sort_by/sort_order accepted for backward compat; ranking always relevance-based (issue #69).
"""),
    tr("""Semantic search entry (hybrid internally). Identical filtering to keyword_search.
"""),
]

SRC['github_auth.py'] = [
    tr("""Issues and caches GitHub App installation access tokens.

get_token() re-issues if expiry within REFRESH_BEFORE seconds.
Ensures tokens don't expire mid-operation during long ingests.
"""),
]

# =================================================================
# Apply translations
# =================================================================
src_dir = os.path.join(os.path.dirname(__file__), 'src', 'shiori')
skipped = {'config.py', 'embedding.py', '__init__.py', '__main__.py'}

for fn in sorted(os.listdir(src_dir)):
    if not fn.endswith('.py') or fn in skipped:
        continue
    fpath = os.path.join(src_dir, fn)
    with open(fpath, encoding='utf-8') as f:
        text = f.read()
    
    jp_docs = collect_jp_docs(text)
    if not jp_docs:
        print(f'{fn}: no JP docstrings')
        continue
    
    if fn not in SRC:
        print(f'MISSING TRANSLATIONS: {fn} ({len(jp_docs)} JP docstrings)')
        continue
    
    trans = SRC[fn]
    if len(trans) != len(jp_docs):
        print(f'COUNT MISMATCH: {fn}: {len(jp_docs)} JP vs {len(trans)} EN')
        continue
    
    # Apply in reverse order to maintain positions
    original = text
    for i in range(len(jp_docs) - 1, -1, -1):
        start, end, q, raw = jp_docs[i]
        indent = get_indent(text, start)
        replacement = make_replacement(raw, q, indent, trans[i])
        if replacement != raw:
            text = text[:start] + replacement + text[end:]
    
    if text != original:
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f'{fn}: SAVED ({len(jp_docs)} replaced)')
    else:
        print(f'{fn}: no changes')
