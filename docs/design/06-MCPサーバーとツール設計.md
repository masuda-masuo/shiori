# 詳細設計: MCP サーバーとツール設計

## 目的

検索・取得の機能を MCP ツールとして公開し、エージェントが「検索 → 必要分だけ取得」を行えるようにする。

## ツール

- `shiori_semantic_search(query, filters?)`: 意味検索。チャンクのポインタ＋スニペットを返す。
- `shiori_keyword_search(query, filters?)`: キーワード／完全一致検索（日本語対応トークナイズ）。
- `shiori_list_tree(path?)`: リポジトリ構造の閲覧。
- `shiori_read_file(path, range?)` / `shiori_read_issue(number)`: 指定ファイル／スレッドを必要なら一部だけ取得。
- `shiori_ingest(rebuild?)`: docs と issue/PR を GitHub から同期し索引を更新する（オンデマンド。`rebuild=true` で全件再構築。issue #6）。
- `shiori_status()`: 索引の鮮度と健全性を照会する（issue #22, #31）。`chunks` の source_type 別内訳・`issue_items` 全件数・差分同期カーソル・警告（warnings）を返す。

検索系には `source_type`（doc / issue / pr_review）, `language`, `state` 等のフィルタを持たせる。bot 投稿は原則索引から除外されるが、`SHIORI_INDEX_BOT_LOGINS` 環境変数で allowlist 指定が可能（issue #25）。

### shiori_status の警告（issue #31）

`warnings` は以下の異常を自動検出する:

| 条件 | 警告の意味 |
|---|---|
| `age_seconds > 86400` | 最終同期から長時間経過。索引が古い可能性 |
| `items_in_db ≫ chunks[issue]` | issue_items に比べ検索可能チャンクが極端に少ない。bot 除外や索引欠落の可能性 |
| sync_state に未登録カテゴリ | 一部カテゴリが未同期。差分同期が必要 |

## 設計方針

- 「ツールボックスを渡してエージェントに選ばせる」形。
- ツールが多いと選択自体がモデル性能依存になるため、各ツールの description を明確化し、まず `shiori_semantic_search` を入口に推奨する等の配慮を入れる。
- 検索結果は常にポインタ。全文取得は明示的な read で行う。

## 検討事項 / 未決

- semantic と keyword を別ツールにするか、1 つの `search` に統合して内部でハイブリッドするか。
- 結果のスニペット長・件数のデフォルト。
- 引用（GitHub の URL）を結果に含めて、エージェントが出典を示せるようにする。
