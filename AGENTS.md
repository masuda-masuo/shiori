# AGENTS.md — Shiori プロジェクトのエージェント指示

## MCP ツールを使う前の基本ルール

**未知の MCP ツールを使うときは、まず Shiori でそのツールのドキュメントを検索すること。**
手探りで使わない。`shiori_search` でツール名＋"使い方" や "workflow" を検索し、
README や design.md の正規パターンを確認してから操作する。

## Shiori (shiori MCP)

プロジェクトナレッジ検索 MCP。定義・ユースケースは `docs/design/13-プロダクト定義とユースケース.md`。

検索（どこに書いてある？）:

- `shiori_search`: 意味検索の入口。まずこれで調べる
- `shiori_keyword_search`: 関数名・API 名・エラーコード等の厳密一致
- `shiori_grep`: クローンの行レベル grep（検索で絞った後の Stage-2、`repo="*"` で横断）

閲覧（何と書いてある？）:

- `shiori_read_issue`: issue/PR のスレッド全体を取得
- `shiori_read_file`: クローンされた実ファイルを読む（範囲指定可）
- `shiori_read_pr_file`: PR head のファイルを読む
- `shiori_list_tree`: リポジトリ構造の閲覧（`source_type` / `extension` で絞り込み）

関係・変更（何とつながっている？何が変わる？）:

- `shiori_issue_links`: issue/PR の相互参照（closes / duplicate / refs / mention）
- `shiori_pr_changes`: PR の変更ファイルマップ
- `shiori_pr_diff`: PR の unified diff
- `shiori_pr_review_comments`: PR のレビューコメント一覧

運用:

- `shiori_status`: 索引の鮮度確認（自動同期が有効なら通常不要）

## code-sandbox-mcp

**正規パターン: `run_container_and_exec` でワンショット実行**

```
run_container_and_exec(
    image="python@sha256:...",       # 省略可（デフォルトイメージ）
    clone_repo="owner/repo",         # Shiori の既存クローンから cp -r（ネットワーク不要）
    clone_dest="/app",               # クローン先（既定 /tmp/repo）
    commands=[
        "cd /app && pip install -e '.[dev]'",
        "cd /app && pytest tests/ -v"
    ],
    allow_network=True,              # pip install に必須
    inject_vcs_token=True            # private リポジトリの認証に必要
)
```

- `clone_repo` を指定すると Shiori の既存クローンを `cp -r` でコピー（ネットワークなし、1秒未満）。詳細は `docs/design/12`。
- clone_repo がない場合: 明示的に `git clone https://...` + `allow_network=True` + `inject_vcs_token=True`
- clone できないときは `GIT_TERMINAL_PROMPT=0` で確認
- コンテナには `ripgrep` / `ast-grep` / `fd` が同梱済み（コード検索に使える）
- `sandbox_initialize` + `sandbox_exec` はセッションが長い場合のみ

## GitHub MCP

- PR 作成: `github_create_pull_request`
- ファイル操作: `github_create_or_update_file`, `github_push_files`
- イシュー操作: `github_issue_read`, `github_issue_write`

## プロジェクト固有

- テスト実行: `PYTHONPATH=src python3 -m pytest tests/ -v`
- 本番環境では `psycopg` が必要 → Docker Compose で PostgreSQL を起動
- リポジトリ: masuda-masuo/shiori, masuda-masuo/code-sandbox-mcp
- 索引更新（CLI）: `python -m shiori ingest`（`shiori_ingest` MCP ツールは廃止）
