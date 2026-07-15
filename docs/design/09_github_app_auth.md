# Detailed Design: GitHub App Auth (Short-Lived Tokens)

## Purpose

Enable synchronization using short-lived GitHub App installation access tokens (1-hour lifespan) instead of static personal access tokens (`GITHUB_TOKEN`). This removes dependencies on external helper binaries in Docker setups and allows secure credential management inside standard HTTP/SSE transports.

## Background

Initially, the design assumed that "Shiori only uses tokens during one-shot ingestion jobs, and the resident MCP server daemon does not access GitHub." However, with the addition of automatic background sync polling (`SHIORI_SYNC_INTERVAL_SECONDS`) and the `shiori_ingest` tool (Issue #6), the active `app` daemon must also communicate with GitHub. 

Under this design, all services (`app` and `ingest`) consume **short-lived GitHub App installation tokens minted host-side**. `build_token_provider()` automatically evaluates credentials and selects the appropriate provider (TokenSocket, TokenCommand, PAT, or Anonymous) based on environment configuration. The GitHub App private key itself never enters shiori or a container — it lives in the host keyring and is used by the host-side minter (`mcp-token`).

The core principle remains unchanged: tokens are resolved within the active job context and can be refreshed on-demand to support long-running tasks.

## Decisions

1.  **Unified `TokenProvider` Abstraction**: Authentication logic is consolidated under a shared `TokenProvider` interface. It provides implementations: TokenSocket (mint socket), TokenCommand, Static Token (PAT), and Anonymous. Core sync logic (`github_sync`) only interacts with credentials via this provider. There is deliberately **no in-container GitHub App provider** — carrying the App PEM into the container (`AppTokenProvider`) was retired in #243 (under EPIC #237); see [Token Supply Path](15_token_supply_path.md).
2.  **Provider Precedence**: Evaluates credentials in the order: **TokenSocket > TokenCommand > Static PAT > Anonymous**.
    *   *TokenSocket*: Used if `GITHUB_TOKEN_SOCKET` is specified (e.g. `/run/shiori/mint.sock`), connecting to a host-side systemd socket-activated service that mints tokens on demand (detailed design/15). This is the current target for both containerized (WSL, GCP VM) deployments.
    *   *TokenCommand*: Used if `GITHUB_TOKEN_COMMAND` is specified (e.g. `mcp-token github`), running the command to fetch tokens dynamically. Used for host-process (native venv) deployments.
    *   *PAT*: Used if `GITHUB_TOKEN` is set.
    *   *Anonymous*: Fallback when no keys are provided.
    *   If both `TokenSocket` and `TokenCommand`/`GITHUB_TOKEN` are set, the socket takes priority and logs an informational notice.
3.  **On-Demand Token Refresh**: Tokens are cached in-memory by the provider and refreshed 5 minutes before expiration. This ensures tokens do not expire mid-run during slow initial ingestion jobs (which can take over an hour for CPU-heavy embeddings).
4.  **Dynamic Git URL Swapping**: To prevent storing plain-text tokens permanently in `.git/config` (which causes authorization failures once tokens expire), Shiori dynamically swaps the remote URL with an authenticated one (using `x-access-token`) immediately before running network operations (`clone`/`fetch`), and restores the unauthenticated URL in a `finally` block. This approach was chosen instead of `http.extraHeader` to ensure compatibility across older Git versions.
5.  **No key propagation into compose**: The App private key is **not** passed to the compose services. It stays in the host keyring; `app` / `ingest` receive only a minted token, pulled from the host via `GITHUB_TOKEN_SOCKET` (bind-mounted socket directory). When using a PAT, `GITHUB_TOKEN` is passed to all services instead. (Historically the PEM was mounted as a Docker secret — Strategy A — now retired in #243.)

---

## Authentication Schemes by Environment

> **The choice of credential pathway is detailed in [Token Supply Path](15_token_supply_path.md).** This section provides a brief summary from the perspective of the provider implementation.

The selection depends on two variables: **where the token is consumed** and **who is responsible for updating it**.

| Environment | Consumer | Method |
| --- | --- | --- |
| Compose Deployment (`app` / `ingest`) | In-container process | **Mint Socket** (current, WSL + GCP VM): A host-side systemd socket-activated service **owned by mcp-launcher, not shiori** (see mcp-launcher#42; shiori carries no socket unit of its own) executes `mcp-token github` on connection and streams the token back. The container mounts the socket's parent **directory** (`${SHIORI_MINT_SOCKET_DIR:-${XDG_RUNTIME_DIR}/mcp-token}:/run/shiori:rw` -- a directory mount, never a file mount; see detailed design/15 for why) and uses `GITHUB_TOKEN_SOCKET=/run/shiori/mint.sock` to pull tokens on demand. Pull-based, no clock-drift windows, and **the App private key never enters the container** (Issue #204 / mcp-launcher#42). |
| Native Run (host venv) | Host process | **TokenCommand Execution**: The process mints tokens directly by calling `GITHUB_TOKEN_COMMAND=mcp-token github`, fetching credentials directly from the host OS keyring. |

> **Retired: in-container App private key (Strategy A).** Compose deployments used to mount `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY_PATH` as read-only Docker secrets and let the container mint its own tokens (`AppTokenProvider`, "Active in GCP VMs (dev-infra#4 / #5)"). This was retired in **#243** (EPIC #237): it carried the long-lived App PEM into the container and onto disk. The GCP VM now uses the Mint Socket like WSL; the App key stays in the host keyring. See [Token Supply Path](15_token_supply_path.md).

Compose methods are opt-in. If unconfigured, the server falls back to anonymous mode, allowing setup guides for public-only repositories to function out-of-the-box. If TokenSocket and TokenCommand/PAT are all set, the socket is selected based on provider precedence (TokenSocket > TokenCommand > PAT > Anonymous).

### Removed Path: In-Container Minting (Legacy `McpTokenProvider`)
The `McpTokenProvider` (introduced in Issue #170 / #173, which dynamically resolved the `mcp-token` binary inside the container to mint tokens) is **conceptually invalid**. 
Keyring integration via the D-Bus Secret Service restricts access strictly to the owner UID of the desktop session bus. While the container can resolve the binary file on disk, attempting to mint credentials fails and **silently falls back to anonymous access with a single warning line**, masking sync failures (Issue #188).

For in-container deployments, the recommended approach is now the **Mint Socket** (`TokenSocketProvider`, `GITHUB_TOKEN_SOCKET`), which uses systemd socket activation to pull tokens on demand without exposing the private key to the container. For host-side usage, `GITHUB_TOKEN_COMMAND=mcp-token github` via `TokenCommandProvider` is sufficient.

The legacy provider has been removed. The active provider hierarchy is **TokenSocket > TokenCommand > PAT > Anonymous**. If a configured provider fails to resolve a token, it raises a `RuntimeError` rather than silently downgrading to anonymous (the error is caught by `record_sync_attempt` and displayed in `shiori_status.last_error`).

The `token_socket` provider connects to a Unix socket, receives the token, and caches it for 55 minutes (re-fetching 5 minutes before expiry). On socket failure, it falls back to a cached token for up to 60 minutes before raising `RuntimeError`.

The active provider can be inspected via the `token_provider` field in `shiori_status` (`token_socket`, `token_command`, `static`, `anonymous`, or `error`). If provider construction itself fails, the status endpoint remains accessible but reports `token_provider: "error"` and logs the exception in warning arrays (Issue #193).

---

## Configuration Variables

The App private key is **not** a shiori configuration variable anymore — it lives in the host keyring and is used by the host-side minter (`mcp-token`). Shiori only sees the *minted token*, via one of:

| Variable | Description |
| --- | --- |
| `GITHUB_TOKEN_SOCKET` | Path to a Unix socket for on-demand token minting (e.g. `/run/shiori/mint.sock`, inside the container). The socket is served by a host-side systemd socket-activated service **owned by mcp-launcher** (mcp-launcher#42) that runs `mcp-token github` on each connection; shiori is a pure consumer and has no socket unit of its own. Highest precedence. |
| `GITHUB_TOKEN_COMMAND` | In-process command to execute (e.g. `mcp-token github`). stdout must yield the token. For host-process deployments. |
| `GITHUB_TOKEN` | Personal Access Token (fallback / public-repo use). |

> **Removed variables (#243):** `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_APP_PRIVATE_KEY_PATH`. These configured the retired in-container `AppTokenProvider`. The App identity now lives only where the minter runs (mcp-token's keyring), never in shiori's environment.

---

## Implementation Details

### 1. Unified Authentication Provider (`src/shiori/github_auth.py`)

```python
"""GitHub Authentication. Abstracts host-minted tokens into TokenProviders."""
from __future__ import annotations

import logging
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


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


# TokenCommandProvider (mcp-token github) and TokenSocketProvider
# (GITHUB_TOKEN_SOCKET) live here too -- both pull a host-minted token, cache it
# ~55 min, and re-fetch 5 min before expiry. See src/shiori/github_auth.py.
# There is no in-container AppTokenProvider: the App PEM stays host-side (#243).


def build_token_provider(settings: "Settings") -> TokenProvider:
    """Priority: TokenSocket > TokenCommand > PAT > anonymous.

    No in-container GitHub App branch: the App key is minted host-side by
    mcp-token and pulled through the socket/command (#243, EPIC #237).
    """
    if settings.github_token_socket:
        return TokenSocketProvider(settings.github_token_socket)
    if settings.github_token_command:
        return TokenCommandProvider(settings.github_token_command)
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
        # TokenSocket first (above)
        ...
        # Next, TokenCommand
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

#### (a) Dynamic Git Authentication via Temporary URL Swapping
Because `http.extraHeader` fails to authenticate consistently on certain older Git versions, Shiori resolves Git authentication by temporarily modifying the origin remote URL to include the token, executing the network commands, and resetting it back to the unauthenticated URL in a `finally` block.

```python
def _authed_url(remote: str, provider: TokenProvider) -> str:
    """Return remote URL with token embedded (x-access-token scheme)."""
    token = provider.get_token()
    if not token:
        return remote
    return remote.replace("https://", f"https://x-access-token:{token}@", 1)
```

*   **Temporary Set-Url Wrap**:
    During execution, the origin remote is updated using `remote set-url`, the fetch or clone is performed, and the unauthenticated URL is restored:
    ```python
    remote = _git(["remote", "get-url", "origin"], cwd=repo_dir)
    authed_remote = _authed_url(remote, provider)
    _git(["remote", "set-url", "origin", authed_remote], cwd=repo_dir)
    try:
        _git(["fetch", "origin", "--depth=1"], cwd=repo_dir)
    finally:
        _git(["remote", "set-url", "origin", remote], cwd=repo_dir)
    ```
*   `_auth_args()` is kept for backwards compatibility but returns `[]` to bypass `http.extraHeader` injection.
*   This temporary swapping ensures the plain-text token is never written permanently to `.git/config` on disk.

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

The compose services carry **no App private key**. They receive a minted token, pulled from the host mint socket. The socket's parent **directory** is bind-mounted (never the socket file — see detailed design/15), and `GITHUB_TOKEN_SOCKET` points at it:

```yaml
services:
  app:
    environment:
      GITHUB_TOKEN: ${GITHUB_TOKEN:-}
      GITHUB_TOKEN_SOCKET: ${GITHUB_TOKEN_SOCKET:-}
      GITHUB_TOKEN_COMMAND: ${GITHUB_TOKEN_COMMAND:-}
    volumes:
      # SHIORI_MINT_SOCKET_DIR defaults to ${XDG_RUNTIME_DIR}/mcp-token
      - ${SHIORI_MINT_SOCKET_DIR:-${XDG_RUNTIME_DIR}/mcp-token}:/run/shiori:rw
    ...
  ingest:
    profiles: ["ingest"]
    image: shiori-app
    command: python -m shiori ingest
    environment:
      GITHUB_TOKEN_SOCKET: ${GITHUB_TOKEN_SOCKET:-}
      GITHUB_TOKEN_COMMAND: ${GITHUB_TOKEN_COMMAND:-}
    volumes:
      - ${SHIORI_MINT_SOCKET_DIR:-${XDG_RUNTIME_DIR}/mcp-token}:/run/shiori:rw
    depends_on: [db]
```

Run manual ingestion via: `docker compose run --rm ingest`. Set `GITHUB_TOKEN_SOCKET=/run/shiori/mint.sock` in `.env`. (The retired Strategy A mounted `./secrets/github-app.private-key.pem` as a Docker secret; that path and the `GITHUB_APP_*` env are gone — #243.)

---

## GitHub App Minimal Permissions

The App still exists — but it is used **host-side by `mcp-token`**, not by shiori. Configure it with the following read-only permissions (unchanged):
*   **Contents**: Read-only (cloning code)
*   **Issues**: Read-only (Timeline extraction)
*   **Pull Requests**: Read-only (Timeline and reviews)

---

## Edge Cases

*   **Refresh Network Loss**: If the mint socket / command fails, the provider falls back to the cached token if it is still within its 60-minute window. If none is left, sync execution halts (rather than silently degrading to anonymous — issue #188).
*   **Access Denied (403)**: A minted token that lacks access surfaces the API error; check the App's repo selection and permissions (host-side).

> JWT/clock-skew handling moved host-side with the minter. Shiori no longer signs App JWTs (`AppTokenProvider` retired, #243).

---

## Test Scenarios

1.  **`build_token_provider` Precedence**: Verify TokenSocket > TokenCommand > PAT > Anonymous selection, and that an empty `GITHUB_TOKEN_COMMAND` is treated as unset (#198).
2.  **`TokenSocketProvider`**: fetch, cache (single connect within window), empty-response raises, and cached-token fallback on connect failure.
3.  **`TokenCommandProvider`**: command runs, cache lifespan, empty-output raises, and cache fallback on subprocess failure.
4.  **Git Config Sanitization**: Verify that legacy plain-text tokens inside `.git/config` are purged before execution.
