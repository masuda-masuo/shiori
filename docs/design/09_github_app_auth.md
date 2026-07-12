# Detailed Design: GitHub App Auth (Short-Lived Tokens)

## Purpose

Enable synchronization using short-lived GitHub App installation access tokens (1-hour lifespan) instead of static personal access tokens (`GITHUB_TOKEN`). This removes dependencies on external helper binaries in Docker setups and allows secure credential management inside standard HTTP/SSE transports.

## Background

Initially, the design assumed that "Shiori only uses tokens during one-shot ingestion jobs, and the resident MCP server daemon does not access GitHub." However, with the addition of automatic background sync polling (`SHIORI_SYNC_INTERVAL_SECONDS`) and the `shiori_ingest` tool (Issue #6), the active `app` daemon must also communicate with GitHub. 

Under this design, all services (`app` and `ingest`) use the GitHub App authentication model. `build_token_provider()` automatically evaluates credentials and selects the appropriate provider (App, PAT, or Anonymous) based on environment configuration. 

The core principle remains unchanged: tokens are resolved within the active job context and can be refreshed on-demand to support long-running tasks.

## Decisions

1.  **Unified `TokenProvider` Abstraction**: Authentication logic is consolidated under a shared `TokenProvider` interface. It provides three implementations: Static Token (PAT), GitHub App, and External Command (TokenCommand). Core sync logic (`github_sync`) only interacts with credentials via this provider.
2.  **Provider Precedence**: Evaluates credentials in the order: **GitHub App > TokenCommand > Static PAT > Anonymous**.
    *   *App*: Used if GITHUB_APP settings are fully defined.
    *   *TokenCommand*: Used if `GITHUB_TOKEN_COMMAND` is specified (e.g. `mcp-token github`), running the command to fetch tokens dynamically.
    *   *PAT*: Used if `GITHUB_TOKEN` is set.
    *   *Anonymous*: Fallback when no keys are provided.
    *   If both `TokenCommand` and `GITHUB_TOKEN` are set, the command takes priority and logs an informational notice.
3.  **On-Demand Token Refresh**: Tokens are cached in-memory by the provider and refreshed 5 minutes before expiration. This ensures tokens do not expire mid-run during slow initial ingestion jobs (which can take over an hour for CPU-heavy embeddings).
4.  **Header Injection (`http.extraHeader`)**: We deprecate embedding tokens in repository remote URLs. In older implementations, plain-text tokens were written to `.git/config` (persisting on disk/named volumes), causing subsequent pulls to fail once the token expired. Instead, tokens are dynamically injected via Git's `http.extraHeader` config flag during execution.
5.  **Key Propagation in Compose**: The GitHub App private key is passed to all compose services (`app` and `ingest`) using secrets and environment configurations. `build_token_provider()` resolves the active provider on-demand. When using PATs, `GITHUB_TOKEN` is similarly passed to all services.
6.  **Dependency Addition**: Added `pyjwt[crypto]` to support RS256 signature signing via cryptography.

---

## Authentication Schemes by Environment

> **The choice of credential pathway is detailed in [Token Supply Path](15_token_supply_path.md).** This section provides a brief summary from the perspective of the provider implementation.

The selection depends on two variables: **where the token is consumed** and **who is responsible for updating it**.

| Environment | Consumer | Method |
| --- | --- | --- |
| Compose Deployment (`app` / `ingest`) | In-container process | **GitHub App Private Key**: Mounts `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY_PATH` as read-only Docker secrets. The container process generates and refreshes its own tokens, removing host-side dependencies. Active in GCP VMs (dev-infra#4 / #5). |
| Compose Deployment (`app` / `ingest`) | In-container process | **Token-File Sharing**: The host runs `scripts/refresh-token.sh` (triggered by `mcp-token` and systemd timers) to mint tokens and write them to `runtime/github-token`. The container mounts `./runtime:/run/shiori:ro` and reads it via `GITHUB_TOKEN_COMMAND=cat /run/shiori/github-token`. No keys are written to the container or disk, but the host-side timer is a load-bearing dependency (Issues #150 / #188 / #198). |
| Native Run (host venv) | Host process | **TokenCommand Execution**: The process mints tokens directly by calling `GITHUB_TOKEN_COMMAND=mcp-token github`, fetching credentials directly from the host OS keyring. |

Compose methods are opt-in. If unconfigured, the server falls back to anonymous mode, allowing setup guides for public-only repositories to function out-of-the-box. If both App and TokenCommand are set, the App is selected based on provider precedence.

### Removed Path: In-Container Minting (Legacy `McpTokenProvider`)
The `McpTokenProvider` (introduced in Issue #170 / #173, which dynamically resolved the `mcp-token` binary inside the container to mint tokens) is **conceptually invalid**. 
Keyring integration via the D-Bus Secret Service restricts access strictly to the owner UID of the desktop session bus. While the container can resolve the binary file on disk, attempting to mint credentials fails and **silently falls back to anonymous access with a single warning line**, masking sync failures (Issue #188).

To execute native host-level minting, configuring `GITHUB_TOKEN_COMMAND=mcp-token github` via the `TokenCommandProvider` is sufficient. The legacy provider has been removed. The active provider hierarchy is simplified to **App > TokenCommand > PAT > Anonymous**. If a configured provider fails to resolve a token, it raises a `RuntimeError` rather than silently downgrading to anonymous (the error is caught by `record_sync_attempt` and displayed in `shiori_status.last_error`).

If `GITHUB_TOKEN_COMMAND` is set but the target file `runtime/github-token` is missing or empty:
`TokenCommandProvider._refresh()` catches the failure (non-zero exit code or empty stdout), logs a warning (`token command returned empty output`), and raises a `RuntimeError` if no cached token is available. This surfaces as a sync exception and is tracked under `shiori_status.last_error` (aligning with Issue #192).

The active provider can be inspected via the `token_provider` field in `shiori_status` (`app`, `static`, `token_command`, `anonymous`, or `error`). If provider construction itself fails (e.g., partially defined App configs), the status endpoint remains accessible but reports `token_provider: "error"` and logs the exception in warning arrays (Issue #193).

---

## Configuration Variables

| Variable | Description |
| --- | --- |
| `GITHUB_APP_ID` | The GitHub App ID (numeric string). |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Path to the private key PEM file. Defaults to `/run/secrets/github_app_key`. |
| `GITHUB_APP_PRIVATE_KEY` | Raw PEM key contents (used if key path is empty). |
| `GITHUB_APP_INSTALLATION_ID` | The App Installation ID (numeric string). |
| `GITHUB_TOKEN` | Personal Access Token (fallback). |
| `GITHUB_TOKEN_COMMAND` | In-process command to execute (e.g. `mcp-token github`). stdout must yield the token. |

To initialize App credentials, both `GITHUB_APP_ID` and `GITHUB_APP_INSTALLATION_ID` must be defined, and the private key must be readable. Partially defined environments raise a `ValueError` during startup.

---

## Implementation Details

### 1. Unified Authentication Provider (`src/shiori/github_auth.py`)

```python
"""GitHub Authentication. Abstracts PAT and GitHub App installation tokens into TokenProviders."""
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
    def get_token(self) -> str | None:  # None represents anonymous
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
    REFRESH_BEFORE = 300  # Refresh token 5 minutes before expiry

    def __init__(self, app_id: str, private_key_pem: str, installation_id: str):
        self._app_id = app_id
        self._key = private_key_pem
        self._installation_id = installation_id
        self._token: str | None = None
        self._expires_at: float = 0.0  # Epoch timestamp in seconds

    def get_token(self) -> str | None:
        if self._token is None or time.time() > self._expires_at - self.REFRESH_BEFORE:
            self._refresh()
        return self._token

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        # Offset iat by 60 seconds to absorb clock drift. Expire in 9 minutes (under the 10 min limit).
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
                "GitHub App JWT rejected. Check the GITHUB_APP_ID, private key mapping, "
                "and server system time."
            )
        if resp.status_code == 403:
            raise RuntimeError(
                "GitHub App lacks required permissions. Verify App permissions (Contents/Issues/"
                "PR: Read) and target installation repositories."
            )
        if resp.status_code == 404:
            raise RuntimeError(
                "Installation not found. Check GITHUB_APP_INSTALLATION_ID and verify the App "
                "is installed on the target repository."
            )
        resp.raise_for_status()  # Expected: 201 Created
        data = resp.json()
        self._token = data["token"]
        # expires_at: "2026-06-11T12:34:56Z" -> Convert directly to UTC epoch seconds
        self._expires_at = calendar.timegm(
            time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
        )
        log.info("issued installation token (expires_at=%s)", data["expires_at"])


def build_token_provider(settings: "Settings") -> TokenProvider:
    pem = settings.github_app_private_key()  # Returns PEM contents (file reads prioritize path settings)
    app_vars = [settings.github_app_id, settings.github_app_installation_id, pem]
    if any(app_vars):
        if not all(app_vars):
            raise ValueError(
                "Incomplete GitHub App configuration: Specify GITHUB_APP_ID, "
                "GITHUB_APP_PRIVATE_KEY(_PATH), and GITHUB_APP_INSTALLATION_ID together."
            )
        if settings.github_token:
            log.info("Both GITHUB_TOKEN and App settings defined. Prioritizing GitHub App.")
        return AppTokenProvider(
            settings.github_app_id, pem, settings.github_app_installation_id
        )
    if settings.github_token:
        return StaticTokenProvider(settings.github_token)
    return AnonymousProvider()
```

### 2. TokenCommandProvider Implementation

The `TokenCommandProvider` runs host-side commands to fetch tokens. The cache duration is set to 55 minutes. On command failures, it falls back to the active cache if it is still within its 60-minute window.

To support this:
*   Add `github_token_command` to `Settings` (maps to `GITHUB_TOKEN_COMMAND`).
*   Update `build_token_provider()` precedence:
    ```python
    def build_token_provider(settings: "Settings") -> TokenProvider:
        # Prioritize App (above)
        ...
        # Next, fallback to TokenCommand
        if settings.github_token_command:
            if settings.github_token:
                log.info("GITHUB_TOKEN_COMMAND takes priority over GITHUB_TOKEN")
            return TokenCommandProvider(settings.github_token_command)
        # Static PAT fallback
        if settings.github_token:
            return StaticTokenProvider(settings.github_token)
        return AnonymousProvider()
    ```

### 3. Git Integration Updates (`github_sync.py`)

Update sync function signatures:
```python
def sync_docs(settings, conn, embedder, repo, provider: TokenProvider) -> int: ...
def sync_issues(settings, conn, embedder, repo, provider: TokenProvider) -> int: ...
```

#### (a) Dynamic Git Authentication via `extraHeader`
```python
import base64

def _auth_args(provider: TokenProvider) -> list[str]:
    token = provider.get_token()
    if not token:
        return []
    b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {b64}"]
```

*   `_clone_url` is removed; the remote origin points to `https://github.com/{repo}.git` without credentials.
*   For all network commands (`clone`/`fetch`), prepend auth headers: `_git(_auth_args(provider) + ["fetch", ...], cwd=...)`.
*   Prior to executing network tasks, the server updates remote origins to purge plain-text credentials saved in legacy configurations:
    `_git(["remote", "set-url", "origin", f"https://github.com/{repo}.git"], cwd=repo_dir)`

#### (b) API Requests Integration (`httpx.Auth` Hook)
```python
class _GitHubAuth(httpx.Auth):
    def __init__(self, provider: TokenProvider):
        self._provider = provider

    def auth_flow(self, request):
        token = self._provider.get_token()  # Auto-refreshes if near expiration
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request
```

API clients are initialized as `httpx.Client(headers=headers, auth=_GitHubAuth(provider), timeout=30.0)`.

---

## Ingest In-Process Operations

During CLI runs, the `ingest` command creates a single `TokenProvider` instance and passes it to all repository sync workers, allowing token caches and refresh loops to share execution contexts.

---

## Docker Compose Configurations

```yaml
secrets:
  github_app_key:
    file: ./secrets/github-app.private-key.pem

services:
  app:
    environment:
      GITHUB_TOKEN: ${GITHUB_TOKEN:-}
      GITHUB_APP_ID: ${GITHUB_APP_ID:-}
      GITHUB_APP_INSTALLATION_ID: ${GITHUB_APP_INSTALLATION_ID:-}
      GITHUB_APP_PRIVATE_KEY_PATH: ${GITHUB_APP_PRIVATE_KEY_PATH:-/run/secrets/github_app_key}
    secrets:
      - github_app_key
    ...
  ingest:
    profiles: ["ingest"]
    image: shiori-app
    command: python -m shiori ingest
    environment:
      GITHUB_APP_ID: ${GITHUB_APP_ID}
      GITHUB_APP_INSTALLATION_ID: ${GITHUB_APP_INSTALLATION_ID}
      GITHUB_APP_PRIVATE_KEY_PATH: /run/secrets/github_app_key
    secrets:
      - github_app_key
    depends_on: [db]
```

Run manual ingestion via: `docker compose run --rm ingest`. The `./secrets/` directory should be added to `.gitignore`.

---

## GitHub App Minimal Permissions

To index repositories, configure the App with the following read-only permissions:
*   **Contents**: Read-only (cloning code)
*   **Issues**: Read-only (Timeline extraction)
*   **Pull Requests**: Read-only (Timeline and reviews)

---

## Edge Cases

*   **Clock Skew**: Handles host/container clock skew by offsetting the JWT `iat` field by -60 seconds.
*   **Refresh Network Loss**: If connection is lost during a refresh attempt, the provider falls back to the cached token if it is still valid. If expired, sync execution halts.
*   **Access Denied (403)**: Surfaced with custom warnings instructing users to check App repo selections and scope configurations.

---

## Test Scenarios

1.  **`build_token_provider` Configurations**: Verify correct initialization priority (App > TokenCommand > PAT > Anonymous) and check ValueError exceptions on partial App configurations.
2.  **`AppTokenProvider` Refresh Loop**: Verify token caching, check refresh requests at expiration windows, mock API responses (401/403/404), and check cache fallbacks on HTTP connection drops.
3.  **`_app_jwt` Payload Fields**: Validate that RS256 encoded JWT payloads contain valid `iat`, `exp`, and `iss` values.
4.  **`_auth_args` Header Generation**: Confirm base64 authorization headers are generated correctly.
5.  **Git Config Sanitization**: Verify that legacy plain-text tokens inside `.git/config` are purged before execution.
6.  **`TokenCommandProvider` Cache**: Check command runs, cache lifespans, and check fallback logic.
