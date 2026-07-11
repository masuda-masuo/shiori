# 詳細設計: MCP サーバーとツール設計

## 目的

検索・取得の機能を MCP ツールとして公開し、エージェントが「検索 → 必要分だけ取得」を行えるようにする。ツール群の分類（4 層モデル）とユースケース別のツールフローは `詳細設計/13`（プロダクト定義とユースケース）を参照。

## ツール

- `shiori_search(query, filters?)`: 意味＋キーワードのハイブリッド検索（RRF 融合）。統合入口ツール。チャンクのポインタ＋スニペットを返す。
- `shiori_keyword_search(query, filters?)`: キーワード／完全一致検索（日本語対応トークナイズ）。厳密一致が必要なときに使う補助ツール。
- `shiori_list_tree(path?, source_type?, extension?)`: リポジトリ構造の閲覧。`source_type`（`doc` / `code`）と `extension`（`.py` / `.md` 等）での絞り込みが可能（issue #43）。
- `shiori_read_file(path, range?)` / `shiori_read_issue(number, repo?, exclude_noise_bots?)`: 指定ファイル／スレッドを必要なら一部だけ取得。`read_issue` の `exclude_noise_bots=true` で allowlist（`SHIORI_INDEX_BOT_LOGINS`）外の bot 投稿を除外可能（issue #44）。
- `shiori_pr_changes(number, repo?, include_diff?)`: PR の変更ファイルマップ（メタデータ）を返す（issue #54, #100）。head_sha と変更ファイル一覧（path / status / additions / deletions / blob_url）。`include_diff=true` で diff を同時取得できる。
- `shiori_pr_diff(number, path?, repo?)`: PR の unified diff を git で計算して返す（issue #96）。`path` 指定で単一ファイルの差分に絞れる。PR head と base を一時 ref に fetch して `git diff` する（ワーキングツリー非破壊）。
- `shiori_pr_review_comments(number, repo?)`: PR のレビューコメント一覧（`kind='pr_review_comment'`）を返す（issue #96）。ファイルパス・行番号・本文・作成者・作成日時を含む。
- `shiori_issue_links(number, repo?)`: issue/PR の相互参照を inbound / outbound で返す（issue #97）。本文・コメント中の `#N` 参照を抽出し、種別（closes / duplicate / refs / mention）を判定。参照先のタイトル・state を同梱。重複チェック・epic 構築・回帰追跡に使う。
- `shiori_read_pr_file(number, path, range?, repo?)`: PR head のファイル内容を git 薄皮ラッパーで透過的に取得する（issue #81）。`shiori_pr_changes` → `shiori_read_pr_file` の流れが ShioriMCP 内で完結する。
  > **v2 廃止:** MCP ツールとしての `shiori_ingest` は廃止されました。同期は CLI（`python -m shiori ingest`）または自動同期（`SHIORI_SYNC_INTERVAL_SECONDS`）を使用します。allowlist 制約（`SHIORI_REPOS`）と rebuild ガード（`SHIORI_ALLOW_REBUILD`）は CLI 経由でも適用されます。
- `shiori_grep(pattern, repo?, path?, regex?, ignore_case?, max_results?)`: クローンを ripgrep で直接検索する（issue #146, #151）。Stage-2 検索（`shiori_search`/`shiori_keyword_search` で絞り込んだファイルをさらに行レベルで grep）。`regex=True` が既定（issue #152）。パターンに `[...]`（文字クラス）を含むリテラルを検索する際は `regex=False` を指定する。`repo="*"` で全リポジトリ横断検索が可能。各マッチに `repo` フィールドを含む。クローン不在のリポジトリは `skipped_repos` として応答に明示される。
- `shiori_status()`: 索引の鮮度と健全性を照会する（issue #22, #31）。repo 毎に `chunks` の source_type 別内訳・`issue_items` 全件数・差分同期カーソル・警告（warnings）に加え、同期試行の記録 `last_attempt_at` / `last_error` / `consecutive_failures`（issue #187）を返す。トップレベルには `auto_sync_running`（auto sync スレッドの実生存。設定値のエコーではない）・`token_provider`（実効 provider。`詳細設計/09` 参照）・`auto_sync_last_error`（auto sync ループの直近エラー。成功でクリア。DB 不達など DB に記録できない失敗でも見える。issue #196）を含む。

検索系には `source_type`（doc / issue / pr_review / code）, `language`, `state` 等のフィルタを持たせる。bot 投稿は原則索引から除外されるが、`SHIORI_INDEX_BOT_LOGINS` 環境変数（GitHub App 名 + `[bot]` 形式のログイン名をカンマ区切りで指定）で allowlist 指定が可能（issue #25）。

### shiori_status の警告（issue #31, #35, #187）

`warnings` は以下の異常を自動検出する。警告がない場合も `"warnings": []` を常に返す:

| 条件 | 警告の意味 |
|---|---|
| `age_seconds` が stale 閾値超過 | 最終同期から長時間経過。索引が古い可能性。閾値は auto sync 有効時 `max(sync_interval_seconds * 30, 300秒)`、無効時は固定 24 時間（issue #187） |
| `consecutive_failures > 0` | 同期が連続失敗中。`last_error` を併記（issue #187） |
| token provider の降格・構築失敗 | mcp_token が匿名へフォールバック中（issue #188）、または provider 構築自体が失敗（`token_provider: "error"`。issue #193） |
| `chunks["issue"] + chunks["pr_review"] < items_in_db // 2` | issue_items に比べ検索可能チャンクが極端に少ない。bot 除外や索引欠落の可能性 |
| sync_state に未登録カテゴリ | 一部カテゴリが未同期。差分同期が必要 |

同期試行の記録（issue #187, #194, #196）: 成功/失敗を問わず `record_sync_attempt` が `last_attempt_at` を更新し、成功で `consecutive_failures` / `last_error` をリセット、失敗でカウントアップ＋エラー保存する。記録は MCP/auto-sync 経路（`_do_sync`。per-repo ループ手前の失敗も全対象 repo に記録）と CLI ingest 経路（`run_ingest`）の両方で行われる。

## 設計方針

- 「ツールボックスを渡してエージェントに選ばせる」形。
- ツールが多いと選択自体がモデル性能依存になるため、各ツールの description を明確化し、まず `shiori_search` を入口に推奨する等の配慮を入れる。
- 検索結果は常にポインタ。全文取得は明示的な read で行う。
- PR diff は shiori では保持せず、`shiori_pr_changes` が返す座標（repo / PR 番号 / head_sha / path / URL）で GitHub MCP から取得する（issue #54）。
- **PR head のファイル内容** は `shiori_read_pr_file` で ShioriMCP 内完結で取得できる（issue #81）。git 薄皮ラッパー（`_git_fetch_ref` / `_git_delete_ref`）でワーキングツリーを変更せずに読み取る。コンテンツを索引（DB）に入れないというポインタ設計の原則は維持したまま、エージェントの体験断絶を解消する。

## 実装詳細: shiori_read_pr_file（issue #81）

内部処理の流れ:

1. `_resolve_repo` で対象リポジトリを確定し、クローンの存在を確認
2. `build_token_provider(settings)` で認証プロバイダを取得
3. `_git_fetch_ref("pull/{N}/head", cwd, provider)` で PR head を一時 ref（`refs/shiori/tmp-{uuid}`）に shallow fetch
4. `_git(["show", "{tmp_ref}:{path}"], cwd)` でファイル内容を取得
5. `start_line` / `end_line` で行範囲を絞り込み
6. `finally` ブロックで `_git_delete_ref(tmp_ref)` により一時 ref を確実に削除

### エラー設計

| 失敗ケース | 検出方法 | エラーメッセージ |
|---|---|---|
| クローン不在 | `os.path.isdir(.../.git)` | 「クローンが存在しません」 |
| PR 不在 | `git fetch` が失敗 | 既存 `_git()` のエラーが伝播 |
| パス不在 | `git show` が失敗 | 「PR #{N} に {path} が見つかりません」 |
| 認証失敗 | `git fetch` が失敗 | 既存 `_git()` の認証ヒントが伝播 |

### 並行競合対策

`FETCH_HEAD` への fetch を避け、UUID 付き一時 ref（`refs/shiori/tmp-{uuid}`）に fetch することで、並行リクエスト間の干渉を防止する。一時 ref は `finally` ブロックで必ず削除されるため、例外発生時のリークもない。

## 決定事項（issue #40）

- `shiori_search` を統合入口ツールとし、内部でハイブリッド融合（RRF）を行う。
- `shiori_keyword_search` は厳密一致専用の補助ツールとして維持する（非推奨化しない）。
- 旧 `shiori_semantic_search` は `shiori_search` にリネーム（外部未公開のため後方互換不要）。

## 決定事項（issue #81）

- `shiori_read_pr_file` を新設し、`shiori_read_file` の「main ブランチ固定」責務は変更しない。
- 内部で `_git_fetch_ref` / `_git_delete_ref` 共通ヘルパーを使用し、#74 / #79 / #77 でも再利用可能にする。
- PR head のファイル内容は索引（DB）には入れず、クローン経由の動的取得にとどめる（ポインタ設計の原則を維持）。

## 決定事項（issue #151）

- `shiori_grep` の `repo` パラメータに `"*"` を指定すると全リポジトリ横断検索となる。
- `repo=None`（未指定時）の既定挙動は従来どおり単一リポジトリ解決（`_resolve_repo` 経由）。後方互換を維持する。
- 各マッチに `repo` フィールドを付与し、どのリポジトリの結果か識別可能にする。
- クローン不在のリポジトリはエラーにせずスキップし、`skipped_repos` として応答に含める。

## 決定事項（issue #152）

- `shiori_grep` の `regex` 既定値を `False`（fixed-strings）から `True`（regex）に変更する。
  - モデルの事前分布（grep/rg は regex 既定）に合わせるため。
  - リテラルのつもりで `foo(` を渡すと rg が exit 2（パースエラー）で大声で落ちる→自己修復可能。
  - `[...]` を含むリテラルは regex=False を明示する。
- rg の exit code 2 検出時、エラーメッセージに `regex=False` を推奨するヒントを含める。

## 検討事項 / 未決

- 結果のスニペット長・件数のデフォルト。
- 引用（GitHub の URL）を結果に含めて、エージェントが出典を示せるようにする。
