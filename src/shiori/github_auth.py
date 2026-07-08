"""GitHub authentication (detailed design/09).
Unifies PAT and GitHub App installation tokens under TokenProvider abstraction.
Decisions:
- App preferred, then TokenCommand, then GITHUB_TOKEN, then anonymous.
- Installation token expires in 1 hour; refreshes 5 min before expiry.
- Private key only passed to ingest process (not MCP server; see detailed design/07, 09).
"""

from __future__ import annotations

import calendar
import hashlib
import logging
import os
import shlex
import shutil
import subprocess
import sys
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
                "GitHub App JWT was rejected (401). Check GITHUB_APP_ID and private key pairing, "
                "and server clock."
            )
        if resp.status_code == 404:
            raise RuntimeError(
                "Installation not found (404). Check GITHUB_APP_INSTALLATION_ID and "
                "whether the App is installed on the target repository."
            )
        if resp.status_code == 403:
            raise RuntimeError(
                "Insufficient permissions (403). Check App permissions (Contents / Issues / Pull requests: Read) and "
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
            # Use shell=True on Windows to preserve backslash paths (shlex.split
            # follows POSIX rules and strips backslashes; #139).
            # GITHUB_TOKEN_COMMAND is an admin-configured env var, so shell metachar
            # injection (|, &, >) is not a practical concern here.
            if sys.platform == "win32":
                result = subprocess.run(
                    self._command, capture_output=True,
                    text=True, timeout=15.0, shell=True,
                )
            else:
                result = subprocess.run(
                    shlex.split(self._command), capture_output=True,
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


class McpTokenProvider(TokenProvider):
    """Resolves mcp-token binary (4-step: env → PATH → cache → download)
    and calls it to obtain a short-lived GitHub token.
    Same cache/expiry pattern as TokenCommandProvider.
    Falls back to anonymous if binary cannot be resolved.

    Linux-only: the download step uses os.uname() to detect architecture and
    fetches a Linux binary. The initial 3 steps (env/PATH/cache) are
    platform-agnostic; only _download() is Linux-specific.

    When bumping mcp-token version:
      1. Update _TAG to the new release tag.
      2. Download the two assets and compute SHA256:
         curl -Lo /tmp/a https://github.com/masuda-masuo/mcp-launcher/releases/download/<tag>/mcp-token-linux-amd64
         curl -Lo /tmp/b https://github.com/masuda-masuo/mcp-launcher/releases/download/<tag>/mcp-token-linux-arm64
         sha256sum /tmp/a /tmp/b
      3. Update _LINUX_AMD64_SHA256 and _LINUX_ARM64_SHA256 accordingly.
    """

    CACHE_SECONDS = 3300
    REFRESH_BEFORE = 300
    HARD_EXPIRY = 3600

    _TAG = "mcp-token/v1.1.1"
    _LINUX_AMD64_SHA256 = "08d22380f0af932508aaaea80cb114acada1ef46d0a3b32507755c67f5f77bba"
    _LINUX_ARM64_SHA256 = "032ee0942fd4e2184158f111873c67f32d843def0cbefca576df614bfc8d3c64"

    def __init__(self, data_dir: str) -> None:
        self._data_dir = data_dir
        self._binary: str | None = None
        self._binary_resolved = False
        self._token: str | None = None
        self._fetched_at: float = 0.0

    def get_token(self) -> str | None:
        if self._binary_resolved and self._binary is None:
            if self._token is not None and time.time() < self._fetched_at + self.HARD_EXPIRY:
                return self._token
            return None
        if self._token is None or time.time() > self._fetched_at + self.CACHE_SECONDS - self.REFRESH_BEFORE:
            self._refresh()
        return self._token

    def _resolve_binary(self) -> str | None:
        exe = os.environ.get("MCP_TOKEN_EXE")
        if exe and os.path.isfile(exe) and os.access(exe, os.X_OK):
            return exe
        which = shutil.which("mcp-token")
        if which:
            return which
        cached = os.path.join(self._data_dir, "mcp-token")
        if os.path.isfile(cached) and os.access(cached, os.X_OK):
            return cached
        try:
            self._download(cached)
            return cached
        except Exception:
            log.warning("mcp-token binary not found and download failed; falling back to anonymous")
            return None

    def _download(self, dest: str) -> None:
        arch = _detect_arch()
        expected_sha = self._LINUX_AMD64_SHA256 if arch == "amd64" else self._LINUX_ARM64_SHA256
        asset = f"mcp-token-linux-{arch}"
        encoded_tag = self._TAG.replace("/", "%2F")
        url = f"https://github.com/masuda-masuo/mcp-launcher/releases/download/{encoded_tag}/{asset}"
        log.info("downloading %s from %s", asset, url)
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = httpx.get(url, headers=headers or None, timeout=30.0, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"failed to download mcp-token: {exc}") from exc
        body = resp.content
        actual = _sha256_hex(body)
        if actual != expected_sha:
            raise RuntimeError(f"mcp-token sha256 mismatch: expected {expected_sha}, got {actual}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fp:
            fp.write(body)
        os.chmod(dest, 0o755)
        log.info("mcp-token cached at %s", dest)

    def _refresh(self) -> None:
        if not self._binary_resolved:
            self._binary = self._resolve_binary()
            self._binary_resolved = True
        if self._binary is None:
            return
        try:
            result = subprocess.run(
                [self._binary, "github"], capture_output=True,
                text=True, timeout=15.0,
            )
            token = result.stdout.strip()
            if token:
                self._token = token
                self._fetched_at = time.time()
                return
            log.warning("mcp-token returned empty output (stderr: %s)", result.stderr.strip())
        except Exception as exc:
            log.warning("mcp-token failed: %s", exc)
        if self._token and time.time() < self._fetched_at + self.HARD_EXPIRY:
            log.warning("mcp-token refresh failed; reusing cached token")
            return
        log.warning("mcp-token failed and no cached token available; falling back to anonymous")
        self._binary = None
        self._binary_resolved = False


def _detect_arch() -> str:
    mach = os.uname().machine.lower()
    if mach in ("x86_64", "amd64"):
        return "amd64"
    if mach in ("aarch64", "arm64"):
        return "arm64"
    raise RuntimeError(f"unsupported architecture: {mach}")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_token_provider(settings: "Settings") -> TokenProvider:  # type: ignore[name-defined]  # noqa: F821
    """Select appropriate TokenProvider from Settings.
    Priority: App > TokenCommand > PAT > mcp-token > anonymous.
    """
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

    return McpTokenProvider(settings.data_dir)
