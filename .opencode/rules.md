# Project Rules

ルールは `.opencode/rules/` 配下に分割されている。`opencode.jsonc` の `instructions` で glob 一括読み込み。

| ファイル | 内容 |
|---|---|
| `rules/refactoring.md` | ファイル編集・関数抽出・テスト修正・コミット |
| `rules/verification.md` | PR 検証・テスト実行 |

## 事前調査

**実装前に必ず `shiori` MCP ツールで調査すること。**

- 関連issue/PRの内容を `shiori_search` + `shiori_read_issue` で取得
- リポジトリ構造の把握は `shiori_list_tree` で行う
- コードやドキュメントの内容は `shiori_read_file` で参照
- `shiori_ingest` で索引が最新であることを確認する

## セットアップ

- Shiori は Docker Compose で動作（`docker-compose.yml`）
- 調査は Shiori MCP ツール経由で行う
- コード変更時は sandbox コンテナ内で検証する

## 編集→検証→公開フロー

```
# 1. 調査
shiori_status() → shiori_ingest()
shiori_search() + shiori_read_file()

# 2. 編集（sandbox 内）: init の自動 pip を止め、CPU torch を先に入れる（CUDA 回避）
sandbox_initialize(allow_network=True, clone_repo="masuda-masuo/shiori", pip_extras=None)
sandbox_exec(container_id, commands=[
    "cd /tmp/repo/shiori && pip install torch --index-url https://download.pytorch.org/whl/cpu",
    "cd /tmp/repo/shiori && pip install -e '.[dev]'",
])
# 編集 → lint → type_check → test のループ

# 3. 公開（一発実行）
publish(container_id, repo, branch, message, create_pr=True, pr_title="...")
```
