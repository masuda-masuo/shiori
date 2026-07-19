# Agent Instructions

## Shiori — ハイブリッド検索・ナレッジベース

Shiori は GitHub リポジトリの docs / issues / PRs / source code を検索対象とする RAG システム。

## ツール早見表（したいこと → 使うツール）

| したいこと | 使うツール | 備考 |
|---|---|---|
| リポジトリ構造の把握 | `shiori_list_tree` | path / source_type / extension で絞り込み |
| コード・ドキュメント検索（意味） | `shiori_search` | embedding + keyword ハイブリッド。概念検索に |
| コード・ドキュメント検索（キーワード） | `shiori_keyword_search` | 関数名・API名・エラーコードの完全一致に |
| ファイル内容の参照 | `shiori_read_file` | clone から直接読み取り。start_line/end_line で範囲指定可 |
| issue/PR スレッドの参照 | `shiori_read_issue` | 本文 + 全コメント + review を時系列 |
| PR の変更ファイル一覧 | `shiori_pr_changes` | head_sha / status / additions / deletions |
| PR の head ファイル内容 | `shiori_read_pr_file` | PR 番号指定で head ブランチのファイル読み取り |
| 索引の更新 | `shiori_ingest` | diff sync（通常秒単位）。rebuild は要環境変数 |
| 索引の状態確認 | `shiori_status` | last_synced_at / age / counts / warnings |

## リポジトリ操作

| 操作 | 使うツール | 禁止 |
|---|---|---|
| コード検索・読み取り | `shiori_search` / `shiori_read_file` / `shiori_read_issue` | bash の `git clone` + ローカルの `read`/`grep` |
| コード作成・全上書き | sunaba の `write_file`（新規作成 / 全上書き。部分更新不可） | ローカルの `write` |
| コード編集（部分） | sunaba の `edit_file`（`old_str` / 行範囲 / append、`.py` は AST 自動解決） / `transform_file`（パターン一括） | ローカルの `edit` |
| 編集の undo | sunaba の `undo_file_edit`（各編集のスナップショットから復元、redo 可能） | — |
| テスト・検証 | sunaba の `verify_in_container`（lint + type check + テスト一括） | bash で直接 pytest |
| git push / PR | sunaba の `publish`（checkpoint を squash して push、create_pr で PR） | bash で直接 git / gh |

コードの読み取り・検索は **Shiori の専用ツール**、編集・検証・公開は **sunaba の sandbox ツール** のみ使う。bash でローカルに clone して直接 read/write するのは禁止。

## Issue/PR 起票ルール

Issue や PR を起票する際は、**冒頭に `Written by:` で記述者と使用モデルを明記する**。
全エージェントが同じ GitHub token (`code-sandbox-mcp[bot]`) を使うため、git log だけでは誰が書いたか判別できない。

### フォーマット

- 通常: `Written by: OpenCode (DeepSeek V4 Flash)`
- 複数関与: `Written by: OpenCode (DeepSeek V4 Flash), based on discussion with Claude`

### 記述すべき項目

| 項目 | 例 | 理由 |
|------|-----|------|
| クライアント | OpenCode / Claude Code / agnostic | どのUIから書かれたか |
| モデル | DeepSeek V4 Flash / DeepSeek V4 Flash Max / Claude Sonnet / Fable 5 など | 事後的な品質評価・原因特定に必要 |
| 人間の関与 | 人間が編集した箇所があれば "edited by masuda" | 最終判断者を明示 |

- shiori の正規クローン: `/home/masuda/shiori/`（systemd + update.sh 対象）
