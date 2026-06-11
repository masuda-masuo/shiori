"""GitHub 認証。PAT と GitHub App installation token を TokenProvider に抽象化する。"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass

import httpx
import jwt

log = logging.getLogger(__name__)
API = "https://api.github.com"


class TokenProvider:
    """トークンプロバイダの抽象インタフェース。"""

    def get_token(self) -> str | None:
        """トークンを取得する。失効間際なら自動再発行される。None は匿名を示す。"""
        raise NotImplementedError


class AnonymousProvider(TokenProvider):
    """認証なし（公開リポジトリのみ）。"""

    def get_token(self) -> str | None:
        return None


@dataclass
class StaticTokenProvider(TokenProvider):
    """静的トークン（PAT）プロバイダ。"""

    token: str

    def get_token(self) -> str | None:
        return self.token


class AppTokenProvider(TokenProvider):
    """GitHub App installation token プロバイダ。
    
    JWT を生成し、installation token を取得・キャッシュする。
    expiry の 5 分前で自動再発行。
    """

    REFRESH_BEFORE = 300  # 5 分前から再発行

    def __init__(self, app_id: str, private_key_pem: str, installation_id: str):
        self._app_id = app_id
        self._key = private_key_pem
        self._installation_id = installation_id
        self._token: str | None = None
        self._expires_at: float = 0.0  # epoch 秒

    def get_token(self) -> str | None:
        """トークンを取得。失効間際なら再発行する。"""
        if self._token is None or time.time() > self._expires_at - self.REFRESH_BEFORE:
            self._refresh()
        return self._token

    def _app_jwt(self) -> str:
        """GitHub App JWT を生成。"""
        now = int(time.time())
        payload = {
            "iat": now - 60,  # 60 秒過去（時計ずれ対策）
            "exp": now + 540,  # 9 分後（上限 10 分）
            "iss": self._app_id,
        }
        return jwt.encode(payload, self._key, algorithm="RS256")

    def _refresh(self) -> None:
        """GitHub API から installation token を取得・キャッシュする。"""
        url = f"{API}/app/installations/{self._installation_id}/access_tokens"
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {self._app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )
        if resp.status_code == 401:
            raise RuntimeError(
                "GitHub App の JWT が拒否されました。GITHUB_APP_ID と秘密鍵の対応、"
                "サーバー時刻を確認してください。"
            )
        if resp.status_code == 404:
            raise RuntimeError(
                "Installation が見つかりません。GITHUB_APP_INSTALLATION_ID と、"
                "App が対象リポジトリにインストール済みかを確認してください。"
            )
        resp.raise_for_status()  # 201 が正常
        data = resp.json()
        self._token = data["token"]
        # expires_at: "2026-06-11T12:34:56Z"
        self._expires_at = time.mktime(
            time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
        ) - time.timezone  # UTC 補正
        log.info("issued installation token (expires_at=%s)", data["expires_at"])


def build_token_provider(settings: "Settings") -> TokenProvider:  # type: ignore
    """設定から適切な TokenProvider を構築する。
    
    優先順序: GitHub App > PAT > Anonymous
    """
    pem = settings.github_app_private_key()
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
