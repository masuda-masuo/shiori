# 詳細設計: MCP サーバーとツール設計

## 目的

検索・取得の機能を MCP ツールとして公開し、エージェントが「検索 → 必要分だけ取得」を行えるようにする。

## ツール（予定）

- `semantic_search(query, filters?)`: 意味検索。チャンクのポインタ＋スニペットを返す。
- `keyword_search(query, filters?)`: キーワード／完全一致検索（日本語対応トークナイズ）。
- `list_tree(path?)`: リポジトリ構造の閲覧。
- `read_file(path, range?)` / `read_issue(number)`: 指定ファイル／スレッドを必要なら一部だけ取得。

検索系には `source_type`（doc / issue / pr_review）, `language`, `state` 等のフィルタを持たせる。

## 設計方針

- 「ツールボックスを渡してエージェントに選ばせる」形。
- ツールが多いと選択自体がモデル性能依存になるため、各ツールの description を明確化し、まず `semantic_search` を入口に推奨する等の配慮を入れる。
- 検索結果は常にポインタ。全文取得は明示的な read で行う。

## 検討事項 / 未決

- semantic と keyword を別ツールにするか、1 つの `search` に統合して内部でハイブリッドするか。
- 結果のスニペット長・件数のデフォルト。
- 引用（GitHub の URL）を結果に含めて、エージェントが出典を示せるようにする。
