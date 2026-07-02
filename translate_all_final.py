"""Complete translation of ALL remaining Japanese docstrings.

This script reads each .py file under src/shiori/ (except already-translated ones),
extracts triple-quoted strings via regex, checks for Japanese text content,
and replaces each Japanese docstring with a pre-defined English translation.

The English translations are keyed by a fingerprint (first 40 chars of the docstring content).
"""
import re, os, textwrap

# Regex to match triple-quoted strings (""" or ''')  
DOC_RE = re.compile(r'''(?P<q>"""|\'\'\')(?P<body>(?:\\.|[^\\])*?)(?P=q)''', re.DOTALL)

# Japanese character range (hiragana, katakana, kanji — NOT CJK punctuation)
JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Translations keyed by (filename, first_40_chars_of_docstring_body)
# The body is the inner content of the docstring (without quotes)
# ---------------------------------------------------------------------------
T = {}

def tr(fn, body_start, body):
    """Register translation for a docstring whose first 40 chars match body_start.
    body is the new English text (without quotes, without outer indent).
    Leading/trailing whitespace of body_start is stripped for matching."""
    T[(fn, body_start.strip()[:40])] = textwrap.dedent(body)

# ====== chunking.py ======
tr('chunking.py',
    'チャンク分割（詳細設計/02）。',
    """
Chunk splitting (detailed design/02).

Decisions:
- docs: Split by heading, preserve heading paths in metadata.
  Long sections use character-based splitting (default 1200 chars), prioritizing sentence boundaries.
- issue/PR: Each comment is a natural unit, with `[title]` prepended as context prefix.
- Language detected heuristically per chunk (effectively per file/comment) (ja/en).
- code: Split by function/method/class via tree-sitter (map type: signature + docstring).
  Unsupported languages fall back to _split_long_text (detailed design/10).
""")

tr('chunking.py',
    'max_chars を超えない最も近い意味的境界を探す。',
    'Find the closest semantic boundary not exceeding max_chars.\n\nAt the point this function is called, sentence splitting by _SENTENCE_END_RE\nhas already completed. Punctuation marks are already used by\n_SENTENCE_END_RE for splitting, so this function finds punctuation in the\nfollowing cases:\n  1. Within text split by non-punctuation conditions, where\n     punctuation remains inside the segment.\n  2. When punctuation was consumed by the previous chunk and the current s\n     has none → fall back to clause delimiters\n\nIn practice, **clause-delimiter fallback** is the main role.\n\nPriority order:\n1. Sentence-ending marks: end of sentence\n2. Clause delimiters: clause boundary\n3. Hard-cut at max_chars if none found\n')

tr('chunking.py',
    'tree-sitter でシンボル（関数／メソッド／クラス）単位に分割する。',
    'Split by symbols (function/method/class) via tree-sitter.\n\nSupported languages and their tree-sitter query files:\n  Python (.py), TypeScript (.ts, .tsx), Go (.go), Rust (.rs)\nQuery file name: queries/{language}-symbols.scm\n(detailed design/10).\n')

tr('chunking.py',
    'コードチャンクを文書化可能な単位に変換する。',
    'Convert code chunks to documentable units.\n\nConverts named blocks from source code into map-type chunks.\n')

# ====== db.py ======
tr('db.py',
    'データベーススキーマとクエリ（詳細設計/02）。',
    'Database schema and queries (detailed design/02).\n\nSee query comments for table and column descriptions.\nOnly design decisions are recorded here.\n\nDecisions:\n- Chunks use 2-table structure (doc and issue) distinguished by target_type.\n- pgvector index: HNSW (higher accuracy and faster build than IVFFlat)\n- pgroonga index: Japanese tokenization (TokenNgram) + English tokenization (TokenBigram)\n- Full-text search fallback: pg_trgm (for environments without pgroonga)\n')

tr('db.py',
    '既存 DB に対する冪等な ALTER。CREATE TABLE IF NOT EXISTS では新カラ',
    'Idempotent ALTER for existing DB. CREATE TABLE IF NOT EXISTS does not add new columns.\n\nCalled by both migrate_light and migrate.\n')

tr('db.py',
    '完全なスキーマ作成（テーブル＋全索引）。増分経路で使用（issue #72）。',
    'Full schema creation (tables + all indexes). Used in incremental path (issue #72).\n\nBulk path uses migrate_light() + create_heavy_indexes() after loading.\n')

tr('db.py',
    '同期の成功をリポジトリ単位で記録し、完了時刻（DB の now()）を返す',
    "Record a successful sync per repository and return the completion timestamp (DB's now()) (issue #22 / #33).\n\nExecutions skipped by advisory lock are NOT recorded (only successful syncs).\nTimestamp uses DB now(): consistent across multiple paths and processes using a single DB clock.\n")

tr('db.py',
    'PR 変更ファイルマップを upsert する（issue #54）。同一 PR の既存行',
    'Upsert PR change file map (issue #54). Deletes existing entries for the same PR first, then inserts new ones.\n')

tr('db.py',
    '_create_pgroonga_index: pgroonga の全文検索インデックスを作成する。',
    '_create_pgroonga_index: create pgroonga full-text search index.\n\nFalls back to pg_trgm when pgroonga extension is unavailable.\n')

tr('db.py',
    '直近の同期実行記録を返す（issue #22 / #33）。',
    'Return recent sync execution records (issue #22 / #33).\n')

tr('db.py',
    'リポジトリごとのチャンク数を集計する。',
    'Aggregate chunk counts per repository.\n')

tr('db.py',
    'issue_item の総数を返す（高速パス: COUNT は使わず reltuples を利用）',
    'Return total issue_item count (fast path: uses reltuples instead of COUNT).\n')

tr('db.py',
    'PR head の SHA を取得する。',
    'Get PR head SHA.\n\nReturns head_sha from pr_changes table.\nUsed to detect force-pushes on PRs.\n')

# ====== github_auth.py ======
tr('github_auth.py',
    'GitHub App の installation access token を発行・キャッシュする。',
    'Issues and caches GitHub App installation access tokens.\n\nget_token() re-issues if not yet obtained or within REFRESH_BEFORE seconds of expiry.\nEnsures tokens do not expire mid-operation even during long ingests (CPU embedding can exceed 1 hour).\n')

# ====== github_sync.py ======
tr('github_sync.py',
    'データ取り込みと同期（詳細設計/01）。',
    'Data fetching and sync (detailed design/01).\n\nDecisions:\n- docs: git clone/pull. Per-file content hash tracked in doc_files;\n  only changed files are re-chunked and re-embedded. Deleted file indexes removed.\n- issue/PR: REST API cross-repo endpoints + `since` (updated_at cursor) incremental sync:\n    - GET /repos/{o}/{r}/issues            (includes PRs, body)\n    - GET /repos/{o}/{r}/issues/comments    (issue/PR comments)\n    - GET /repos/{o}/{r}/pulls/comments     (review comments, path/line/diff_hunk)\n- Bot comments (user.type=="Bot" or login ends with "[bot]") excluded from index.\n  Logins in SHIORI_INDEX_BOT_LOGINS allowlisted (issue #25).\n  Raw data in issue_items is_bot=true.\n- PR diffs not indexed. Review comments include diff_hunk as context.\n- code shares same clone as sync_docs; sha-delta re-indexes only changed files (issue #33).\n- PR change file maps synced as metadata only; content (patch) delegated to GitHub MCP (issue #54).\n- Initial/rebuild bulk path: ChunkBuffer batches embeddings across files,\n  bulk-inserts chunks and coarsens commits (issue #72).\n- _git_fetch_ref / _git_delete_ref: common primitives for PR head files (issue #81).\n  Read files from arbitrary refs without modifying working tree.\nAuthentication via TokenProvider abstraction (detailed design/09).\nGit injects tokens via http.extraHeader; API via httpx Auth hook per request.\n')

tr('github_sync.py',
    'チャンクをバッファに追加する。batch_size に達したら自動 flush。',
    'Add chunk to buffer. Auto-flushes when batch_size is reached.')

tr('github_sync.py',
    'バッファをフラッシュ: 一括埋め込み → バルク挿入 → commit。戻り値は',
    'Flush buffer: batch embed → bulk insert → commit. Returns insertion count.')

tr('github_sync.py',
    'バッファ付きチャンク挿入器。初回／rebuild のバルク経路で使用。',
    'Buffered chunk inserter for bulk paths (initial/rebuild).\nBatches across files, bulk-inserts chunks, coarsens commits (issue #72).\n')

tr('github_sync.py',
    '認証情報を git の追加引数として組み立てる。',
    'Build authentication info as git extra args.\ngit -c http.extraHeader="Authorization: bearer <token>" ...\n\nIndependent of TokenProvider implementation (env var / GitHub CLI / OIDC).\n')

tr('github_sync.py',
    'refs/pull/{N}/head を fetch し SHA を返す。',
    'Fetch refs/pull/{N}/head and return its SHA.\n\nDoes NOT modify working tree. Use with git cat-file, clean up via _git_delete_ref.\n\nRetry: httpx network errors retried up to _FETCH_RETRIES times.\n')

tr('github_sync.py',
    'httpx の Auth フック。各リクエストの Authorization ヘッダを設定する',
    'httpx Auth hook. Sets the Authorization header on each request.')

tr('github_sync.py',
    'ドキュメント（Markdown）の同期。',
    'Sync documentation (Markdown).\n\nInternal steps:\n  1. git clone (first) / git pull (subsequent)\n  2. Compute content hash per file, compare with doc_files sha\n  3. Re-chunk and re-embed only changed files\n  4. Delete doc_files rows for deleted files\n  5. Mark completion via DocFilesRepository.sync_completed\n\nChunk embedding and sha update in same DB transaction.\nAuthentication independent of TokenProvider.\n')

tr('github_sync.py',
    'コードファイルの同期（issue #33）。',
    'Sync code files (issue #33).\n\nUses same clone as sync_docs; sha-delta re-indexes only changed files.\n\nOnly runs when SHIORI_INDEX_CODE=true. Skipped when false/not set.\n')

tr('github_sync.py',
    'PR 変更ファイルマップの同期（issue #54）。',
    'Sync PR change file maps (issue #54).\n\nFetches changed files per PR from GitHub API, upserts into pr_changes.\nStored: head_sha / path / status / additions / deletions / changes / blob_url\nNot stored: patch hunks (via GitHub MCP) and PR head files (via shiori_read_pr_file).\n')

tr('github_sync.py',
    'issue/PR スレッドの同期。',
    'Sync issue/PR threads.\n\nIncremental sync via REST API cross-repo endpoints + `since` cursor.\nFirst sync: no since (all items). Subsequent: previous updated_at as cursor.\nBot comments stored with is_bot flag, excluded from indexing.\n')

# ====== ingest.py ======
tr('ingest.py',
    'ingest ジョブ（詳細設計/01・07）。',
    'Ingest job (detailed design/01, 07).\n\nDecision: Sync is on-demand execution.\n    docker compose run --rm app python -m shiori ingest\nFor scheduled execution, trigger the same command from host cron etc.\nAuthentication via build_token_provider shared across all repos (detailed design/09).\n\nProcess mutual exclusion (issue #6):\n    Uses PostgreSQL advisory lock (pg_try_advisory_lock) to prevent concurrent execution\n    with the serve process auto-sync or MCP tool ingest.\n    SYNC_LOCK_KEY matches mcp_server.py (0x5348494F = \'SHIO\').\n\nFreshness tracking (issue #22 / #33):\n    Records completion timestamp and route to sync_runs per repository.\n    Route is from SHIORI_INGEST_ROUTE env var (default \'cli\').\n    reindex.yml (self-hosted runner) sets \'runner\'.\n\nSecurity (issue #63):\n    Validates specified repo against SHIORI_REPOS (allowlist); rejects unlisted repos.\n')

tr('ingest.py',
    'バルク経路か判定する: rebuild=True または chunks テーブルが未存在（i',
    "Determine if this is a bulk path: rebuild=True or chunks table doesn't exist (issue #72).")

# ====== mcp_server.py ======
tr('mcp_server.py',
    'MCP サーバーの実装。エントリーポイント: main()、tool/fastmcp の登録',
    'MCP server implementation. Entry point: main(), tool/fastmcp registration.\n\nThis module is long (~1100 lines) and structured as follows:\n  1. Server setup & lifecycle (main, lifespan) — lines 1-90\n  2. Internal helpers (paths, extensions, sync) — lines 90-310\n  3. Tool definitions — lines 310-1100 (each tool has its own span)\n\nEach tool function is typically under 100 lines.\n')

tr('mcp_server.py',
    'バルク経路か判定する: rebuild=True または chunks テーブルが空／未存',
    'Determine if this is a bulk path: rebuild=True or chunks table is empty/non-existent (issue #72).')

tr('mcp_server.py',
    'ファイル名がドキュメント拡張子か（大文字小文字無視）。',
    'Check if filename has a document extension (case-insensitive).')

tr('mcp_server.py',
    'ファイル名が除外拡張子か（大文字小文字無視）。',
    'Check if filename has an excluded extension (case-insensitive).')

tr('mcp_server.py',
    '拡張子が指定値にマッチするか（大文字小文字無視、\'.\' 有無両対応）',
    "Check if extension matches the given value (case-insensitive, with/without leading '.').")

tr('mcp_server.py',
    '1 件の issue を取得（内部ヘルパー）。未索引の場合は ValueError。',
    'Fetch a single issue (internal helper). Raises ValueError if not indexed.')

tr('mcp_server.py',
    '索引の異常を検出して警告リストを返す（issue #31）。',
    'Detect index anomalies and return warning list (issue #31).')

tr('mcp_server.py',
    '同期・ingest タスクを逐次実行する。全て成功なら True、そうでなければ',
    'Execute sync and ingest tasks sequentially. Returns True if all succeeded.\nMultiple repos synced sequentially. wrap_ingest_error catches and logs exceptions.\n')

tr('mcp_server.py',
    'クローン済みリポジトリのコードファイルをディスク上から走査する。_COD',
    'Walk code files on disk for a cloned repository. Returns relative paths matching _CODE_EXTENSIONS.\nDotfiles and binary-extension files excluded.\n')

tr('mcp_server.py',
    'セマンティック検索の MCP ツール。内部で search.semantic_search を呼',
    'Semantic search MCP tool. Calls search.semantic_search internally with fallback for missing args.')

tr('mcp_server.py',
    'キーワード検索の MCP ツール。内部で search.keyword_search を呼ぶ。',
    'Keyword search MCP tool. Calls search.keyword_search internally.')

tr('mcp_server.py',
    '索引済みドキュメント／コードファイルのパス一覧を返す。path/source_ty',
    'List indexed doc/code file paths, optionally filtered by path/source_type/extension.')

tr('mcp_server.py',
    'ファイルシステムのクローンからファイル内容を読み取る（索引ではなく）',
    'Read file content from the filesystem clone (not from the index).\n\nReads from local clone (main branch fixed). No index required.\nPR head files: use read_pr_file or GitHub MCP.')

tr('mcp_server.py',
    'issue/PR スレッド（本文＋全コメント）を時系列で取得する。',
    'Read an issue/PR thread (body + all comments) ordered by timestamp.\n\nFetches from the index. Raises ValueError if not indexed.\nexclude_noise_bots=True excludes bots outside SHIORI_INDEX_BOT_LOGINS (issue #44).\n')

tr('mcp_server.py',
    'PR 変更ファイルマップ（メタデータのみ）。ポインタを返す: head_sha ',
    'PR change file map (metadata only). Returns pointer: head_sha + file list.\n\nStored: head_sha / path / status / additions / deletions / changes / blob_url\nNot stored: patch hunks (via GitHub MCP) and PR head files (via shiori_read_pr_file).\n')

tr('mcp_server.py',
    'PR head のファイル内容を透過的に取得する（git fetch 経由）。',
    'Read PR head file content (transparently via git fetch).\n\nFetches refs/pull/{N}/head temporarily, reads file, cleans up.\nDoes NOT modify working tree.\n')

tr('mcp_server.py',
    'ingest MCP ツール。リポジトリ（または全リポジトリ）の docs/issues/c',
    'Ingest MCP tool. Syncs docs/issues/code for a repo (or all repos).\n\nWraps github_sync.py logic.\nrebuild=True: discard and rebuild index (requires SHIORI_ALLOW_REBUILD=true, issue #63).\n')

tr('mcp_server.py',
    '索引の鮮度と健全性。リポジトリごとの同期情報と警告を返す。',
    'Index freshness and health status. Returns per-repo sync info and warnings.\n\nUsed by LLM to check if index is up-to-date before searching.\n')

# ====== search.py ======
tr('search.py',
    '検索オーケストレーション（ハイブリッド: 埋め込み＋キーワード）。詳細',
    'Search orchestration (hybrid: embedding + keyword). Detailed design/03.\n\nThis module is the external API for search.\n\nTwo-store design:\n  - Embedding search (pgvector): cosine similarity, query-time only.\n  - Keyword search (pgroonga): tokenized FTS, index at migration time.\n    Falls back to pg_trgm.word_similarity when pgroonga unavailable.\n    This is why docker-compose.yml includes pg_trgm.\n\nHybrid search via RRF (detailed design/03):\n  - Merges embedding + keyword results with configurable weight.\n  - Post-filter: language / source_type / repo / state / path_prefix.\n  - Deduplication by (target_type, target_id).\n\nTwo execution modes:\n  - Simple (1 chunk table): single kNN with pre-filtering.\n    Conditions: <=1 source_type, <=1 repo, no language filter.\n    select_target_ids_simple handles this.\n  - Complex (2 chunk table): kNN per (source_type, repo) → aggregator.\n    select_target_ids_complex handles this.\n')

tr('search.py',
    '埋め込み類似度＋キーワード類似度で候補をスコアリングし、ランク付けし',
    'Score candidates by embedding + keyword similarity and return ranked results (detailed design/03).\n\nEmbedding: cosine distance (1 - cosine).\nKeyword: pgroonga_score (0-1) / pg_trgm word_similarity (0-1).\nRRF weight: 0.5 embedding, 0.5 keyword.\n')

tr('search.py',
    '主キー順にソートし、(target_type, target_id) で重複排除する。',
    'Sort by (source_type, source_id) and deduplicate by (target_type, target_id).\n')

tr('search.py',
    'キーワード検索のエントリポイント。内部でハイブリッド検索として実行さ',
    'Keyword search entry point. Hybrid search internally.\n\nFilters: source_type / language / state / repo / path_prefix / updated_after / prog_lang / top_k (default 20)\nsort_by/sort_order accepted for backward compat; ranking always relevance-based (issue #69).\n')

tr('search.py',
    'セマンティック検索のエントリポイント。内部でハイブリッド検索として実',
    'Semantic search entry point. Hybrid search internally.\n\nIdentical filtering to keyword_search.\n')


# ==================================================================
def translate():
    src = os.path.join(HERE, 'src', 'shiori')
    for fn in sorted(os.listdir(src)):
        if not fn.endswith('.py') or fn in ('config.py', 'embedding.py', '__init__.py', '__main__.py'):
            continue
        fpath = os.path.join(src, fn)
        with open(fpath, encoding='utf-8') as f:
            text = f.read()
        original = text

        # Find all triple-quoted strings using regex
        for m in DOC_RE.finditer(text):
            body = m.group('body')
            if not JP.search(body):
                continue
            # Find matching translation by first 40 chars
            key = (fn, body.strip()[:40])
            if key not in T:
                print(f'  MISSING: {key}')
                continue
            q = m.group('q')
            raw = m.group(0)
            # Detect indentation (leading whitespace before the docstring on its line)
            start = m.start()
            line_start = text.rfind('\n', 0, start)
            if line_start < 0:
                indent = ''
            else:
                indent = text[line_start + 1:start]
            new_en = T[key]
            # Build replacement
            en_lines = new_en.split('\n')
            result = []
            for i, line in enumerate(en_lines):
                if i == 0:
                    result.append(indent + q + line)
                elif line.strip():
                    result.append(indent + line)
                else:
                    result.append('')
            result[-1] = result[-1] + q
            replacement = '\n'.join(result)
            if replacement != raw:
                text = text[:m.start()] + replacement + text[m.end():]
                print(f'  {fn}: replaced ({key[1][:30]}...)')
            else:
                print(f'  {fn}: skipped (same content)')

        if text != original:
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(text)
            print(f'{fn}: SAVED')
        else:
            print(f'{fn}: no changes')

if __name__ == '__main__':
    translate()
