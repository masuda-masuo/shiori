# AGENTS.md — Shiori プロジェクトのエージェント指示

## MCP ツールを使う前の基本ルール

**未知の MCP ツールを使うときは、まず Shiori でそのツールのドキュメントを検索すること。**
手探りで使わない。`shiori_search` でツール名＋"使い方" や "workflow" を検索し、
README や design.md の正規パターンを確認してから操作する。

## Shiori (shiori MCP)

- `shiori_search`: 意味検索の入口。まずこれで調べる
- `shiori_keyword_search`: 関数名・API 名・エラーコード等の厳密一致
- `shiori_read_issue`: issue/PR のスレッド全体を取得
- `shiori_read_file`: クローンされた実ファイルを読む
- `shiori_read_pr_file`: PR head のファイルを読む
- `shiori_ingest`: 索引が古いときに差分同期
- `shiori_status`: 索引の鮮度確認

## code-sandbox-mcp

**正規パターン: `run_container_and_exec` でワンショット実行**

```
run_container_and_exec(
    image="python@sha256:...",       # 省略可（デフォルトイメージ）
    commands=[
        "git clone https://github.com/user/repo.git /app",
        "cd /app && pip install -e '.[dev]'",
        "cd /app && pytest tests/ -v"
    ],
    allow_network=True,              # git clone に必須
    inject_vcs_token=True            # private リポジトリの認証に必要
)
```

- git clone するときは必ず `allow_network=True` + `inject_vcs_token=True`
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
