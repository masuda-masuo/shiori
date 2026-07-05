# 詳細設計: GitHub App 認証（短期トークン）

## 目的

長期 PAT（`GITHUB_TOKEN`）の代わりに、GitHub App の installation access token
（有効期限 1 時間）で ingest を実行できるようにする。外部ランチャー（stdio 限定）への
依存をなくし、streamable HTTP 構成のまま短期トークン運用を成立させる。

## 前提

設計当初は「shiori がトークンを使うのは ingest（一回限りのジョブ）だけで、常駐の
MCP サーバーは GitHub に触れない」という前提だったが、issue #6 で serve プロセス内の
自動同期（`SHIORI_SYNC_INTERVAL_SECONDS`）と `shiori_ingest` ツールを追加したため、
現在は常駐 `app` も `GITHUB_TOKEN`（PAT）で GitHub に触れる。本設計では、
すべてのサービス（app / ingest）で App 認証を使う。`build_token_provider()` が
環境変数の有無に応じて App / PAT / anonymous を自動選択する。
ジョブ内でトークンを取得し、長時間ジョブに備えてリクエスト単位で再発行できれば十分、
という基本方針自体は変わらない。

## 決定事項

1. **認証は `TokenProvider` 抽象に統一する。** 静的トークン（PAT）、GitHub App、
   および外部コマンド（TokenCommand）の 3 実装を持ち、`github_sync` は provider 経由でのみトークンに触れる。
2. **App > TokenCommand > PAT > 匿名** の優先順位。TokenCommand は `GITHUB_TOKEN_COMMAND` 環境変数で
   指定されたコマンド（例: `mcp-token github`）を定期実行してトークンを取得する。
   App が設定されていれば App を優先、なければ TokenCommand、なければ `GITHUB_TOKEN`、どちらも
   なければ匿名。TokenCommand と PAT 両方設定時は TokenCommand を使い info ログを出す。
3. **トークンは expiry の 5 分前を過ぎたら再発行**（provider 内でキャッシュ）。
   初回 ingest（CPU 埋め込みで 1 時間超があり得る）でも途中で失効しない。
4. **git の認証は clone URL 埋め込みをやめ、`http.extraHeader` で毎回注入する。**
   理由: 現行方式は `.git/config` にトークンが平文で永続化され（named volume 上に残る）、
   短期トークンでは次回 pull 時に失効済みトークンが残って失敗する。
5. **GitHub App の秘密鍵は app / ingest の全サービスに渡す。**
   compose 上では secrets + environment で全サービスに同一設定を共有する。
   `build_token_provider()` が App → PAT → anonymous の優先順位で認証方式を選択する。
   PAT 運用時も `GITHUB_TOKEN` は全サービスに渡す。
6. **依存追加: `pyjwt[crypto]`**（RS256 署名に cryptography が必要）。

## 設定（環境変数）

| 変数 | 説明 |
| --- | --- |
| `GITHUB_APP_ID` | App ID（数値文字列） |
| `GITHUB_APP_PRIVATE_KEY_PATH` | 秘密鍵 PEM のパス。既定 `/run/secrets/github_app_key` |
| `GITHUB_APP_PRIVATE_KEY` | PEM 本文を直接渡す場合（PATH より優先度低。両方あれば PATH） |
| `GITHUB_APP_INSTALLATION_ID` | Installation ID（数値文字列） |
| `GITHUB_TOKEN` | 従来どおり。App / TokenCommand 未設定時のフォールバック |
| `GITHUB_TOKEN_COMMAND` | 外部コマンド（例: `mcp-token github`）。stdout がトークン。TokenCommandProvider がキャッシュ＋自動再発行。 |

TokenCommand の優先順位: App > TokenCommand > PAT > 匿名。
TokenCommand と PAT 両方設定時は TokenCommand を優先し info ログを出す。

App 利用の判定: `GITHUB_APP_ID` と `GITHUB_APP_INSTALLATION_ID` が両方あり、
鍵（PATH または本文）が読めること。一部だけ設定されている場合は起動時に
`ValueError`（設定ミスをサイレントにフォールバックさせない）。

## 実装

### 1. `src/shiori/github_auth.py`（新規）

```python
"""GitHub 認証。PAT と GitHub App installation token を TokenProvider に抽象化する。"""
from __future__ import annotations

import calendar
import logging
import time
from dataclasses import dataclass

import httpx
import jwt  # pyjwt[crypto]

log = logging.getLogger(__name__)
API = "https://api.github.com"


class TokenProvider:
    def get_token(self) -> str | None:  # None = 匿名
        raise NotImplementedError


class AnonymousProvider(TokenProvider):
    def get_token(self) -> str | None:
        return None


@dataclass
class StaticTokenProvider(TokenProvider):
    token: str

    def get_token(self) -> str | None:
        return self.token


class AppTokenProvider(TokenProvider):
    REFRESH_BEFORE = 300  # expiry の 5 分前から再発行

    def __init__(self, app_id: str, private_key_pem: str, installation_id: str):
        self._app_id = app_id
        self._key = private_key_pem
        self._installation_id = installation_id
        self._token: str | None = None
        self._expires_at: float = 0.0  # epoch 秒

    def get_token(self) -> str | None:
        if self._token is None or time.time() > self._expires_at - self.REFRESH_BEFORE:
            self._refresh()
        return self._token

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        # iat を 60 秒過去にして時計ずれを吸収。exp は上限 10 分未満の 9 分。
        return jwt.encode(payload, self._key, algorithm="RS256")

    def _refresh(self) -> None:
        url = f"{API}/app/installations/{self._installation_id}/access_tokens"
        try:
            resp = httpx.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._app_jwt()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
            )
        except httpx.HTTPError:
            if self._token and time.time() < self._expires_at:
                log.warning("token refresh failed (network); keeping cached token")
                return
            raise
        if resp.status_code == 401:
            raise RuntimeError(
                "GitHub App の JWT が拒否されました。GITHUB_APP_ID と秘密鍵の対応、"
                "サーバー時刻を確認してください。"
            )
        if resp.status_code == 403:
            raise RuntimeError(
                "GitHub App に必要な権限がありません。App の権限（Contents/Issues/"
                "PR: Read）とインストール対象リポジトリを確認してください。"
            )
        if resp.status_code == 404:
            raise RuntimeError(
                "Installation が見つかりません。GITHUB_APP_INSTALLATION_ID と、"
                "App が対象リポジトリにインストール済みかを確認してください。"
            )
        resp.raise_for_status()  # 201 が正常
        data = resp.json()
        self._token = data["token"]
        # expires_at: "2026-06-11T12:34:56Z" -> UTC の epoch 秒へ直接変換（DST 非依存）
        self._expires_at = calendar.timegm(
            time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
        )
        log.info("issued installation token (expires_at=%s)", data["expires_at"])


def build_token_provider(settings: "Settings") -> TokenProvider:
    pem = settings.github_app_private_key()  # PATH 優先で PEM 文字列を返す。なければ None
    app_vars = [settings.github_app_id, settings.github_app_installation_id, pem]
    if any(app_vars):
        if not all(app_vars):
            raise ValueError(
                "GitHub App 設定が不完全です: GITHUB_APP_ID / "
                "GITHUB_APP_PRIVATE_KEY(_PATH) / GITHUB_APP_INSTALLATION_ID を揃えてください。"
            )
        if settings.github_token:
            log.info("GITHUB_TOKEN と App 設定が両方あります。App を優先します。")
        return AppTokenProvider(
            settings.github_app_id, pem, settings.github_app_installation_id
        )
    if settings.github_token:
        return StaticTokenProvider(settings.github_token)
    return AnonymousProvider()
```

> `_refresh()` の UTC 変換・ネットワークエラー時のキャッシュフォールバック・403 専用の
> エラーメッセージは、上記コード例どおりに実装済み（issue #14）。

### 6. TokenCommandProvider（第3実装）

`TokenCommandProvider` は外部コマンド（例: `mcp-token github`）を定期実行してトークンを取得する。
キャッシュ有効期間は55分（GitHub installation token の1時間より5分短い）、
コマンド失敗時はキャッシュ期限内（60分）ならフォールバックする。

`Settings` への追加: `github_token_command`（`GITHUB_TOKEN_COMMAND` 環境変数）。
`build_token_provider()` の優先順位（更新）:

```python
def build_token_provider(settings: "Settings") -> TokenProvider:
    # App 優先（既存）
    ...
    # TokenCommand が次に優先
    if settings.github_token_command:
        if settings.github_token:
            log.info("GITHUB_TOKEN_COMMAND takes priority over GITHUB_TOKEN")
        return TokenCommandProvider(settings.github_token_command)
    # StaticToken（PAT）
    if settings.github_token:
        return StaticTokenProvider(settings.github_token)
    return AnonymousProvider()
```

`Settings` への追加: `github_app_id` / `github_app_installation_id`（環境変数の単純読み）、
`github_app_private_key()`（`GITHUB_APP_PRIVATE_KEY_PATH` のファイルを読む。
存在しなければ `GITHUB_APP_PRIVATE_KEY` を返す。どちらもなければ `None`）。

### 2. `github_sync.py` の変更

シグネチャ変更（呼び出し元の CLI も追随）:

```python
def sync_docs(settings, conn, embedder, repo, provider: TokenProvider) -> int: ...
def sync_issues(settings, conn, embedder, repo, provider: TokenProvider) -> int: ...
```

**(a) git: URL 埋め込みを廃止し extraHeader 注入に変更。**

```python
import base64

def _auth_args(provider: TokenProvider) -> list[str]:
    token = provider.get_token()
    if not token:
        return []
    b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {b64}"]
```

- `_clone_url` は削除し、リモート URL は常に
  `https://github.com/{repo}.git`（トークンなし）。
- ネットワークに出る操作（`clone` / `fetch`）の直前で `_auth_args(provider)` を取得し、
  `_git(_auth_args(provider) + ["fetch", ...], cwd=...)` のように **`git` の直後**に挿入する
  （`-c` はサブコマンドより前。`_git` は先頭が `-c` でも動くよう変更不要、引数順のみ注意）。
- `rev-parse` / `reset` 等ローカル操作には付けない。
- **既存クローンの移行:** fetch の前に毎回
  `_git(["remote", "set-url", "origin", f"https://github.com/{repo}.git"], cwd=repo_dir)`
  を実行する（旧方式でトークン入り URL が `.git/config` に残っていても上書きされる。
  冪等なので常時実行でよい）。
- `_redact` は防御として残す。

**(b) API: httpx の Auth フックでリクエストごとに注入。**

```python
class _GitHubAuth(httpx.Auth):
    def __init__(self, provider: TokenProvider):
        self._provider = provider

    def auth_flow(self, request):
        token = self._provider.get_token()  # 失効間際なら自動再発行される
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request
```

`sync_issues` 冒頭の headers 組み立てから `Authorization` を外し、
`httpx.Client(headers=headers, auth=_GitHubAuth(provider), timeout=30.0)` とする。
これで 1 時間超の ingest でもページネーション途中の失効が起きない。

### 3. CLI（`python -m shiori ingest`）

冒頭で `provider = build_token_provider(settings)` を 1 回構築し、
全リポジトリの `sync_docs` / `sync_issues` に渡す（provider はプロセス内でトークンを
キャッシュ・再発行する）。

### 4. Docker Compose（App 秘密鍵をワンショットコンテナに限定）

```yaml
secrets:
  github_app_key:
    file: ./secrets/github-app.private-key.pem

services:
  app:            # 常駐 MCP サーバー。全認証方式対応（App / PAT / anonymous）
    environment:
      # GitHub App 認証（GITHUB_TOKEN はフォールバック、未設定時は anonymous）
      GITHUB_TOKEN: ${GITHUB_TOKEN:-}
      GITHUB_APP_ID: ${GITHUB_APP_ID:-}
      GITHUB_APP_INSTALLATION_ID: ${GITHUB_APP_INSTALLATION_ID:-}
      GITHUB_APP_PRIVATE_KEY_PATH: ${GITHUB_APP_PRIVATE_KEY_PATH:-/run/secrets/github_app_key}
    secrets:
      - github_app_key
    ...
  ingest:
    profiles: ["ingest"]   # `up` では起動しない
    image: shiori-app      # app と同イメージ
    command: python -m shiori ingest
    environment:
      GITHUB_APP_ID: ${GITHUB_APP_ID}
      GITHUB_APP_INSTALLATION_ID: ${GITHUB_APP_INSTALLATION_ID}
      GITHUB_APP_PRIVATE_KEY_PATH: /run/secrets/github_app_key
    secrets:
      - github_app_key
    depends_on: [db]
```

実行: `docker compose run --rm ingest`（cron からも同コマンド）。
`./secrets/` は `.gitignore` に追加する。

上記は compose の基本構成を示す抜粋であり、`app` + `ingest` のみを示している。
issue #116 で廃止された `runner` を除く最新の全サービス構成は `docker-compose.yml` を参照すること。

### 5. App の権限（README に記載）

最小権限: **Contents: Read-only**（clone 用）、**Issues: Read-only**、
**Pull requests: Read-only**。Write は不要。Webhook 無効。

## エッジケース

- **時計ずれ:** JWT の `iat` を 60 秒過去に設定済み。それでも 401 になる場合は
  エラーメッセージでサーバー時刻の確認を促す（上記実装に含む）。
- **再発行失敗（ネットワーク断等）:** キャッシュ済みトークンが**まだ有効なら**
  warning を出して継続、失効済みなら例外で ingest を中断する。
  実装: `_refresh` を try で包み、`self._token and time.time() < self._expires_at`
  なら握りつぶし、そうでなければ re-raise。
- **権限不足（403）:** 403 専用のエラーメッセージで
  「App の権限（Contents/Issues/PR: Read）とインストール対象リポジトリを確認」を案内する。
- **匿名 + private リポジトリ:** 現行どおり git のエラーヒントで案内（変更なし）。

## テスト項目

1. `build_token_provider`: App 完備 → App / 一部欠け → ValueError /
   PAT のみ → Static / なし → Anonymous / 両方 → App 優先。
2. `AppTokenProvider`: 初回 `_refresh` 呼び出し、キャッシュ有効中は再発行しない、
   `expires_at - 300s` 経過で再発行、401/403/404 のエラーメッセージ、
   ネットワークエラー時のキャッシュフォールバック。
   （httpx はモック。`respx` 等を使用）
3. `_app_jwt`: `iat = now-60`, `exp = now+540`, `iss = app_id`, alg=RS256 を
   デコードして検証。
4. `_auth_args`: トークンあり → Basic ヘッダの base64 が `x-access-token:{token}`、
   匿名 → 空リスト。
5. 結合: 旧トークン入り URL を仕込んだ `.git/config` が `remote set-url` で
   浄化されること。
6. `build_token_provider`: App + TokenCommand 両方 → App 優先 / TokenCommand のみ → TokenCommandProvider /
   TokenCommand + PAT 両方 → TokenCommand 優先 / すべてなし → Anonymous。
7. `TokenCommandProvider`: 初回 get_token でコマンド実行、キャッシュ有効中は再実行しない、
   55分経過で再実行、コマンド失敗時は60分以内ならキャッシュフォールバック、
   空出力＋キャッシュなしで RuntimeError。

## 検討事項 / 未決

- 複数 organization（installation 複数）対応。v1 は 1 installation のみ。
  必要になったら `SHIORI_REPOS` の repo ごとに installation を引く map を追加する。
- MCP サーバー自体の認可（OAuth 2.1）。本書のスコープ外
  （localhost 超えの公開時に別途設計。`基本設計` 未決事項に追加）。

## 基本設計.md への反映

- §5 決定ログに「GitHub 認証は TokenProvider 抽象。GitHub App（installation token、
  extraHeader 注入）を推奨、PAT はフォールバック。App 秘密鍵は app / ingest に渡し、
  PAT は app にも渡す」を追記。
- §6 未決事項に「MCP サーバー自体の認可（OAuth 2.1）— リモート公開時」を追加。
