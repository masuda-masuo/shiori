"""GitHub authentication (detailed design/09).
Unifies PAT and host-minted installation tokens under a TokenProvider abstraction.
Decisions:
- TokenSocket preferred, then TokenCommand, then GITHUB_TOKEN, then anonymous.
- Short-lived tokens are minted host-side (mcp-token) and pulled on demand; the
  App private key never enters this process or a container.

Which provider a deployment should use is decided by *where the token is
consumed* and *who is responsible for refreshing it* -- see detailed design/15
(トークン供給経路) for the full rule and the deployment matrix. In short:

- Consumer is a host process that can reach the OS keystore: mint on demand with
  ``GITHUB_TOKEN_COMMAND=mcp-token github`` (TokenCommandProvider). No secret on
  disk.
- Consumer is inside a container (the compose ``app`` / ``ingest`` services): the
  keystore's D-Bus Secret Service only accepts the bus-owner UID, so it is not
  reachable from in there. The token is pulled from a host-side mint socket
  (``GITHUB_TOKEN_SOCKET``, TokenSocketProvider) -- a systemd socket-activated
  service that mints on each connection. The App private key stays host-side.

There is deliberately **no in-container GitHub App provider and no provider that
mints from the OS keystore inside a container**. The retired ``AppTokenProvider``
carried the long-lived App PEM into the container to self-refresh (#243 under
EPIC #237); the retired ``McpTokenProvider`` resolved the mcp-token binary (just
a file, so that step succeeded), failed at the mint step, and fell back to
anonymous behind a single warning line -- a silent downgrade that hid a real
outage (issue #188). Both are replaced by pulling a host-minted token: the PEM
never leaves the host, and a mint failure surfaces instead of degrading.
"""

from __future__ import annotations

import logging
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import ClassVar

log = logging.getLogger(__name__)

API = "https://api.github.com"


class TokenProvider:
    """Abstract token supplier. get_token() returning None means anonymous (no auth)."""

    #: Stable identifier surfaced via shiori_status's token_provider field
    #: (issue #188). Subclasses override with their own name.
    name: ClassVar[str] = "unknown"

    def get_token(self) -> str | None:
        raise NotImplementedError


class AnonymousProvider(TokenProvider):
    """No authentication. Public repos only (strict rate limits).

    This is the terminal case of build_token_provider(): it is selected only when
    *nothing* is configured, which is the documented "public repos need no auth"
    path. No other provider silently degrades into it -- a configured provider
    that cannot produce a token raises instead (issue #188).
    """

    name: ClassVar[str] = "anonymous"

    def get_token(self) -> str | None:
        return None


@dataclass
class StaticTokenProvider(TokenProvider):
    """Static token, e.g. long-lived PAT."""

    name: ClassVar[str] = "static"

    token: str

    def get_token(self) -> str | None:
        return self.token


class TokenCommandProvider(TokenProvider):
    """Calls an external command (``GITHUB_TOKEN_COMMAND``) to obtain a token.

    Two shapes are in use (detailed design/15):

    - ``mcp-token github`` -- consumer is a host process; the command mints a
      fresh short-lived token straight from the OS keystore.
    - ``cat /run/shiori/github-token`` -- consumer is a container; the command
      reads a token a host-side minter has already written to a bind-mounted
      file (the token-file bridge).

    Caches for 55 min, re-runs 5 min before expiry, falls back to the cached
    token for up to HARD_EXPIRY on command failure. Raises when the command fails
    and no usable cached token is left: a command that was explicitly configured
    and does not work is an outage, not a reason to quietly go anonymous.
    """

    name: ClassVar[str] = "token_command"

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


class TokenSocketProvider(TokenProvider):
    """Connects to a Unix socket (``GITHUB_TOKEN_SOCKET``) to obtain a token.

    The socket is served by a host-side systemd socket-activated service that
    shiori does not own -- it is installed and maintained by mcp-launcher
    (mcp-launcher#42), which runs ``mcp-token github`` on each connection and
    streams the token back (detailed design/15). Shiori is a pure consumer
    of this contract: connection = request, no payload; read until EOF and
    strip the result.

    Caches for 55 min, re-fetches 5 min before expiry, falls back to the
    cached token for up to HARD_EXPIRY on failure. Raises when the socket
    fails and no usable cached token is left.

    Expiry bookkeeping uses the wall clock (``time.time()``), never a
    monotonic clock: a monotonic clock does not advance while the host is
    suspended, which would silently reintroduce the clock-drift bug this
    provider exists to eliminate (see detailed design/15).
    """

    name: ClassVar[str] = "token_socket"

    CACHE_SECONDS = 3300      # 55 min
    REFRESH_BEFORE = 300      # 5 min
    HARD_EXPIRY = 3600        # 60 min — fallback window on fetch failure

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._token: str | None = None
        self._fetched_at: float = 0.0

    def get_token(self) -> str | None:
        if self._token is None or time.time() > self._fetched_at + self.CACHE_SECONDS - self.REFRESH_BEFORE:
            self._refresh()
        return self._token

    def _refresh(self) -> None:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(15.0)
            sock.connect(self._socket_path)
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            sock.close()
            token = data.decode("utf-8").strip()
            if token:
                self._token = token
                self._fetched_at = time.time()
                return
            log.warning("token socket returned empty response from %s", self._socket_path)
        except OSError as exc:
            log.warning("token socket connect/read failed: %s", exc)
        # fallback
        if self._token and time.time() < self._fetched_at + self.HARD_EXPIRY:
            log.warning("token socket refresh failed; reusing cached token")
            return
        raise RuntimeError(
            f"token socket {self._socket_path} failed and no cached token available"
        )


def build_token_provider(settings: "Settings") -> TokenProvider:  # type: ignore[name-defined]  # noqa: F821
    """Select appropriate TokenProvider from Settings.
    Priority: TokenSocket > TokenCommand > PAT > anonymous.

    There is deliberately no in-container GitHub App provider: the App private
    key is minted host-side by mcp-token and pulled through the mint socket
    (TokenSocketProvider) or a host command (TokenCommandProvider). Placing the
    long-lived PEM inside the container was retired (#243 under EPIC #237).
    """
    if settings.github_token_socket:
        if settings.github_token_command or settings.github_token:
            log.info("GITHUB_TOKEN_SOCKET takes priority over token command/PAT")
        return TokenSocketProvider(settings.github_token_socket)

    if settings.github_token_command:
        if settings.github_token:
            log.info("GITHUB_TOKEN_COMMAND takes priority over GITHUB_TOKEN")
        return TokenCommandProvider(settings.github_token_command)

    if settings.github_token:
        if settings.github_token.startswith("ghs_"):
            log.warning(
                "GITHUB_TOKEN starts with 'ghs_' (a GitHub App installation "
                "token, which expires in about 1 hour). It will be used as a "
                "static token and will silently stop working once it expires. "
                "Prefer GITHUB_TOKEN_SOCKET or GITHUB_TOKEN_COMMAND for a "
                "token that refreshes itself (issue #187)."
            )
        return StaticTokenProvider(settings.github_token)

    # Nothing configured. Public repos only -- an explicit terminal state, not a
    # degraded one (issue #188).
    return AnonymousProvider()
