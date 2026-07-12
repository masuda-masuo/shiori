# Detailed Design: Token Supply Path

## 1. Context

While [GitHub App Auth](09_github_app_auth.md) defines *how* short-lived tokens are minted, this document defines **the paths used to deliver minted tokens to the consuming services**.

The choice of credential pathway is determined directly by the active **runtime environment**. The rules outlined here apply to both the Shiori server and external tools like Sunaba.

---

## 2. In-Container Keyring Limitations

Because the container runs in an isolated user namespace, it cannot access the host's keyring bus (D-Bus Secret Service). The Secret Service rejects requests from UIDs outside the bus owner's namespace.

To allow the container to access GitHub APIs, we must either:
1.  Mount the private key files directly into the container.
2.  Pre-mint tokens on the host and share them with the container.

---

## 3. The Fallacy of Push-Based Token Files

Using a background host timer to pre-mint tokens and write them to a shared file (a "push" model) introduces several failure vectors:
*   **Startup Windows**: If the container starts when the shared token file is expired, operations fail until the next timer tick.
*   **Silent Failures**: If the host timer service fails, sync operations fail after 60 minutes.
*   **Sleep States**: Because systemd timers use monotonic clocks, they pause when the host machine suspends. However, token lifespans decrease in wall-clock time. Upon waking, the shared token file is expired, but the host timer assumes it still has time before the next trigger, causing temporary auth failures.

Therefore, tokens must be retrieved on-demand using a **pull model** triggered when the container starts an operation.

---

## 4. Ingestion Authentication Options

| Strategy | Private Key Location | Refresh Control | Lifespan |
| --- | --- | --- | --- |
| **A. App Private Key** | Container (secrets) | Container (On-demand Pull) | Token: 1 hr / Key: Persistent |
| **B. Token-File Share** | Host Keyring | Host Timer (Periodic Push) | Token: 1 hr / Key: Protected |
| **C. Host-Side Broker** | Host Keyring | Container (On-demand Pull) | Token: 1 hr / Key: Protected |

---

## 5. Ultimate Strategy: On-Demand Socket Activation

To avoid storing long-lived private keys inside the container while bypassing the flaws of push-based timers, we implement **systemd socket activation** on the host.

### Mechanics

1.  **Systemd Socket (`shiori-mint.socket`)**:
    Binds to a Unix socket file on the host (e.g. `runtime/mint.sock`).
2.  **Systemd Service (`shiori-mint@.service`)**:
    Configured to trigger on incoming socket connections. When a connection is opened, systemd executes `mcp-token github`, passing the stdout back to the socket.
3.  **Container Mount**:
    The container mounts the Unix socket file and connects using standard library socket modules (`TokenSocketProvider`). On connect, it receives a freshly minted 1-hour token.

### Benefits
*   **On-Demand Pull**: No authentication failures caused by sleep states.
*   **No Container Private Keys**: Long-lived private keys never leave the host's keyring.
*   **No Wall-Clock Timers**: The background timer service is removed.
*   **Race Conditions Solved**: Adding `Requires=mcp-keyring.service` to the systemd socket configuration ensures keyrings are fully initialized before token requests are handled.
