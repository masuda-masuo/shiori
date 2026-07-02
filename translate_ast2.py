"""Translate Japanese docstrings using AST + position-based raw text extraction."""
import ast, re, os

JP = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]')
TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)

def tr(s):
    import textwrap; return textwrap.dedent(s)

# =====================================================================
# English translations keyed by (fn, first_60_chars_of_cleaned_docstring)
# =====================================================================
EN = {}

def reg(fn, doc_start, en_body):
    key = (fn, doc_start[:60])
    EN[key] = tr(en_body)

# ---- chunking.py ----
reg('chunking.py', 'チャンク分割（詳細設計/02）。',
    'Chunk splitting (detailed design/02).\n\nDecisions:\n- docs: Split by heading, preserve heading paths in metadata.\n  Long sections use character-based splitting (default 1200 chars), prioritizing sentence boundaries.\n- issue/PR: Each comment is a natural unit, with `[title]` prepended as context prefix.\n- Language detected heuristically per chunk (effectively per file/comment) (ja/en).\n- code: Split by function/method/class via tree-sitter (map type: signature + docstring).\n  Unsupported languages fall back to _split_long_text (detailed design/10).\n')
reg('chunking.py', 'max_chars を超えない最も近い意味的境界を探す。',
    'Find the closest semantic boundary not exceeding max_chars.\n\nAt this point, sentence splitting by _SENTENCE_END_RE has completed.\nHandles punctuation embedded within segments or consumed by previous chunks.\nFalls back to clause delimiters. Priority: sentence end marks → clause delimiters → hard cut.\n')
reg('chunking.py', '識別子を snake_case / camelCase / PascalCase 境界で分割し、小文字スペース区切りで返す。',
    'Split identifiers at snake_case / camelCase / PascalCase boundaries and return space-separated lowercase.\n')
reg('chunking.py', 'ソースコードを関数/メソッド/クラス単位でチャンク分割する（詳細設計/10 Step 2）。',
    'Chunk source code by function/method/class (detailed design/10 Step 2).\n')

# ---- db.py ----
reg('db.py', 'DB 接続とスキーマ。\n\n設計判断（詳細設計/04）:\n- docs / issue / pr_review / code は単一 DB を共有\n- pgvector で埋め込みクエリ\n- pgroonga で日英全文検索（TokenMecab/Mecab 優先）',
    'DB connection and schema (detailed design/04).\ndocs/issue/pr_review/code share a single DB.\npgvector for embedding queries.\npgroonga for JP/EN full-text search (TokenMecab/Mecab preferred).\n')
reg('db.py', 'CREATE EXTENSION IF NOT EXISTS vector;',
    'CREATE EXTENSION IF NOT EXISTS vector;')
reg('db.py', 'テーブル・制約・btree 索引のみ作成。HNSW はロード後に別途作成する。',
    'Create tables, constraints, and btree indexes only. HNSW created separately after load.')
reg('db.py', 'pgroonga 索引を作成する。TokenMecab 優先、無ければ TokenBigram にフォールバック。',
    'Create pgroonga indexes. Prefers TokenMecab; falls back to TokenBigram.')
reg('db.py', 'HNSW と pgroonga 索引を作成する（issue #72）。バルクロード中は避ける。',
    'Create HNSW and pgroonga indexes (issue #72). Avoided during bulk load.')
reg('db.py', 'HNSW と pgroonga 索引を削除する（issue #72）。バルクロード中は一時的に削除してパフォーマンス向上。',
    'Drop HNSW and pgroonga indexes (issue #72). Temporarily dropped during bulk load for performance.')
reg('db.py', '完全なスキーマ作成（テーブル＋全索引）。増分経路で使用（issue #72）。\n\nバルク経路では migrate_light() + ロード後 create_heavy_indexes() を使う。',
    'Full schema creation (tables + all indexes). Used in incremental path (issue #72).\nBulk path uses migrate_light() + create_heavy_indexes() after loading.\n')
reg('db.py', '同期の成功をリポジトリ単位で記録し、完了時刻（DB の now()）を返す（issue #22 / #33）。\n\nadvisory lock で skip された実行は呼び出し側で記録しないこと（成功した同期のみ）。\n時刻は DB の now() を使う: 複数経路・複数プロセスでも単一 DB の時計で一貫する。',
    "Record sync success per repo and return completion timestamp (DB's now()) (issue #22 / #33).\nSkipped executions (advisory lock) not recorded. Uses DB now() for cross-path consistency.\n")
reg('db.py', 'リポジトリごとの最終同期記録。age_seconds は DB 時計基準の経過秒数。',
    'Latest sync record per repo. age_seconds based on DB clock.')
reg('db.py', 'source_type ごとのチャンク数（issue #31）。',
    'Chunk count by source_type (issue #31).')
reg('db.py', 'issue_items の総行数（bot 含む全件。issue #31）。',
    'Total issue_item rows (includes bots; issue #31).')
reg('db.py', 'PR の変更ファイルマップを取得する（issue #54）。',
    'Fetch PR change file map (issue #54).')
reg('db.py', 'PR の変更ファイルマップを upsert する（issue #54）。同一 PR の既存行を削除してから insert。',
    'Upsert PR change file map (issue #54). Deletes existing entries for the same PR before insert.\n')
reg('db.py', 'PR の保存済み head_sha を取得する（変更検知用。issue #54）。',
    'Get stored PR head_sha for change detection (issue #54).')
reg('db.py', 'チャンクをバルク挿入する（executemany。issue #72）。',
    'Bulk insert chunks via executemany (issue #72).')

# ---- github_auth.py ----
reg('github_auth.py', 'GitHub 認証（詳細設計/09）。\n\nPAT と GitHub App installation token を TokenProvider 抽象で統一。\n\n決定事項:\n- App 優先、無ければ GITHUB_TOKEN、無ければ匿名（公開リポジトリのみ）\n- Installation token は 1 時間で期限切れ。5 分前から再発行\n- 秘密鍵は ingest プロセスにのみ渡す（MCP サーバーには渡さない; 詳細設計/07, 09）',
    'GitHub authentication (detailed design/09).\nUnifies PAT and GitHub App installation tokens under TokenProvider abstraction.\nDecisions:\n- App preferred, then GITHUB_TOKEN, then anonymous (public repos only).\n- Installation token expires in 1 hour; refreshes 5 min before expiry.\n- Private key only passed to ingest process (not MCP server; see detailed design/07, 09).\n')
reg('github_auth.py', 'トークン供給の抽象。get_token() は None を返したら匿名（認証なし）を意味する。',
    'Abstract token supplier. get_token() returning None means anonymous (no auth).')
reg('github_auth.py', '認証なし。公開リポジトリのみ（レート制限は厳しい）。',
    'No authentication. Public repos only (strict rate limits).')
reg('github_auth.py', '長期 PAT などの固定トークン。',
    'Static token, e.g. long-lived PAT.')
reg('github_auth.py', 'GitHub App の installation access token を発行・キャッシュする。\n\nget_token() は、未取得または expiry の REFRESH_BEFORE 秒前を過ぎていれば再発行する。\n長時間の ingest（CPU 埋め込みで 1 時間超があり得る）でも途中で失效しないようにする。',
    'Issues and caches GitHub App installation access tokens.\nget_token() re-issues if not obtained or within REFRESH_BEFORE seconds of expiry.\nEnsures tokens survive long ingests (CPU embedding can exceed 1 hour).\n')
reg('github_auth.py', '環境変数から TokenProvider を構築する。\n\n優先順位:\n1. SHIORI_GITHUB_APP_ID + SHIORI_GITHUB_APP_KEY + SHIORI_GITHUB_INSTALLATION_ID → AppTokenProvider\n2. GITHUB_TOKEN → StaticTokenProvider\n3. どちらも無い → AnonymousProvider',
    'Build TokenProvider from environment variables.\nPriority:\n1. SHIORI_GITHUB_APP_ID + _KEY + _INSTALLATION_ID → AppTokenProvider\n2. GITHUB_TOKEN → StaticTokenProvider\n3. Neither → AnonymousProvider\n')
reg('github_auth.py', 'JWT（RS256）を生成する。\n\ni  at はクロックスキュー吸収のため 60 秒過去に設定。exp は 9 分（10 分制限以下）。',
    'Generate JWT (RS256) for App authentication.\ni at set 60s in past to absorb clock skew. exp is 9 min (under 10-min limit).\n')

# ---- github_sync.py ----
reg('github_sync.py', 'データ取り込みと同期（詳細設計/01）。\n\n決定事項:\n- docs は git clone / pull。ファイル単位の content ハッシュを doc_files に保持し、変化したファイルだけ再チャンク・再埋め込みする。削除されたファイルの索引も消す。\n- issue/PR は REST API の repo 横断エンドポイント＋ `since`（updated_at カーソル）で差分同期\n- bot コメント（user.type == "Bot" または login が "[bot]" で終わる）は索引から除外する。ただし SHIORI_INDEX_BOT_LOGINS に列挙された login は allowlist として索引対象にする（issue #25）。\n- PR の diff 自体は索引しない。レビューコメントには diff_hunk を文脈として付与する。\n- code は sync_docs と同一クローンを共有し、sha デルタで変化ファイルのみ再索引する（issue #33）。\n- PR 変更ファイルマップはメタデータのみ同期・保持し、コンテンツ（patch）は GitHub MCP に委譲する（issue #54）。\n- 初回／rebuild のバルク経路では、ChunkBuffer により埋め込みをファイル横断でバッチ化し、チャンク挿入をバルク化＋commit を粗くする（issue #72）。\n- _git_fetch_ref / _git_delete_ref は PR head ファイル取得のための共通プリミティブ（issue #81）。\n認証は TokenProvider 抽象経由（詳細設計/09）。git は http.extraHeader でトークンを注入し、API は httpx の Auth フックでリクエスト毎に注入する。',
    'Data fetching and sync (detailed design/01).\n\nDecisions:\n- docs: git clone/pull; changed files re-chunked/re-embedded; deleted files removed from index.\n- issue/PR: REST API cross-repo endpoints + `since` (updated_at cursor) incremental sync.\n- Bot comments excluded; allowlist via SHIORI_INDEX_BOT_LOGINS (issue #25).\n- PR diffs not indexed; review comments include diff_hunk as context.\n- code: shares same clone; sha-delta re-indexes only changed files (issue #33).\n- PR change file maps: metadata only; content delegated to GitHub MCP (issue #54).\n- Bulk path: ChunkBuffer batches across files, bulk-inserts chunks, coarsens commits (issue #72).\n- _git_fetch_ref / _git_delete_ref: PR head file primitives (issue #81).\nAuth via TokenProvider (detailed design/09); git via http.extraHeader; API via httpx Auth hook.\n')
reg('github_sync.py', 'bot でも allowlist に含まれていれば索引対象とする（issue #25）。',
    'Allow indexing even for bot comments if login is in allowlist (issue #25).')
reg('github_sync.py', 'チャンクを蓄積し、バッチ埋め込み＋バルク挿入＋粗粒度 commit で高速化する。\n\n増分経路では使わず、初回／rebuild のバルク経路のみ。\n（詳細設計/01, 02, 10）',
    'Accumulate chunks, batch-embed, bulk-insert, coarse-commit for high throughput.\nIncremental path unused; initial/rebuild only (detailed design/01, 02, 10).\n')
reg('github_sync.py', 'URL に埋め込まれた認証情報（x-access-token:...@ 等）をマスクする。',
    'Mask auth credentials embedded in URLs (x-access-token:...@ etc.).')
reg('github_sync.py', 'git の認証ヘッダを `-c http.extraHeader=...` 引数として返す。\n\nトークンを clone URL からクリップする。',
    'Return git auth header as `-c http.extraHeader=...` args.\nToken clipped from clone URL.')
reg('github_sync.py', '指定 ref を shallow fetch し、一時 ref 名を返す（issue #81）。\n\ntmp_ref が None の場合は fetch しない。戻り値は fetch した ref の SHA。',
    'Shallow-fetch a ref and return a temp ref name (issue #81).\ntmp_ref=None skips fetch. Returns SHA of fetched ref.')
reg('github_sync.py', '一時 ref を削除する。存在しない場合は無視（issue #81）。',
    'Delete a temporary ref (issue #81). No-op if not found.')
reg('github_sync.py', 'httpx の Auth フック。リクエストごとに provider からトークンを得て注入する。\n\n長時間の ingest（CPU 埋め込みで 1 時間超があり得る）でも途中で失效しないように、\ntoken が期限切れ間近なら再取得する。',
    'httpx Auth hook. Gets token from provider per request.\nRefreshes token near expiry to survive long ingests.')
reg('github_sync.py', 'リポジトリの Markdown を同期し、変化分だけ索引する。戻り値は更新ファイル数。\n\nbuffer が指定された場合（バルク経路）、ChunkBuffer を使って一括埋め込み。',
    'Sync repo Markdown; index only changed files. Returns update count.\nWhen buffer specified (bulk path), uses ChunkBuffer for batch embedding.')
reg('github_sync.py', 'リポジトリのソースコードを同期し、変化分だけ索引する（詳細設計/10 Step 3）。\n\nsync_docs と同一クローンを使う。',
    'Sync source code; index only changed files (detailed design/10 Step 3).\nShares clone with sync_docs.')
reg('github_sync.py', '除外 glob パターンにマッチするか。',
    'Check if path matches excluded glob patterns.')
reg('github_sync.py', 'PR の変更ファイルマップを同期する（issue #54）。\n\nGET /repos/{repo}/pulls/{issue_number}/files',
    'Sync PR change file maps (issue #54).\nGET /repos/{repo}/pulls/{issue_number}/files')
reg('github_sync.py', 'issue / PR / コメント / レビューコメントを差分同期し索引する。\n\nbuffer が指定された場合（バルク経路）、ChunkBuffer を使って一括埋め込み。',
    'Incremental sync of issues/PRs/comments/reviews.\nWhen buffer specified (bulk path), uses ChunkBuffer for batch embedding.')
reg('github_sync.py', 'チャンクをバッファに追加する。batch_size に達したら自動 flush。',
    'Add chunk to buffer. Auto-flushes at batch_size.')
reg('github_sync.py', 'バッファをフラッシュ: 一括埋め込み → バルク挿入 → commit。戻り値は挿入件数。',
    'Flush buffer: batch embed → bulk insert → commit. Returns insert count.')

# ---- ingest.py ----
reg('ingest.py', 'ingest ジョブ（詳細設計/01・07）。\n\n決定: 同期はオンデマンド実行。\n    docker compose run --rm app python -m shiori ingest\nスケジュール実行が必要な場合はホスト側 cron 等から同コマンドを叩く。\n認証は build_token_provider で構築し、全リポジトリの同期で共有する（詳細設計/09）。\n\nプロセス横断排他（issue #6）:\n    PostgreSQL advisory lock (pg_try_advisory_lock) を使い、serve プロセスの自動同期や MCP ツール ingest との同時実行を防ぐ。\n    SYNC_LOCK_KEY は mcp_server.py と同じ値（0x5348494F = \'SHIO\'）。\n\n鮮度の記録（issue #22 / #33）:\n    リポジトリごとの同期完了時に sync_runs へ完了時刻と経路を記録する。\n    経路は環境変数 SHIORI_INGEST_ROUTE（既定 \'cli\'）。\n    reindex.yml（self-hosted runner）は \'runner\' を設定して実行経路を識別できるようにする。\n\nセキュリティ（issue #63）:\n    指定された repo を SHIORI_REPOS（allowlist）と照合し、含まれないものは拒否する。',
    'Ingest job (detailed design/01, 07).\nOn-demand: docker compose run --rm app python -m shiori ingest.\nAuth via build_token_provider shared across all repos (detailed design/09).\n\nProcess mutual exclusion (issue #6): PostgreSQL advisory lock prevents concurrent execution with serve auto-sync or MCP ingest.\n\nFreshness tracking (issue #22 / #33): Records completion to sync_runs per repo. Route via SHIORI_INGEST_ROUTE (default \'cli\').\n\nSecurity (issue #63): Validates repo against SHIORI_REPOS allowlist.\n')
reg('ingest.py', 'バルク経路か判定する: rebuild=True または chunks テーブルが未存在（issue #72）。',
    'Determine if bulk path: rebuild=True or chunks table missing (issue #72).')

# ---- mcp_server.py ----
reg('mcp_server.py', 'MCP サーバーの実装。エントリーポイント: main()、tool/fastmcp の登録。\n\nこのモジュールは長い（~1100 行）ため構造を以下に示す:\n1. サーバーセットアップ＆ライフサイクル（main, lifespan）— 1-90 行\n2. 内部ヘルパー（パス、拡張子、同期、検索）— 90-310 行\n3. ツール定義 — 310-1100 行\n\n各ツール関数は通常 100 行未満。\n',
    'MCP server implementation.\n~1100 lines: setup (1-90), helpers (90-310), tools (310-1100). Each tool typically <100 lines.\n')
reg('mcp_server.py', 'バルク経路か判定する: rebuild=True または chunks テーブルが空／未存在（issue #72）。',
    'Determine bulk path: rebuild=True or chunks empty/missing (issue #72).')
reg('mcp_server.py', '差分同期の実体。ingest ツールと自動同期ループの両方から呼ばれる。\n\nプロセス内排他: _sync_lock（threading.Lock）で逐次化。',
    'Incremental sync body. Called by both ingest tool and auto-sync loop.\nProcess-level exclusion via _sync_lock (threading.Lock).')
reg('mcp_server.py', 'ファイル名がドキュメント拡張子か（大文字小文字無視）。',
    'Check if filename has a document extension (case-insensitive).')
reg('mcp_server.py', 'ファイル名が除外拡張子か（大文字小文字無視）。',
    'Check if filename has an excluded extension (case-insensitive).')
reg('mcp_server.py', '拡張子が指定値にマッチするか（大文字小文字無視、\'.\' 有無両対応）。',
    'Check if extension matches given value (case-insensitive, with/without leading dot).')
reg('mcp_server.py', 'クローンを walk し、コードファイルの相対パス集合を返す。\n\n- .git / node_modules / .venv はスキップ\n- バイナリ拡張子のファイルもスキップ\n- _CODE_EXTENSIONS に含まれる拡張子のみ',
    'Walk clone and return code file relative paths.\nSkips .git/node_modules/.venv, binary extensions.\nOnly extensions in _CODE_EXTENSIONS.')
reg('mcp_server.py', '意味ベースの検索（入口ツール）。言い換え・概念・クロスリンガル（日本語クエリで英語ドキュメント）に強い。\n内部でキーワード検索とハイブリッド実行。',
    'Semantic search (entry). Strong for paraphrasing, concept, cross-lingual queries.\nHybrid with keyword search internally.')
reg('mcp_server.py', 'キーワード検索（日本語対応トークナイズ）。関数名・API 名・エラーコード・設定キーなど\n固有の文字列の厳密一致に強い。通常は意味ベース検索（semantic_search）から呼ばれる。',
    'Keyword search (Japanese tokenize). Strong for exact matches: function names, API names, error codes, config keys.\nUsually called via semantic_search.')
reg('mcp_server.py', '索引済みドキュメント＋コードファイルのパス一覧。path を渡すとその配下に絞る。\nリポジトリの構造を把握し、当たりをつけるのに使う。拡張子のバリデーションも行う。',
    'List indexed doc/code file paths. Filter by path/source_type/extension.\nUnderstand repo structure and locate files.')
reg('mcp_server.py', '指定ファイルの全文（または start_line〜end_line の範囲）を取得する。\n検索結果のポインタから本当に必要な範囲だけ読むこと。\nクローンからの読み取り。PR head は read_pr_file または GitHub MCP から取得。',
    'Read full file (or range) from clone (not index).\nPR head files via read_pr_file or GitHub MCP.')
reg('mcp_server.py', '1 件の issue を取得（内部ヘルパー）。未索引の場合は ValueError。',
    'Fetch single issue (internal helper). Raises ValueError if not indexed.')
reg('mcp_server.py', 'issue / PR のスレッド全体（本文＋コメント＋レビューコメント）を時系列で取得する。\nbot コメントも含まれる（is_bot で識別可能）。',
    'Fetch full issue/PR thread chronologically (body + comments + review).\nBot comments included (identifiable via is_bot).')
reg('mcp_server.py', 'PR の変更ファイルマップ（メタデータ）を返す。ポインタ層のツール（issue #54, #100）。\n\n保持するもの（メタデータ）:\n  - head_sha: PR の head コミット SHA（force-push 追従用）\n  - ファイル一覧: path / status / additions / deletions / changes / blob_url\n\n保持しないもの（コンテンツ）:\n  - patch hunk 全文 → GitHub MCP の pull_request_read(method=\'get_diff\') で取得\n  - PR head のファイル全文 → shiori_read_pr_file で取得（推奨）、または GitHub MCP で取得',
    'PR change file map (metadata pointer; issue #54, #100).\nStored: head_sha / path / status / additions / deletions / changes / blob_url\nNot stored: patch hunks (via GitHub MCP) and PR head files (via shiori_read_pr_file).')
reg('mcp_server.py', 'PR head のファイル内容（または start_line〜end_line の範囲）を取得する。\nread_file から委譲される（デフォルトブランチ用）→ PR 用に上書き。',
    'Read PR head file content (or range). Delegated from read_file with PR-specific fetch.')
reg('mcp_server.py', 'docs / issue/PR / code を GitHub から同期し索引を更新する（入口ツール）。\n\nrebuild=True は索引を破棄して全件作り直し。\nSHIORI_ALLOW_REBUILD=true 環境変数が必要（MCP ツールからは既定で無効; issue #63）。\nchunks テーブルが空の場合も rebuild 扱いになる。',
    'Sync docs/issues/code from GitHub and update index (entry).\nrebuild=True: discard and full rebuild (requires SHIORI_ALLOW_REBUILD=true; issue #63).\nAlso treated as rebuild when chunks table is empty.')
reg('mcp_server.py', '索引の異常を検出して警告リストを返す（issue #31）。',
    'Detect index anomalies and return warning list (issue #31).')
reg('mcp_server.py', '索引の鮮度と健全性を返す。リポジトリごとに最終同期の完了時刻（last_synced_at）・経過秒数（age_seconds）・実行経路・チャンク数内訳・issue_items 件数・差分同期カーソル・警告を返す。',
    'Index freshness and health. Per-repo: last_synced_at, age_seconds, route, counts, items, cursor, warnings.')

# ---- search.py ----
reg('search.py', '検索オーケストレーション（ハイブリッド: 埋め込み＋キーワード）。詳細設計/03。\n\nこのモジュールは検索の外部 API。semantic_search / keyword_search を提供する。\n\n2 ストア設計:\n  - 埋め込み検索（pgvector）: cosine similarity、常にクエリ時に実行\n  - キーワード検索（pgroonga）: トークナイズ済み全文検索、マイグレーション時に索引作成\n    pgroonga が利用できない場合は pg_trgm（pg_trgm.word_similarity）にフォールバック。\n    このフォールバックが docker-compose.yml に pg_trgm 拡張がある主な理由。\n\nハイブリッド検索: RRF（Reciprocal Rank Fusion）で結果を統合（詳細設計/03）。\n  - 埋め込みとキーワードの結果を設定可能な重みで統合。\n  - 事後フィルタ: language / source_type / repo / state / path_prefix。\n  - 重複排除: (target_type, target_id)。\n\n2 つの実行モード:\n  - 単純パス（1 テーブル）: 単一 kNN + 事前フィルタ。select_target_ids_simple\n  - 複雑パス（2 テーブル）: source_type/repo の組み合わせで kNN → 集約。select_target_ids_complex',
    'Search orchestration (hybrid: embedding + keyword; detailed design/03).\nTwo-store: pgvector (embedding, cosine similarity) + pgroonga (FTS, falls back to pg_trgm).\nHybrid: RRF fusion with configurable weights. Post-filter: language/source_type/repo/state/path_prefix.\nTwo modes: simple (1 table, single kNN) / complex (2 tables, kNN per combo → agg).')
reg('search.py', '埋め込み類似度＋キーワード類似度で候補をスコアリングし、ランク付けして返す（詳細設計/03）。\n\n埋め込み類似度: cosine distance (1 - cosine)。\nキーワード類似度: pgroonga_score（0-1）利用可能時、なければ pg_trgm word_similarity（0-1）。\nRRF 重み: 埋め込み 0.5、キーワード 0.5。',
    'Score by embedding + keyword similarity; return ranked results (detailed design/03).\nEmbedding: cosine distance (1-cosine). Keyword: pgroonga_score/pg_trgm.\nRRF: 0.5 embedding + 0.5 keyword.')
reg('search.py', '主キー順にソートし、(target_type, target_id) で重複排除する。\nselect_target_ids_simple/complex の戻り値順序を安定化するためのソート。',
    'Sort by PK, dedup by (target_type, target_id). Stable ordering for simple/complex paths.')
reg('search.py', 'キーワード検索のエントリポイント。内部でハイブリッド検索として実行。\n\n対応フィルタ:\n  source_type / language / state / repo / path_prefix / updated_after / prog_lang\n  top_k（既定 20）\n\nsort_by / sort_order パラメータは後方互換のため受け付けるが、ランキングは常に関連度主（issue #69）。\n  一次ソース（doc/code）: スコア順。\n  二次ソース（issue/pr_review）: スコア順 + state/updated_at tie-break。\n  純粋な日付置換ソートは行わず、既定（score）のまま使うことを推奨。\n\n戻り値は Hit オブジェクトのリスト。',
    'Keyword search entry (hybrid internally). Supports standard filters.\nsort_by/sort_order for backward compat; ranking always relevance-based (issue #69).\nReturns list of Hit objects.')
reg('search.py', 'セマンティック検索のエントリポイント。内部でハイブリッド検索として実行。\n\nkeyword_search と同一のフィルタリング。',
    'Semantic search entry (hybrid internally). Identical filtering to keyword_search.')

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

        # Find the EXACT docstring statement in the body
        raw_text = None
        for stmt in node.body:
            if not isinstance(stmt, ast.Expr):
                continue
            val = stmt.value
            if not isinstance(val, ast.Constant) or not isinstance(val.value, str):
                continue
            # Take the raw lines from the source
            raw_lines = lines[stmt.lineno-1:stmt.end_lineno]
            raw_text = '\n'.join(raw_lines)
            break

        if raw_text is None:
            print(f'  {fn}:{name} could not locate')
            continue

        # Use cleaned docstring first 60 chars as key
        body_start = doc.strip()[:60]
        key = (fn, body_start)
        if key not in EN:
            print(f'  {fn}:{name} MISSING {body_start!r}')
            continue

        new_body = EN[key]

        # Detect indentation and quote style from raw text
        m = re.match(r'^(\s*)("""|\'\'\')', raw_text)
        if m:
            indent = m.group(1)
            q = m.group(2)
        else:
            indent = ''
            q = '"""'

        # Build replacement with same indentation
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
            continue

        if raw_text not in text:
            print(f'  {fn}:{name} raw text vanished!')
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
