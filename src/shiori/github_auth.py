"""GitHub authentication (detailed design/09).
Unifies PAT and GitHub App installation tokens under TokenProvider abstraction.
Decisions:
- App preferred, then GITHUB_TOKEN, then anonymous (public repos only).
- Installation token expires in 1 hour; refreshes 5 min before expiry.
- Private key only passed to ingest process (not MCP server; see detailed design/07, 09).
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
    """Abstract token supplier. get_token() returning None means anonymous (no auth)."""

    def get_token(self) -> str | None:
        raise NotImplementedError


class AnonymousProvider(TokenProvider):
    """No authentication. Public repos only (strict rate limits)."""

    def get_token(self) -> str | None:
        return None


@dataclass
class StaticTokenProvider(TokenProvider):
    """Static token, e.g. long-lived PAT."""

    token: str

    def get_token(self) -> str | None:
        return self.token


class AppTokenProvider(TokenProvider):
    """Issues and caches GitHub App installation access tokens.
    get_token() re-issues if not obtained or within REFRESH_BEFORE seconds of expiry.
    Ensures tokens survive long ingests (CPU embedding can exceed 1 hour).
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
        """Generate JWT (RS256) for App authentication.
        iat set 60s in past. exp is 9 min (under 10-min limit)."""
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
    """Select appropriate TokenProvider from Settings. Priority: App > PAT > anonymous."""
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
