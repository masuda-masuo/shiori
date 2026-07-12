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
| **B. Token-File Share** | Host Keyring | Host Timer (**Push**) | **✗ Drift windows** | WSL |
| **C. In-Container Mint** | Keystore (Unreachable) | — | — | **Removed** (Legacy `McpTokenProvider`) |
| **D. Native Host Run** | Host Keyring | Consumer (Pull) | ◯ No drift windows | Host venv / Sunaba |

Strategies A and B both sacrifice a security parameter:
*   **Strategy A** is pull-based (no clock-drift windows), but **exposes the long-lived App private key (PEM) inside the container and on disk**. If leaked, the App is compromised indefinitely.
*   **Strategy B** protects the private key on the host. However, it relies on **push-based timers (clock-drift windows)** and is a load-bearing operational dependency.

---

## Target Solution: On-Demand Socket Activation

We can satisfy both parameters. By replacing push-based timers with a host-side socket activation service, **containers can pull tokens on-demand** when needed, keeping the private key protected inside the host keyring.

### Architecture

#### 1. Host systemd Socket File (`shiori-mint.socket`)
```ini
[Socket]
ListenStream=@SHIORI_DIR@/runtime/mint.sock
Accept=yes
SocketMode=0600

[Install]
WantedBy=sockets.target
```

#### 2. Host systemd Service (`shiori-mint@.service`)
```ini
[Unit]
Description=Mint a short-lived GitHub token (one connection)
Requires=mcp-keyring.service
After=mcp-keyring.service

[Service]
Type=oneshot
ExecStart=@SHIORI_DIR@/scripts/mint-token.sh
StandardInput=socket
StandardOutput=socket
```

On connection, systemd runs `mcp-token github` once and routes the stdout stream back to the socket.

#### 3. Container Integration
The container runs a socket provider (`TokenSocketProvider`) pointing to the Unix socket (`GITHUB_TOKEN_SOCKET=/run/shiori/mint.sock`). It establishes a connect-recv-close loop. Tokens are cached in-memory for 55 minutes, checking the socket for updates 5 minutes before expiration.

The provider precedence becomes **App > TokenSocket > TokenCommand > PAT > Anonymous**.

### Security & Operational Guarantees

*   **On-Demand Pull**: Bypasses clock-drift and suspension issues.
*   **Zero Container Keys**: Long-lived private keys never leave the host keyring.
*   **Zero Wall-Clock Timers**: Eliminates `refresh-token.sh` and cron dependencies.
*   **Startup Races Solved**: `Requires=mcp-keyring.service` forces systemd to verify that the keyring bus is fully initialized before resolving requests, resolving race conditions where mcp-token starts before the Secret Service is ready.
*   **Reduced Blast Radius**: Exposure is limited to a short-lived 1-hour token scoped to a single installation, rather than a permanent private key.

### Implementation Considerations

*   **Socket Write Access**: Unix socket connections (`connect(2)`) require **write permissions** on the socket inode. The shared volume mapping must be updated from read-only (`ro`) to read-write (`rw`).
*   **WSL Bind Mounts**: Verify socket bind mount stability under WSL Docker environments.
*   **Concurrent Minting**: `Accept=yes` forks connection processes, preventing concurrent calls from blocking.

---

## Migration Plan

| Phase | Task | Status |
| --- | --- | --- |
| **Phase 0** | Align GCP VMs to use `use_github_app=true` (retains Strategy A). | Complete |
| **Phase 1** | Remove legacy `McpTokenProvider` and clean scripts, consolidating configurations into Strategy A and B. | Current PR |
| **Phase 2** | Implement `TokenSocketProvider` and socket units, validating under WSL. Retract Strategy B. | Shiori PR #204 |
| **Phase 3** | Deploy to GCP VMs and retract Strategy A (removing PEM files from VM disks). | Post Phase 2 |

---

## Downstream Application

*   **Sunaba (GCP VM)**: Currently runs `mcp-token` via sudoers (`GITHUB_TOKEN_COMMAND="sudo -n -u mcpsecrets ... mcp-token"`). Transitioning to socket activation removes sudoer dependencies.
*   **dev-infra**: Updates deployment configurations to configure systemd socket templates rather than writing PEM keys to `.env` files.
