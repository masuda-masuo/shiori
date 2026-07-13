# Detailed Design: Token Supply Path

## Context

While [GitHub App Auth](09_github_app_auth.md) defines *how* short-lived tokens are minted, this document defines **the paths used to deliver minted tokens to the consuming services**.

The choice of `TokenProvider` implementation is determined directly by the active **runtime environment**. The rules outlined here apply to both the Shiori server and external tools like Sunaba.

---

## Prerequisites: Non-Continuous Host Operation

This is the primary constraint that dictates the design. In this project:
*   **GCP VM (`mcp-host-vm`)**: Shuts down automatically via `mcp-autostop.timer` when no client traffic is detected (non-24h operation).
*   **WSL (Windows Local)**: Suspends when the Windows host machine enters sleep state, freezing the WSL VM.

**Designs that assume a "constantly running host process daemon" are invalid for both environments.**

---

## Core Principles

### Rule 1: For Container Consumers, Deliver Credentials to the Consumer Side
Keystore daemons (D-Bus Secret Service) restrict access strictly to the **owner UID of the desktop session bus**. Because containers run in separate UID namespaces, they cannot access the host bus. To run operations inside the container, we must **deliver the keys or tokens into the container**.

The legacy `McpTokenProvider` (which resolved the `mcp-token` binary inside the container to mint tokens) violated this rule. While it resolved the file path, execution failed and **silently fell back to anonymous access with a single warning line**, masking credential failures (Issue #188).

### Rule 2: In-Container Consumers Must Pull Tokens; Push-Based Timers are Invalid
Using a background host timer to pre-mint tokens and write them to a shared file (a "push" model) introduces three critical failure vectors:
1.  **Startup Latency**: If the container starts when the shared token file is expired, operations fail until the next timer tick.
2.  **Single Points of Failure**: If the timer service fails, sync operations fail after 60 minutes.
3.  **Clock-Drift and Suspension (Fatal)**: systemd `OnUnitActiveSec` timers run on the **monotonic clock**, which pauses when the host suspends. However, token lifespans decrease in **wall-clock** time. Upon waking, the shared token file is **expired, but the host timer assumes it still has time before the next trigger**, causing synchronization to fail with 401 errors for up to 5 minutes. *This cannot be solved by adjusting timer intervals.*

**Therefore: Tokens must be retrieved on-demand using a pull model triggered when the consumer starts an operation.**

---

## Ingest Authentication Options

| Strategy | Private Key Location | Refresh Control | Lifespan | Current Target |
| --- | --- | --- | --- | --- |
| **A. App Private Key** | **Container (PEM)** | Consumer (Pull) | ◯ No drift windows | GCP VM |
| **B. Mint Socket** | Host Keyring | Consumer (Pull) | ◯ No drift windows | WSL (current) |
| **C. Token-File Share** (Retired) | Host Keyring | Host Timer (**Push**) | **✗ Drift windows** | **Removed** (Replaced by Mint Socket in #204) |
| **D. In-Container Mint** | Keystore (Unreachable) | — | — | **Removed** (Legacy `McpTokenProvider`) |
| **E. Native Host Run** | Host Keyring | Consumer (Pull) | ◯ No drift windows | Host venv / Sunaba |

Strategy A (App Private Key) sacrifices a security parameter:
*   **Strategy A** is pull-based (no clock-drift windows), but **exposes the long-lived App private key (PEM) inside the container and on disk**. If leaked, the App is compromised indefinitely.

Strategy B (Mint Socket) resolves this by keeping the private key on the host and using pull-based socket activation, eliminating both the clock-drift window and the disk exposure. It replaces the retired Token-File Share (former Strategy C), which relied on push-based timers with clock-drift windows and was a load-bearing operational dependency (removed in #204).

**Ownership note**: Strategy B's host-side socket-activated minter is **owned by mcp-launcher, not shiori** (mcp-launcher#42). Shiori only implements the consumer side (`TokenSocketProvider`) and documents the contract it depends on; it carries no systemd unit for the minter itself. See "Architecture" below.

---

## Target Solution: On-Demand Socket Activation

We can satisfy both parameters. By replacing push-based timers with a host-side socket activation service, **containers can pull tokens on-demand** when needed, keeping the private key protected inside the host keyring.

### Architecture

#### 1. Host-side socket-activated minter (owned by mcp-launcher, not shiori)

The host-side systemd socket unit and per-connection minting service used to
live in this repo as `scripts/shiori-mint.socket` / `scripts/shiori-mint@.service`
/ `scripts/mint-token.sh`. They have been **retracted from shiori and moved to
mcp-launcher** (mcp-launcher#42): the socket is a generic pull-token primitive
shared by every consumer on the host (shiori, sunaba, ...), not something
specific to this project. Install it with mcp-launcher's own
`scripts/install-mint-socket.sh`; shiori's `scripts/install-systemd.sh` only
installs `shiori.service` and no longer touches any mint-socket unit.

Verified on WSL (mcp-launcher#42, 2026-07): the socket lives at
`%t/mcp-token/mint.sock` (i.e. `/run/user/<uid>/mcp-token/mint.sock`; systemd
creates both the directory, mode 0700, and the socket, mode 0600). A
connecting client -- including from inside a container, when the *directory*
is bind-mounted (verified through Docker Desktop) -- receives a `ghs_` token
(40 chars, one line, no other output on the wire) in about 0.4s.

**Socket contract**: connection = request. There is no request payload -- the
client just connects. The server writes the token and closes; the client
must **read until EOF and strip** the result. This is exactly what
`TokenSocketProvider._refresh()` (`src/shiori/github_auth.py`) does.

**Boundary**: do not bind-mount this socket, or its parent directory, into
sandbox / dev-container tooling (e.g. sunaba). It is scoped to this compose
deployment's own token consumption; sandbox containers have their own
credential path (host-side resolution, no token enters the container) and
must not reach into this socket.

#### 2. Container integration (this repo)

The container runs `TokenSocketProvider` (`src/shiori/github_auth.py`),
pointed at the socket via `GITHUB_TOKEN_SOCKET=/run/shiori/mint.sock`. It
opens a connect-recv(until EOF)-close loop, caches the token for 55 minutes,
and re-fetches 5 minutes before expiry -- the same cache/fallback shape as
`TokenCommandProvider`.

The provider precedence is **App > TokenSocket > TokenCommand > PAT >
Anonymous**.

### Invariant: Wall-Clock Expiry, Never Monotonic

`TokenSocketProvider` (like `TokenCommandProvider`) judges cache expiry using
the **wall clock** (`time.time()`), never `time.monotonic()`. A monotonic
clock does not advance while the host is suspended, so a monotonic-based
cache would silently believe a token is still fresh across a sleep/resume
cycle -- exactly reintroducing the clock-drift bug (Rule 2, above) that this
whole design exists to eliminate. This is a property of the *provider's own
bookkeeping*, independent of the pull-vs-push distinction: pulling on demand
solves the "was the shared value produced recently enough" problem, but only
if "recently enough" is itself measured on the same clock the token's actual
lifetime is denominated in (wall-clock GitHub expiry), which is why both
`CACHE_SECONDS`/`REFRESH_BEFORE`/`HARD_EXPIRY` fields are plain `time.time()`
floats.

### Security & Operational Guarantees

*   **On-Demand Pull**: Bypasses clock-drift and suspension issues.
*   **Zero Container Keys**: Long-lived private keys never leave the host keyring.
*   **Zero Wall-Clock Timers**: Eliminates `refresh-token.sh` and cron dependencies.
*   **Reduced Blast Radius**: Exposure is limited to a short-lived 1-hour token scoped to a single installation, rather than a permanent private key.

### Implementation Considerations (mount footguns, verified 2026-07)

*   **Mount the parent directory, never the socket file.** A file-level bind
    mount (e.g. `./runtime/mint.sock:/run/shiori/mint.sock:rw`) pins the
    inode. If the host-side unit ever recreates the socket (service restart,
    host reboot), the container keeps pointing at the old, now-dead inode and
    gets `ECONNREFUSED` **permanently** -- it does not recover until the
    container itself is recreated. Mount the directory instead
    (`${SHIORI_MINT_SOCKET_DIR:-${XDG_RUNTIME_DIR}/mcp-token}:/run/shiori:rw`)
    so the container always resolves the *current* socket by path lookup.
*   **A file mount over a not-yet-created path corrupts the host.** If docker
    is asked to bind-mount a file path that does not exist yet, it silently
    creates a **root-owned directory** at that path instead of the plain file
    the config implied. Once that has happened, systemd can no longer
    `bind()` its own socket there (permission denied) and the minter unit
    fails to start until an operator manually removes the root-owned
    directory. This is the second, independent reason to mount a directory
    that systemd itself creates, rather than a file path shiori guesses at
    ahead of time.
*   **Socket Write Access**: `connect(2)` on a Unix socket requires write
    permission on the socket inode, so the mount must be `rw`.
*   **Concurrent Minting**: `Accept=yes` (on the host-side unit) forks a
    connection handler per connection, so concurrent callers do not block
    each other.

---

## Migration Plan

| Phase | Task | Status |
| --- | --- | --- |
| **Phase 0** | Align GCP VMs to use `use_github_app=true` (retains Strategy A). | Complete |
| **Phase 1** | Remove legacy `McpTokenProvider` and clean scripts, consolidating configurations into Strategy A and B. | Current PR |
| **Phase 2** | Implement `TokenSocketProvider` (consumer side, this repo) and the host-side socket-activated minter, validating under WSL. Retract Strategy C -- Token-File Share (`refresh-token.sh`, `shiori-refresh.{service,timer}`). | **Complete** -- consumer side: Shiori PR #242; host side: mcp-launcher#42 |
| **Phase 3** | Deploy to GCP VMs and retract Strategy A (removing PEM files from VM disks). | Post Phase 2 |

---

## Downstream Application

*   **Sunaba (GCP VM)**: Currently runs `mcp-token` via sudoers (`GITHUB_TOKEN_COMMAND="sudo -n -u mcpsecrets ... mcp-token"`). Transitioning to socket activation removes sudoer dependencies.
*   **dev-infra**: Updates deployment configurations to configure systemd socket templates rather than writing PEM keys to `.env` files.
