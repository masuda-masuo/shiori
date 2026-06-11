"""GitHub 認証（詳細設計/09）。

PAT と GitHub App installation token を TokenProvider 抽象に統一する。

決定事項:
- App 設定があれば App を優先、なければ GITHUB_TOKEN、どちらもなければ匿名（公開リポジトリのみ）。
- installation token は有効期限 1 時間。expiry の 5 分前を過ぎたら provider 内で再発行する。
- 秘密鍵は ingest 実行プロセスにだけ渡す（常駐 MCP サーバーには渡さない構成。詳細設計/07・09）。
"""

from __future__ import annotations

import calendar
import logging
import time
from dataclasses import dataclass

import httpx
import jwt

log = logging.getLogger(__name__)

API = "https://api.github.com"


class TokenProvider:
    """トークン供給の抽象。get_token() は None を返したら匿名（認証なし）を意味する。"""

    def get_token(self) -> str | None:
        raise NotImplementedError


class AnonymousProvider(TokenProvider):
    """認証なし。公開リポジトリのみ（レート制限は厳しい）。"""

    def get_token(self) -> str | None:
        return None


@dataclass
class StaticTokenProvider(TokenProvider):
    """長期 PAT などの固定トークン。"""

    token: str

    def get_token(self) -> str | None:
        return self.token


class AppTokenProvider(TokenProvider):
    """GitHub App の installation access token を発行・キャッシュする。

    get_token() は、未取得または expiry の REFRESH_BEFORE 秒前を過ぎていれば再発行する。
    長時間の ingest（CPU 埋め込みで 1 時間超があり得る）でも途中で失効しないようにする。
    """

    REFRESH_BEFORE = 300  # expiry の 5 分前から再発行

    def __init__(self, app_id: str, private_key_pem: str, installation_id: str) -> None:
        self._app_id = app_id
        self._key = private_key_pem
        self._installation_id = installation_id
        self._token: str | None = None
        self._expires_at: float = 0.0  # epoch 秒（UTC）

    def get_token(self) -> str | None:
        if self._token is None or time.time() > self._expires_at - self.REFRESH_BEFORE:
            self._refresh()
        return self._token

    def _app_jwt(self) -> str:
        """App 認証用の JWT（RS256）を生成する。

        iat を 60 秒過去にして時計ずれを吸収する。exp は上限 10 分未満の 9 分。
        """
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
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
        except httpx.HTTPError as exc:
            # ネットワーク断等。キャッシュ済みトークンがまだ有効なら継続、無効なら中断。
            if self._token and time.time() < self._expires_at:
                log.warning("token refresh failed, reusing cached token: %s", exc)
                return
            raise RuntimeError(f"installation token の取得に失敗しました: {exc}") from exc

        if resp.status_code == 401:
            raise RuntimeError(
                "GitHub App の JWT が拒否されました（401）。GITHUB_APP_ID と秘密鍵の対応、"
                "およびサーバー時刻を確認してください。"
            )
        if resp.status_code == 404:
            raise RuntimeError(
                "Installation が見つかりません（404）。GITHUB_APP_INSTALLATION_ID と、"
                "App が対象リポジトリにインストール済みかを確認してください。"
            )
        if resp.status_code == 403:
            raise RuntimeError(
                "権限不足です（403）。App の権限（Contents / Issues / Pull requests: Read）と、"
                "インストール対象リポジトリを確認してください。"
            )
        resp.raise_for_status()  # 201 が正常

        data = resp.json()
        self._token = data["token"]
        # expires_at は "2026-06-11T12:34:56Z"（UTC）。epoch 秒に変換する。
        parsed = time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
        self._expires_at = float(calendar.timegm(parsed))
        log.info("issued installation token (expires_at=%s)", data["expires_at"])


def build_token_provider(settings: "Settings") -> TokenProvider:  # type: ignore[name-defined]  # noqa: F821
    """Settings から適切な TokenProvider を選ぶ。優先順: App > PAT > 匿名。

    App 設定が部分的（一部の変数だけ）な場合は、設定ミスをサイレントに握り潰さず ValueError にする。
    """
    app_id = settings.github_app_id
    installation_id = settings.github_app_installation_id
    pem = settings.github_app_private_key()

    app_vars = [app_id, installation_id, pem]
    if any(app_vars):
        if not all(app_vars):
            raise ValueError(
                "GitHub App 設定が不完全です。GITHUB_APP_ID / "
                "GITHUB_APP_PRIVATE_KEY(_PATH) / GITHUB_APP_INSTALLATION_ID を揃えてください。"
            )
        if settings.github_token:
            log.info("GITHUB_TOKEN と GitHub App 設定が両方あります。App を優先します。")
        return AppTokenProvider(app_id, pem, installation_id)

    if settings.github_token:
        return StaticTokenProvider(settings.github_token)

    return AnonymousProvider()
