# Detailed Design: GitHub App Auth (Short-Lived Tokens)

## 1. Purpose

Enable synchronization using short-lived GitHub App installation access tokens (1-hour lifespan) instead of static personal access tokens (`GITHUB_TOKEN`). This removes dependencies on external helper binaries in Docker setups and allows secure credential management inside standard HTTP/SSE transports.

---

## 2. In-Process Token Lifecycle

Initially, Shiori only required tokens during one-shot CLI ingestion jobs. However, with the addition of automatic background sync polling (`SHIORI_SYNC_INTERVAL_SECONDS`) and the `shiori_ingest` tool, the active `app` daemon must also communicate with GitHub. 

Shiori handles authentication via a `TokenProvider` abstraction layer.

### Resolution Priority
`build_token_provider()` evaluates credentials in the following sequence:
**GitHub App → TokenCommand (`GITHUB_TOKEN_COMMAND`) → Static PAT (`GITHUB_TOKEN`) → Anonymous**. 

If both a static PAT and a token command are configured, the command takes priority and logs an informational notice. If a selected provider fails to fetch a token, the sync fails with a `RuntimeError` and logs the error in `shiori_status.last_error` rather than silently downgrading to anonymous.

---

## 3. Git Header Injection

Rather than storing tokens in plain-text inside `.git/config` clone URLs (which persist on disk and cause authorization errors once expired), Shiori injects tokens dynamically using Git's `http.extraHeader` configuration variable.

### Headers Ingestion Loop
1.  **Header Generation**: The helper `_auth_args(provider)` reads the current token, encodes it in base64 as `x-access-token:{token}`, and formats it as `http.extraHeader=Authorization: Basic <base64>`.
2.  **Dynamic Inject**: For all network operations (`git clone` or `git fetch`), the arguments are inserted immediately after `git` (e.g. `git -c http.extraHeader="..." fetch`).
3.  **Config Sanitization**: Before executing fetch updates, the server runs `git remote set-url origin https://github.com/{repo}.git` to sanitize any legacy plain-text tokens saved in `.git/config`.

For API queries via `httpx`, the client uses a custom `_GitHubAuth` helper to dynamically refresh and inject `Authorization: Bearer <token>` headers before executing page requests.

---

## 4. Configuration Variables

| Key | Description |
| --- | --- |
| `GITHUB_APP_ID` | The GitHub App ID. |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Path to the private key PEM file. Defaults to `/run/secrets/github_app_key`. |
| `GITHUB_APP_PRIVATE_KEY` | Plain text PEM payload (fallback if key path is empty). |
| `GITHUB_APP_INSTALLATION_ID` | The App Installation ID. |
| `GITHUB_TOKEN` | Static personal access token (fallback). |
| `GITHUB_TOKEN_COMMAND` | CLI command used to fetch short-lived tokens on the host. |

---

## 5. Implementation

### Token Provider Classes (`src/shiori/github_auth.py`)

```python
import time
import httpx
import jwt  # pyjwt[crypto]

class TokenProvider:
    def get_token(self) -> str | None:
        raise NotImplementedError

class AnonymousProvider(TokenProvider):
    def get_token(self) -> str | None:
        return None

class StaticTokenProvider(TokenProvider):
    def __init__(self, token: str):
        self.token = token
    def get_token(self) -> str | None:
        return self.token

class AppTokenProvider(TokenProvider):
    REFRESH_BEFORE = 300  # Refresh token 5 minutes before expiration

    def __init__(self, app_id: str, private_key_pem: str, installation_id: str):
        self._app_id = app_id
        self._key = private_key_pem
        self._installation_id = installation_id
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get_token(self) -> str | None:
        if self._token is None or time.time() > self._expires_at - self.REFRESH_BEFORE:
            self._refresh()
        return self._token

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60, # Clock drift offset
            "exp": now + 540,
            "iss": self._app_id
        }
        return jwt.encode(payload, self._key, algorithm="RS256")

    def _refresh(self) -> None:
        # Request installation token and update cache
        ...
```

---

## 6. Target Permissions

Configure the GitHub App with the following minimal scopes:
*   **Contents**: Read-only
*   **Issues**: Read-only
*   **Pull Requests**: Read-only
*   *Note*: Webhooks can be disabled.
