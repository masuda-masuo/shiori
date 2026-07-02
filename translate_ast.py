"""Translate Japanese docstrings using AST for reliable detection."""
import ast, re, os, textwrap

JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

def tr(s):
    return textwrap.dedent(s)

# =====================================================================
# English translations keyed by (fn, first_60_chars_of_docstring_body)
# =====================================================================
EN = {}

def reg(fn, jp_body_start, en_body):
    key = (fn, jp_body_start[:60])
    EN[key] = tr(en_body)

# ---- chunking.py ----
reg('chunking.py',
    'チャンク分割（詳細設計/02）。',
    'Chunk splitting (detailed design/02).\n\nDecisions:\n- docs: Split by heading, preserve heading paths in metadata.\n  Long sections use character-based splitting (default 1200 chars), prioritizing sentence boundaries.\n- issue/PR: Each comment is a natural unit, with `[title]` prepended as context prefix.\n- Language detected heuristically per chunk (effectively per file/comment) (ja/en).\n- code: Split by function/method/class via tree-sitter (map type: signature + docstring).\n  Unsupported languages fall back to _split_long_text (detailed design/10).\n')
reg('chunking.py',
    'max_chars を超えない最も近い意味的境界を探す。',
    'Find the closest semantic boundary not exceeding max_chars.\n\nAt the point this function is called, sentence splitting by _SENTENCE_END_RE\nhas already completed. This function handles punctuation embedded within segments\nor consumed by previous chunks, falling back to clause delimiters.\nPriority: sentence end marks, clause delimiters, hard cut at max_chars.\n')
reg('chunking.py',
    '識別子を snake_case / camelCase / PascalCase 境界で分割し、小文字スペース区切りで返す。',
    'Split identifiers at snake_case / camelCase / PascalCase boundaries and return space-separated lowercase.\n')
reg('chunking.py',
    'ソースコードを関数/メソッド/クラス単位でチャンク分割する（詳細設計/10 Step 2）。',
    'Chunk source code by function/method/class (detailed design/10 Step 2).\n')

# ---- db.py ----
reg('db.py',
    'データベーススキーマとクエリ（詳細設計/02）。',
    'Database schema and queries (detailed design/02).\n\nDecisions:\n- Chunks: 2 tables (doc + issue), distinguished by target_type.\n- pgvector index: HNSW.\n- pgroonga: TokenNgram (JP) + TokenBigram (EN).\n- Fallback: pg_trgm.\n')
reg('db.py',
    'CREATE EXTENSION IF NOT EXISTS vector;',
    'CREATE EXTENSION IF NOT EXISTS vector;')
reg('db.py',
    'テーブル・制約・btree 索引のみ作成。HNSW・pgroonga は作成しない。',
    'Create tables, constraints, and btree indexes only (no HNSW/pgroonga).')
reg('db.py',
    'pgroonga 索引を作成する。TokenMecab 優先、無ければ TokenNgram で。',
    'Create pgroonga indexes. Prefers TokenMecab; falls back to TokenNgram.')
reg('db.py',
    'HNSW と pgroonga 索引を作成する（issue #72）。',
    'Create HNSW and pgroonga indexes (issue #72). Avoided during bulk load.')
reg('db.py',
    'HNSW と pgroonga 索引を削除する（issue #72）。',
    'Drop HNSW and pgroonga indexes (issue #72). Temporarily during bulk load for performance.')
reg('db.py',
    'リポジトリごとの最終同期記録。age_seconds は DB 時計基準の経過秒数。',
    'Latest sync record per repo. age_seconds based on DB clock.')
reg('db.py',
    'source_type ごとのチャンク数（issue #31）。',
    'Chunk count by source_type (issue #31).')
reg('db.py',
    'issue_items の総行数（bot 含む全件。issue #31）。',
    'Total issue_item rows (includes bots; issue #31).')
reg('db.py',
    'PR の変更ファイルマップを取得する（issue #54）。',
    'Fetch PR change file map (issue #54).')
reg('db.py',
    'PR の変更ファイルマップを upsert する（issue #54）。',
    'Upsert PR change file map (issue #54).')
reg('db.py',
    'PR の保存済み head_sha を取得する（変更検知用。issue #54）。',
    'Get stored PR head_sha for change detection (issue #54).')
reg('db.py',
    'チャンクをバルク挿入する（executemany。issue #72）。',
    'Bulk insert chunks via executemany (issue #72).')

# ---- github_auth.py ----
reg('github_auth.py',
    'GitHub App の installation access token を発行・キャッシュする。',
    'Issues/caches GitHub App installation tokens.\nget_token() re-issues if within REFRESH_BEFORE seconds of expiry.\nEnsures tokens survive long ingests.\n')

# ---- github_sync.py ----
reg('github_sync.py',
    'データ取り込みと同期（詳細設計/01）。',
    'Data fetching and sync (detailed design/01).\n\nDecisions:\n- docs: git clone/pull; changed files re-indexed.\n- issue/PR: REST API cross-repo endpoints + `since` cursor.\n- Bot comments excluded; allowlist via SHIORI_INDEX_BOT_LOGINS (issue #25).\n- PR diffs not indexed; review comments include diff_hunk.\n- code shares same clone (issue #33).\n- PR change file maps are metadata only (issue #54).\n- Bulk path: ChunkBuffer (issue #72).\n- _git_fetch_ref / _git_delete_ref for PR head files (issue #81).\nAuth: TokenProvider abstraction (detailed design/09).\n')
reg('github_sync.py',
    'チャンクを蓄積し、バッチ埋め込み＋バルク挿入＋粗粒度 commit で高速化する。',
    'Accumulate chunks, batch-embed, bulk-insert, coarse-commit for high throughput.\nInitial/rebuild only (detailed design/01, 02, 10).\n')
reg('github_sync.py',
    'git の認証ヘッダを `-c http.extraHeader=...` 引数として返す。',
    'Return git auth header as `-c http.extraHeader=...` args.')
reg('github_sync.py',
    '指定 ref を shallow fetch し、一時 ref 名を返す（issue #81）。',
    'Shallow-fetch a ref and return a temp ref name (issue #81).')
reg('github_sync.py',
    'httpx の Auth フック。リクエストごとに provider からトークンを得て注入する。',
    'httpx Auth hook. Gets token from provider per request.')
reg('github_sync.py',
    'リポジトリの Markdown を同期し、変化分だけ索引する。戻り値は更新ファイル数。',
    'Sync repo Markdown; index only changed files. Returns update count.')
reg('github_sync.py',
    'リポジトリのソースコードを同期し、変化分だけ索引する（詳細設計/10 Step 3）。',
    'Sync repo source code; index only changed files (detailed design/10 Step 3).')
reg('github_sync.py',
    'PR の変更ファイルマップを同期する（issue #54）。',
    'Sync PR change file maps (issue #54).')
reg('github_sync.py',
    'issue / PR / コメント / レビューコメントを差分同期し索引する。',
    'Incremental sync of issues/PRs/comments/reviews.')
reg('github_sync.py',
    'チャンクをバッファに追加する。batch_size に達したら自動 flush。',
    'Add chunk to buffer. Auto-flushes at batch_size.')
reg('github_sync.py',
    'バッファをフラッシュ: 一括埋め込み → バルク挿入 → commit。戻り値は挿入件数。',
    'Flush buffer: batch embed → bulk insert → commit. Returns insert count.')

# ---- ingest.py ----
reg('ingest.py',
    'ingest ジョブ（詳細設計/01・07）。',
    'Ingest job (detailed design/01, 07).\nOn-demand: docker compose run --rm app python -m shiori ingest.\nAdvisory lock prevents concurrent execution (issue #6).\nFreshness: sync_runs (issue #22 / #33).\nSecurity: repo validated against SHIORI_REPOS (issue #63).\n')
reg('ingest.py',
    'バルク経路か判定する: rebuild=True または chunks テーブルが未存在（issue #72）。',
    'Determine if bulk path: rebuild=True or chunks table missing (issue #72).')

# ---- mcp_server.py ----
reg('mcp_server.py',
    'MCP サーバーの実装。エントリーポイント: main()、tool/fastmcp の登録。',
    'MCP server implementation.\nEntry point: main(), tool/fastmcp registration.\n~1100 lines: setup (1-90), helpers (90-310), tools (310-1100).\n')
reg('mcp_server.py',
    'バルク経路か判定する: rebuild=True または chunks テーブルが空／未存在（issue #72）。',
    'Determine bulk path: rebuild=True or chunks empty/missing (issue #72).')
reg('mcp_server.py',
    '差分同期の実体。ingest ツールと自動同期ループの両方から呼ばれる。',
    'Incremental sync body. Called by both ingest tool and auto-sync loop.\nProcess exclusion via _sync_lock.\n')
reg('mcp_server.py',
    'ファイル名がドキュメント拡張子か（大文字小文字無視）。',
    'Check if filename has a document extension (case-insensitive).')
reg('mcp_server.py',
    'ファイル名が除外拡張子か（大文字小文字無視）。',
    'Check if filename has an excluded extension (case-insensitive).')
reg('mcp_server.py',
    '拡張子が指定値にマッチするか（大文字小文字無視、\'.\' 有無両対応）。',
    "Check extension match (case-insensitive, with/without leading '.').")
reg('mcp_server.py',
    'クローンを walk し、コードファイルの相対パス集合を返す。',
    'Walk clone and return code file relative paths. Skips .git, node_modules, .venv, binary files.\n')
reg('mcp_server.py',
    '意味ベースの検索（入口ツール）。言い換え・概念・クロスリンガル（日本語クエリで英語ドキュメント）に強い。',
    'Semantic search (entry). Strong for paraphrasing, concept, cross-lingual queries. Hybrid with keyword.\n')
reg('mcp_server.py',
    'キーワード検索（日本語対応トークナイズ）。関数名・API 名・エラーコード・設定キーなど',
    'Keyword search (Japanese tokenize). Strong for exact matches.\n')
reg('mcp_server.py',
    '索引済みドキュメント＋コードファイルのパス一覧。path を渡すとその配下に絞る。',
    'List indexed doc/code file paths. Filter by path/source_type/extension.\n')
reg('mcp_server.py',
    '指定ファイルの全文（または start_line〜end_line の範囲）を取得する。',
    'Read file content (or range). From local clone, not index.\n')
reg('mcp_server.py',
    '1 件の issue を取得（内部ヘルパー）。未索引の場合は ValueError。',
    'Fetch a single issue (internal). Raises ValueError if not indexed.')
reg('mcp_server.py',
    'issue / PR のスレッド全体（本文＋コメント＋レビューコメント）を時系列で取得する。',
    'Fetch full issue/PR thread chronologically (body + comments + review).\n')
reg('mcp_server.py',
    'PR の変更ファイルマップ（メタデータ）を返す。ポインタ層のツール（issue #54, #100）。',
    'PR change file map (metadata pointer; issue #54, #100).\n')
reg('mcp_server.py',
    'PR head のファイル内容（または start_line〜end_line の範囲）を取得する。',
    'Read PR head file (or range). Uses git fetch; no working tree modification.\n')
reg('mcp_server.py',
    'docs / issue/PR / code を GitHub から同期し索引を更新する（入口ツール）。',
    'Sync docs/issues/code from GitHub and update index (entry). rebuild=True: full rebuild.\n')
reg('mcp_server.py',
    '索引の異常を検出して警告リストを返す（issue #31）。',
    'Detect index anomalies and return warning list (issue #31).')
reg('mcp_server.py',
    '索引の鮮度と健全性を返す。リポジトリごとに最終同期の完了時刻（last_synced_at）・',
    'Index freshness/health. Per-repo: last_synced_at, age_seconds, route, counts, warnings.\n')

# ---- search.py ----
reg('search.py',
    '検索オーケストレーション（ハイブリッド: 埋め込み＋キーワード）。詳細設計/03。',
    'Search orchestration (hybrid: embedding + keyword; detailed design/03).\nTwo-store: pgvector + pgroonga (fallback pg_trgm).\nHybrid: RRF fusion (0.5/0.5).\nPost-filter: language / source_type / repo / state / path_prefix / updated_after / prog_lang.\nTwo modes: simple (1 table) / complex (2 tables).\n')
reg('search.py',
    '埋め込み類似度＋キーワード類似度で候補をスコアリングし、ランク付けして返す。',
    'Score by embedding + keyword similarity and return ranked results.\nEmbedding: cosine distance. Keyword: pgroonga_score / pg_trgm.\nRRF: 0.5 + 0.5.\n')
reg('search.py',
    '主キー順にソートし、(target_type, target_id) で重複排除する。',
    'Sort by PK, dedup by (target_type, target_id).')
reg('search.py',
    'キーワード検索のエントリポイント。内部でハイブリッド検索として実行。',
    'Keyword search (hybrid internally). Supports all standard filters.\nsort_by/sort_order for backward compat; ranking always relevance-based (issue #69).\n')
reg('search.py',
    'セマンティック検索のエントリポイント。内部でハイブリッド検索として実行。',
    'Semantic search (hybrid internally). Identical filtering to keyword_search.\n')

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
    lines = text.split('\n')

    tree = ast.parse(text)
    file_replaced = 0

    for node in ast.walk(tree):
        if not isinstance(node, TYPES):
            continue
        name = getattr(node, 'name', '<module>')
        doc = ast.get_docstring(node)
        if not doc or not JP.search(doc):
            continue

        # Find the raw docstring text in the source
        body = node.body if hasattr(node, 'body') else []
        raw_text = None
        for stmt in body:
            if not isinstance(stmt, ast.Expr):
                continue
            val = stmt.value
            if not isinstance(val, ast.Constant) or not isinstance(val.value, str):
                continue
            # Check content overlap — ast.get_docstring cleans text, so match loosely
            raw_val = val.value
            # Get the source lines
            raw_lines = lines[stmt.lineno-1:stmt.end_lineno]
            raw_text_candidate = '\n'.join(raw_lines)
            if doc.strip() in raw_val or raw_val.strip() in doc:
                raw_text = raw_text_candidate
                q_match = re.match(r'^(\s*)("""|\'\'\')', raw_text_candidate)
                break

        if raw_text is None:
            print(f'  {fn}:{name} could not locate raw text')
            continue

        # Build key from body content
        body_start = doc.strip()[:60]
        key = (fn, body_start)
        if key not in EN:
            print(f'  {fn}:{name} MISSING {body_start!r}')
            continue

        new_body = EN[key]
        # Detect indentation and quote style
        m_indent = re.match(r'^(\s*)("""|\'\'\')', raw_text)
        if m_indent:
            indent = m_indent.group(1)
            q = m_indent.group(2)
        else:
            indent = ''
            q = '"""'

        # Build replacement
        en_lines = new_body.split('\n')
        result_lines = []
        for i, line in enumerate(en_lines):
            if i == 0:
                result_lines.append(indent + q + line)
            elif line.strip():
                result_lines.append(indent + line)
            else:
                result_lines.append('')
        result_lines[-1] += q
        replacement = '\n'.join(result_lines)

        if replacement == raw_text:
            print(f'  {fn}:{name} SAME')
            continue

        if raw_text not in text:
            print(f'  {fn}:{name} raw text not in file!')
            continue

        text = text.replace(raw_text, replacement, 1)
        file_replaced += 1
        print(f'  {fn}:{name} replaced')

    if text != original:
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f'{fn}: SAVED ({file_replaced})')
        total_replaced += file_replaced

print(f'Done: {total_replaced} total')
