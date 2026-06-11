# GitHub App 認証実装計画

## ブランチ
`feat/github-app-auth`

## Issue
#4

## 実装ファイル一覧

### 新規ファイル
- **`src/shiori/github_auth.py`** ✅ 完成
  - `TokenProvider` 抽象クラス
  - `AnonymousProvider`, `StaticTokenProvider`, `AppTokenProvider` 実装
  - `build_token_provider()` ファクトリ関数

### 既存ファイル変更（ガイド提供）
- **`src/shiori/config.py`**
  - ガイド: `IMPLEMENTATION_GUIDE_config.py.txt`
  - 追加内容:
    - `github_app_id` プロパティ
    - `github_app_installation_id` プロパティ
    - `github_app_private_key()` メソッド

- **`src/shiori/github_sync.py`**
  - ガイド: `IMPLEMENTATION_GUIDE_github_sync.py.txt`
  - 変更内容:
    - インポート追加（github_auth）
    - `_auth_args()` 関数追加
    - `_GitHubAuth` クラス追加
    - `sync_docs()` / `sync_issues()` のシグネチャ変更
    - git 認証方式の変更（URL 埋め込み → extraHeader 注入）
    - `.git/config` 浄化（remote set-url）
    - CLI から provider を構築

- **`docker-compose.yml`**
  - ガイド: `IMPLEMENTATION_GUIDE_docker-compose.yml.txt`
  - 追加内容:
    - `secrets` セクション（github_app_key）
    - `ingest` サービス（profiles: ["ingest"]）
    - 環境変数（GITHUB_APP_ID / INSTALLATION_ID / PRIVATE_KEY_PATH）

- **`requirements.txt`** (or `pyproject.toml`)
  - ガイド: `IMPLEMENTATION_GUIDE_requirements.txt`
  - 追加: `pyjwt[crypto]>=2.8.0`

## 実装手順

### Step 1: github_auth.py は完成済み

### Step 2: config.py に設定を追加
`IMPLEMENTATION_GUIDE_config.py.txt` の内容を参考に:
```python
@property
def github_app_id(self) -> str | None: ...
@property
def github_app_installation_id(self) -> str | None: ...
def github_app_private_key(self) -> str | None: ...
```

### Step 3: github_sync.py を変更
`IMPLEMENTATION_GUIDE_github_sync.py.txt` の内容を参考に:
- インポート追加
- `_auth_args()` 関数追加
- `_GitHubAuth` クラス追加
- `sync_docs()` / `sync_issues()` シグネチャ変更
- git コマンド実行を extraHeader 方式に変更
- CLI から provider を構築

### Step 4: docker-compose.yml を更新
`IMPLEMENTATION_GUIDE_docker-compose.yml.txt` の内容を参考に:
- `secrets` セクション追加
- `ingest` サービス追加
- 環境変数設定

### Step 5: requirements.txt に依存を追加
```
pyjwt[crypto]>=2.8.0
```

### Step 6: テスト実施
以下の項目をテスト:
1. `build_token_provider` の優先順位（App > PAT > Anonymous）
2. `AppTokenProvider` の JWT 生成と検証
3. `AppTokenProvider` のトークン再発行（expiry 5 分前）
4. `_auth_args` の base64 エンコード
5. git fetch 時の extraHeader 注入確認
6. `.git/config` 浄化（remote set-url）確認
7. 実際の ingest 実行テスト

## 検証ポイント

- [ ] AppTokenProvider が JWT を正しく生成している
- [ ] Installation token を正しく取得できている
- [ ] Expiry 5 分前で自動再発行されている
- [ ] Git 認証が extraHeader で正しく注入されている
- [ ] 既存の tro ken 入り `.git/config` が浄化されている
- [ ] API リクエストで Bearer トークンが正しく注入されている
- [ ] 環境変数なしで従来の GITHUB_TOKEN にフォールバックされている

## マイグレーション

既存の `GITHUB_TOKEN` 環境変数を設定したまま、新しく `GITHUB_APP_*` 環境変数を設定すると:
- 自動的に App 認証が優先される
- ログで「App を優先します」と表示される
- `.git/config` の既存トークンは自動浄化される

## 関連ドキュメント

- `09-GitHubApp認証.md` - 詳細設計
- Issue #4 - GitHub App 認証の実装

## PR 作成後

すべての実装が完了したら:
1. PR を作成（base: main, head: feat/github-app-auth）
2. CI テストが通ることを確認
3. Code Review を実施
4. マージして feature complete
