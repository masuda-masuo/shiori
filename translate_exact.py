"""Translate remaining Japanese docstrings — exact text matching.

For each remaining file, finds JP docstrings via regex, extracts exact raw text,
and replaces with English translations using str.replace.
"""
import re, os, textwrap

DOC_RE = re.compile(r'''(?P<q>"""|\'\'\')(?P<body>(?:\\.|[^\\])*?)(?P=q)''', re.DOTALL)
JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')

def tr(s):
    return textwrap.dedent(s)

def get_indent(text, pos):
    line_start = text.rfind('\n', 0, pos)
    return text[line_start + 1:pos] if line_start >= 0 else ''

def make_replacement(q, indent, body):
    lines = body.split('\n')
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

def raw_fingerprint(raw):
    """Return ~60-char fingerprint of raw text (including first line)."""
    first_line = raw.split('\n')[0]
    return first_line[:60]

# =====================================================================
# Exact Japanese raw text → English body translations
# We match by finding each JP docstring via regex, then looking up
# the English body in this dict by the raw text's fingerprint.
# =====================================================================
EN = {}  # fingerprint -> new body

def reg(fn, jp_raw_fingerprint, en_body):
    """Register translation. jp_raw_fingerprint: first line of raw text (~60 chars)."""
    key = (fn, jp_raw_fingerprint)
    EN[key] = tr(en_body)

# ---- chunking.py ----
reg('chunking.py', '"""チャンク分割（詳細設計/02）。',
    'Chunk splitting (detailed design/02).\n\nDecisions:\n- docs: Split by heading, preserve heading paths in metadata.\n  Long sections use character-based splitting (default 1200 chars), prioritizing sentence boundaries.\n- issue/PR: Each comment is a natural unit, with `[title]` prepended as context prefix.\n- Language detected heuristically per chunk (effectively per file/comment) (ja/en).\n- code: Split by function/method/class via tree-sitter (map type: signature + docstring).\n  Unsupported languages fall back to _split_long_text (detailed design/10).\n')
reg('chunking.py', '    """識別子を snake_case / camelCase / PascalCase',
    'Split identifiers at snake_case / camelCase / PascalCase boundaries and return space-separated lowercase.\n')
reg('chunking.py', '    """ソースコードを関数/メソッド/クラス単位でチャンク分割する（詳細設計/10 Step 2）。',
    'Chunk source code by function/method/class (detailed design/10 Step 2).\n')

# ---- github_sync.py ----
reg('github_sync.py', '    """チャンクを蓄積し、バッチ埋め込み＋バルク挿入＋粗粒度 commit で高速化する。',
    'Accumulate chunks, batch-embed, bulk-insert, and coarse-commit for high throughput (detailed design/01, 02, 10). Not used in incremental path; initial/rebuild only.\n')
reg('github_sync.py', '    """git の認証ヘッダを `-c http.extraHeader=...` 引数として返す。',
    'Return git auth header as `-c http.extraHeader=...` args. Token clipped from clone URL.\n')
reg('github_sync.py', '    """指定 ref を shallow fetch し、一時 ref 名を返す（issue #81）。',
    'Shallow-fetch a given ref and return a temporary ref name (issue #81). Returns None on failure.\n')
reg('github_sync.py', '    """httpx の Auth フック。リクエストごとに provider からトークンを得て注入する。',
    'httpx Auth hook. Gets token from provider per request. Long-ingest-safe refresh.\n')
reg('github_sync.py', '    """リポジトリの Markdown を同期し、変化分だけ索引する。戻り値は更新ファイル数。',
    'Sync repository Markdown; index only what changed. Returns updated file count. Buffer for bulk embedding when specified.\n')
reg('github_sync.py', '    """リポジトリのソースコードを同期し、変化分だけ索引する（詳細設計/10 Step 3）。',
    'Sync repository source code; index only what changed (detailed design/10 Step 3). Shares clone with sync_docs.\n')
reg('github_sync.py', '    """PR の変更ファイルマップを同期する（issue #54）。',
    'Sync PR change file maps (issue #54). Fetches via GET /repos/{repo}/pulls/{num}/files.\n')
reg('github_sync.py', '    """issue / PR / コメント / レビューコメントを差分同期し索引する。',
    'Incremental sync of issues/PRs/comments/reviews. Buffer for bulk path when specified.\n')

# ---- mcp_server.py ----
reg('mcp_server.py', '    """差分同期の実体。ingest ツールと自動同期ループの両方から呼ばれる。',
    'Incremental sync body. Called by both ingest tool and auto-sync loop. Process-level exclusion via _sync_lock (threading.Lock).\n')
reg('mcp_server.py', '    """ファイル名がドキュメント拡張子か（大文字小文字無視）。\"\"\"',
    'Check if filename has a document extension (case-insensitive).')
reg('mcp_server.py', '    """ファイル名が除外拡張子か（大文字小文字無視）。\"\"\"',
    'Check if filename has an excluded extension (case-insensitive).')
reg('mcp_server.py', '    """拡張子が指定値にマッチするか（大文字小文字無視、\'.\' 有無両対応）。\"\"\"',
    'Check if extension matches the given value (case-insensitive, with/without leading dot).')
reg('mcp_server.py', '    """クローンを walk し、コードファイルの相対パス集合を返す。',
    'Walk clone and return set of code file relative paths. Skips .git / node_modules / .venv and binary extensions.\n')
reg('mcp_server.py', '    """意味ベースの検索（入口ツール）。言い換え・概念・クロスリンガル（日本語クエリで英語ドキュメント）に強い。',
    'Semantic search (entry tool). Strong for paraphrasing, concept, and cross-lingual queries. Internal hybrid with keyword search.\n')
reg('mcp_server.py', '    """キーワード検索（日本語対応トークナイズ）。関数名・API 名・エラーコード・設定キーなど',
    'Keyword search (Japanese-aware tokenization). Strong for exact matches: function names, API names, error codes, config keys. Usually called via semantic_search.\n')
reg('mcp_server.py', '    """索引済みドキュメント＋コードファイルのパス一覧。path を渡すとその配下に絞る。',
    'List indexed doc/code file paths. Filter by path/source_type/extension. Use to understand repo structure and locate files to read.\n')
reg('mcp_server.py', '    """指定ファイルの全文（または start_line〜end_line の範囲）を取得する。',
    'Read full file content (or start_line-end_line range). Reads from local clone (not index). Get pointers from search results.\n')

# Remaining mcp_server.py entries
reg('mcp_server.py', '    """1 件の issue を取得（内部ヘルパー）。未索引の場合は ValueError。\"\"\"',
    'Fetch a single issue (internal helper). Raises ValueError if not indexed.')
reg('mcp_server.py', '    """issue / PR のスレッド全体（本文＋コメント＋レビューコメント）を時系列で取得する。',
    'Fetch full issue/PR thread chronologically (body + comments + review comments). Bot comments included with is_bot flag.\n')
reg('mcp_server.py', '    """PR の変更ファイルマップ（メタデータ）を返す。ポインタ層のツール（issue #54, #100）。',
    'PR change file map (metadata pointer tool; issue #54, #100). Returns head_sha + file list (path/status/changes/additions/deletions/blob_url). Content via GitHub MCP or shiori_read_pr_file.\n')
reg('mcp_server.py', '    """PR head のファイル内容（または start_line〜end_line の範囲）を取得する。',
    'Read PR head file content (or range). Delegated from read_file (default branch) with PR-specific fetch.\n')
reg('mcp_server.py', '    """docs / issue/PR / code を GitHub から同期し索引を更新する（入口ツール）。',
    'Sync docs/issues/code from GitHub and update index (entry tool). rebuild=True: discard index and full rebuild.\n')
reg('mcp_server.py', '    """索引の鮮度と健全性を返す。リポジトリごとに最終同期の完了時刻（last_synced_at）・',
    'Index freshness and health. Per-repo: last_synced_at, age_seconds, execution route, chunk counts, total items, sync cursor, and warnings (issue #31).\n')
reg('mcp_server.py', '    """リポジトリごとの最終同期情報と警告リストを返す。',
    'Return per-repo latest sync info with warning list.\n')

# ---- search.py ----
reg('search.py', '"""検索オーケストレーション（ハイブリッド: 埋め込み＋キーワード）。詳細設計/03。',
    'Search orchestration (hybrid: embedding + keyword; detailed design/03).\n\nEntry point for semantic_search / keyword_search.\n\nTwo-store design: pgvector embedding + pgroonga (jp/en) full-text search.\nFallback to pg_trgm when pgroonga unavailable.\nHybrid search: RRF with configurable weights (0.5/0.5).\nPost-filter: source_type / language / repo / state / path_prefix / updated_after / prog_lang.\nTwo execution modes: simple (1 table) / complex (2 tables).\n')
reg('search.py', '    """埋め込み類似度＋キーワード類似度で候補をスコアリングし、ランク付けして返す。',
    'Score candidates by embedding + keyword similarity and return ranked results (detailed design/03).\n\nEmbedding: cosine distance (1-cosine). Keyword: pgroonga_score (0-1) or pg_trgm word_similarity (0-1).\nRRF: 0.5 embedding + 0.5 keyword.\n')
reg('search.py', '    """主キー順にソートし、(target_type, target_id) で重複排除する。',
    'Sort by primary key order and deduplicate by (target_type, target_id). Returns stable ordering.\n')
reg('search.py', '    """キーワード検索のエントリポイント。内部でハイブリッド検索として実行。',
    'Keyword search entry (hybrid internally). Supports all filters. sort_by/sort_order for backward compat; ranking always relevance-based (issue #69).\n')
reg('search.py', '    """セマンティック検索のエントリポイント。内部でハイブリッド検索として実行。',
    'Semantic search entry (hybrid internally). Identical filtering to keyword_search.\n')

# =================================================================
print(f'Registered {len(EN)} translations')
src_dir = os.path.join(os.path.dirname(__file__), 'src', 'shiori')
skipped = {'config.py', 'embedding.py', '__init__.py', '__main__.py'}
total_replaced = 0

for fn in sorted(os.listdir(src_dir)):
    if not fn.endswith('.py') or fn in skipped:
        continue
    fpath = os.path.join(src_dir, fn)
    with open(fpath, encoding='utf-8') as f:
        text = f.read()
    original = text
    file_replaced = 0

    for m in DOC_RE.finditer(text):
        raw = m.group(0)
        body = m.group('body')
        if not JP.search(body):
            continue
        fp = raw_fingerprint(raw)
        key = (fn, fp)
        if key not in EN:
            print(f'  {fn}: MISSING {fp}')
            continue
        new_body = EN[key]
        indent = get_indent(text, m.start())
        q = m.group('q')
        replacement = make_replacement(q, indent, new_body)
        if replacement != raw:
            text = text.replace(raw, replacement, 1)
            file_replaced += 1
        else:
            print(f'  {fn}: SAME {fp}')
    
    if text != original:
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f'{fn}: SAVED ({file_replaced} replaced)')
        total_replaced += file_replaced

print(f'Done: {total_replaced} replacements total')
