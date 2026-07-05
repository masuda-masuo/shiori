"""GitHub authentication (detailed design/09).
Unifies PAT and GitHub App installation tokens under TokenProvider abstraction.
Decisions:
- App preferred, then TokenCommand, then GITHUB_TOKEN, then anonymous.
- Installation token expires in 1 hour; refreshes 5 min before expiry.
- Private key only passed to ingest process (not MCP server; see detailed design/07, 09).
"""

from __future__ import annotations

import calendar
import logging
import subprocess
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

    REFRESH_BEFORE = 300  # Re-issue 5 min before expiry

    def __init__(self, app_id: str, private_key_pem: str, installation_id: str) -> None:
        self._app_id = app_id
        self._key = private_key_pem
        self._installation_id = installation_id
        self._token: str | None = None
        self._expires_at: float = 0.0  # Epoch seconds (UTC)

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
            # Network down etc. Continue if cached token still valid; abort if not.
            if self._token and time.time() < self._expires_at:
                log.warning("token refresh failed, reusing cached token: %s", exc)
                return
            raise RuntimeError(f"failed to obtain installation token: {exc}") from exc

        if resp.status_code == 401:
            raise RuntimeError(
                "GitHub App JWT was rejected (401). Check GITHUB_APP_ID and private key pairing,"
                "and server clock."
            )
        if resp.status_code == 404:
            raise RuntimeError(
                "Installation not found (404). Check GITHUB_APP_INSTALLATION_ID and"
                "whether the App is installed on the target repository."
            )
        if resp.status_code == 403:
            raise RuntimeError(
                "Insufficient permissions (403). Check App permissions (Contents / Issues / Pull requests: Read) and"
                "the installation target repository."
            )
        resp.raise_for_status()  # 201 is success

        data = resp.json()
        self._token = data["token"]
        # expires_at is "2026-06-11T12:34:56Z" (UTC). Convert to epoch seconds.
        parsed = time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
        self._expires_at = float(calendar.timegm(parsed))
        log.info("issued installation token (expires_at=%s)", data["expires_at"])


class TokenCommandProvider(TokenProvider):
    """Calls external command (e.g. mcp-token github) to get token.
    Caches for 55 min, re-runs 5 min before expiry. Falls back on failure.
    """

    CACHE_SECONDS = 3300      # 55 min (GitHub installation token = 1 hour)
    REFRESH_BEFORE = 300      # 5 min
    HARD_EXPIRY = 3600        # 60 min — fallback window on command failure

    def __init__(self, command: str) -> None:
        self._command = command
        self._token: str | None = None
        self._fetched_at: float = 0.0

    def get_token(self) -> str | None:
        if self._token is None or time.time() > self._fetched_at + self.CACHE_SECONDS - self.REFRESH_BEFORE:
            self._refresh()
        return self._token

    def _refresh(self) -> None:
        try:
            result = subprocess.run(
                self._command, shell=True, capture_output=True,
                text=True, timeout=15.0,
            )
            token = result.stdout.strip()
            if token:
                self._token = token
                self._fetched_at = time.time()
                return
            log.warning("token command returned empty output (stderr: %s)", result.stderr.strip())
        except Exception as exc:
            log.warning("token command failed: %s", exc)
        # fallback
        if self._token and time.time() < self._fetched_at + self.HARD_EXPIRY:
            log.warning("token refresh failed; reusing cached token")
            return
        raise RuntimeError("token command failed and no cached token available")


def build_token_provider(settings: "Settings") -> TokenProvider:  # type: ignore[name-defined]  # noqa: F821
    """Select appropriate TokenProvider from Settings. Priority: App > TokenCommand > PAT > anonymous."""
    app_id = settings.github_app_id
    installation_id = settings.github_app_installation_id
    pem = settings.github_app_private_key()

    app_vars = [app_id, installation_id, pem]
    if any(app_vars):
        if not all(app_vars):
            raise ValueError(
                "GitHub App configuration is incomplete. Set GITHUB_APP_ID / "
                "and GITHUB_APP_PRIVATE_KEY(_PATH) / GITHUB_APP_INSTALLATION_ID."
            )
        if settings.github_token or settings.github_token_command:
            log.info("Both GitHub App config and token command/PAT present. App takes priority.")
        return AppTokenProvider(app_id, pem, installation_id)

    if settings.github_token_command:
        if settings.github_token:
            log.info("GITHUB_TOKEN_COMMAND takes priority over GITHUB_TOKEN")
        return TokenCommandProvider(settings.github_token_command)

    if settings.github_token:
        return StaticTokenProvider(settings.github_token)

    return AnonymousProvider()
