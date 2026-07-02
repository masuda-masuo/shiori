"""Translate remaining Japanese docstrings to English."""
import re, os

JP = re.compile(r'[\u3000-\u9fff]')

OLDS = {}
NEWS = {}

# github_sync.py — module docstring
OLDS['github_sync.py:module'] = "'''\nデータ取り込みと同期（詳細設計/01）。\n\n決定事項:\n- docs は git clone / pull。ファイル単位の content ハッシュを doc_files に保持し、\n  変化したファイルだけ再チャンク・再埋め込みする。削除されたファイルの索引も消す。\n- issue/PR は REST API の repo 横断エンドポイント＋ `since`（updated_at カーソル）で差分同期:\n    - GET /repos/{o}/{r}/issues            (PR を含む。本文)\n    - GET /repos/{o}/{r}/issues/comments    (issue/PR コメント)\n    - GET /repos/{o}/{r}/pulls/comments     (レビューコメント。path/line/diff_hunk 付き)\n- bot コメント（user.type == \"Bot\" または login が \"[bot]\" で終わる）は索引から除外する。\n  ただし SHIORI_INDEX_BOT_LOGINS に列挙された login は allowlist として索引対象にする（issue #25）。\n  生データは issue_items に is_bot=true で保持する（read_issue では表示する）。\n- PR の diff 自体は索引しない。レビューコメントには diff_hunk を文脈として付与する。\n- code は sync_docs と同一クローンを共有し、sha デルタで変化ファイルのみ再索引する（issue #33）。\n- PR 変更ファイルマップはメタデータのみ同期・保持し、コンテンツ（patch）は GitHub MCP に委譲する（issue #54）。\n- 初回／rebuild のバルク経路では、ChunkBuffer により埋め込みをファイル横断でバッチ化し、\n  チャンク挿入をバルク化＋commit を粗くする（issue #72）。\n- _git_fetch_ref / _git_delete_ref は PR head ファイル取得のための共通プリミティブ（issue #81）。\n  ワーキングツリーを変更せずに任意の ref からファイルを読み取るために使う。\n認証は TokenProvider 抽象経由（詳細設計/09）。git は http.extraHeader でトークンを注入し、\nAPI は httpx の Auth フックでリクエスト毎に注入する。\n'''"

NEWS['github_sync.py:module'] = "'''\nData fetching and sync (detailed design/01).\n\nDecisions:\n- docs: git clone/pull. Per-file content hash tracked in doc_files;\n  only changed files are re-chunked and re-embedded. Deleted file indexes are removed.\n- issue/PR: REST API cross-repo endpoints + `since` (updated_at cursor) for incremental sync:\n    - GET /repos/{o}/{r}/issues            (includes PRs, body)\n    - GET /repos/{o}/{r}/issues/comments    (issue/PR comments)\n    - GET /repos/{o}/{r}/pulls/comments     (review comments, with path/line/diff_hunk)\n- Bot comments (user.type == \"Bot\" or login ends with \"[bot]\") are excluded from indexing.\n  Logins listed in SHIORI_INDEX_BOT_LOGINS are allowlisted for indexing (issue #25).\n  Raw data is stored in issue_items with is_bot=true (shown in read_issue).\n- PR diffs themselves are not indexed. Review comments include diff_hunk as context.\n- code shares the same clone as sync_docs; only files with changed sha are re-indexed (issue #33).\n- PR change file maps are synced and stored as metadata only; content (patch) is delegated to GitHub MCP (issue #54).\n- On initial/rebuild bulk path, ChunkBuffer batches embeddings across files,\n  bulk-inserts chunks and coarsens commits (issue #72).\n- _git_fetch_ref / _git_delete_ref are common primitives for fetching PR head files (issue #81).\n  Used to read files from arbitrary refs without modifying the working tree.\nAuthentication goes through the TokenProvider abstraction (detailed design/09).\nGit injects tokens via http.extraHeader; the API injects tokens per-request via httpx Auth hook.\n'''"

# ChunkBuffer.add
OLDS['github_sync.py:add'] = '        """チャンクをバッファに追加する。batch_size に達したら自動 flush。"""'
NEWS['github_sync.py:add'] = '        """Add chunk to buffer. Auto-flushes when batch_size is reached."""'

# ChunkBuffer.flush
OLDS['github_sync.py:flush'] = '        """バッファをフラッシュ: 一括埋め込み → バルク挿入 → commit。戻り値は挿入件数。"""'
NEWS['github_sync.py:flush'] = '        """Flush buffer: batch embed → bulk insert → commit. Returns insertion count."""'

# mcp_server.py
OLDS['mcp_server.py:_is_bulk_path'] = '    """バルク経路か判定する: rebuild=True または chunks テーブルが空／未存在（issue #72）。"""'
NEWS['mcp_server.py:_is_bulk_path'] = '    """Determine if this is a bulk path: rebuild=True or chunks table is empty/non-existent (issue #72)."""'

OLDS['mcp_server.py:_is_doc_file'] = '    """ファイル名がドキュメント拡張子か（大文字小文字無視）。"""'
NEWS['mcp_server.py:_is_doc_file'] = '    """Check if filename has a document extension (case-insensitive)."""'

OLDS['mcp_server.py:_is_excluded_file'] = '    """ファイル名が除外拡張子か（大文字小文字無視）。"""'
NEWS['mcp_server.py:_is_excluded_file'] = '    """Check if filename has an excluded extension (case-insensitive)."""'

OLDS['mcp_server.py:_match_extension'] = '    """拡張子が指定値にマッチするか（大文字小文字無視、\'.\' 有無両対応）。"""'
NEWS['mcp_server.py:_match_extension'] = "    \"\"\"Check if extension matches the given value (case-insensitive, with/without leading '.').\"\"\""

OLDS['mcp_server.py:_read_issue_single'] = '    """1 件の issue を取得（内部ヘルパー）。未索引の場合は ValueError。"""'
NEWS['mcp_server.py:_read_issue_single'] = '    """Fetch a single issue (internal helper). Raises ValueError if not indexed."""'

OLDS['mcp_server.py:_build_warnings'] = '    """索引の異常を検出して警告リストを返す（issue #31）。"""'
NEWS['mcp_server.py:_build_warnings'] = '    """Detect index anomalies and return warning list (issue #31)."""'

def translate():
    src = os.path.join(os.path.dirname(__file__), 'src', 'shiori')
    for fname in sorted(os.listdir(src)):
        if not fname.endswith('.py'):
            continue
        fpath = os.path.join(src, fname)
        with open(fpath, encoding='utf-8') as f:
            text = f.read()
        old_text = text
        for key in sorted(OLDS):
            kfname = key.split(':', 1)[0]
            if kfname != fname:
                continue
            old = OLDS[key]
            new = NEWS[key]
            if old in text:
                text = text.replace(old, new, 1)
                print(f'  {key}: replaced')
            else:
                print(f'  {key}: NOT FOUND (looking for {old[:50]!r}...)')
        if text != old_text:
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(text)
            print(f'{fname}: saved')
        else:
            print(f'{fname}: no changes')

if __name__ == '__main__':
    translate()
