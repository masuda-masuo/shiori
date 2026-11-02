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
| **B. Mint Socket** | Host Keyring | Consumer (Pull) | ◯ No drift windows | **WSL + GCP VM (current)** |
| **E. Native Host Run** | Host Keyring | Consumer (Pull) | ◯ No drift windows | Host venv / Sunaba |
| **A. App Private Key** (Retired) | Container (PEM) | Consumer (Pull) | ◯ No drift windows | **Removed** (Retired by #243; superseded by Mint Socket) |
| **C. Token-File Share** (Retired) | Host Keyring | Host Timer (**Push**) | **✗ Drift windows** | **Removed** (Replaced by Mint Socket in #204) |
| **D. In-Container Mint** (Retired) | Keystore (Unreachable) | — | — | **Removed** (Legacy `McpTokenProvider`) |

**Strategy B (Mint Socket) is the token supply for every deployment.** The private key stays in the host keyring; containers pull a short-lived token on demand via socket activation, so there is neither a clock-drift window nor any long-lived secret on disk.

Strategy A (App Private Key in the container) was **retired (#243, under EPIC #237)**. It was pull-based and had no drift window, but it **carried the long-lived App private key (PEM) into the container and onto disk** — if leaked, the App is compromised indefinitely. Once the mint socket (B) proved out on both WSL and the GCP VM (2026-07-15), A had no remaining advantage and one strict disadvantage, so its provider (`AppTokenProvider`) and all `GITHUB_APP_*` container config were removed. The App private key now exists only in the host keyring, consumed by `mcp-token`. Strategy C (Token-File Share) was likewise removed in #204 (push-based timers with drift windows).

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

The provider precedence is **TokenSocket > TokenCommand > PAT >
Anonymous**. (The in-container App provider was retired — #243.)

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

---

## Mount Footguns & Lifecycle Mechanics (shiori #461)

Operating the mint socket via container bind mounts across systemd session
lifecycles and host reboots involves critical filesystem and container
runtime footguns:

### 1. Socket Inode Replacement vs. Directory Inode Replacement

*   **Socket inode replacement succeeds with directory mounts:**
    When the host-side `mcp-token` unit is restarted, systemd unlinks the old
    Unix socket file and binds a new one with a new `st_ino`. Because Shiori
    mounts the parent directory (`${SHIORI_MINT_SOCKET_DIR:-${XDG_RUNTIME_DIR}/mcp-token}:/run/shiori:rw`),
    path lookups for `/run/shiori/mint.sock` inside the container resolve the
    current socket inode dynamically.
*   **Source directory / runtime tmpfs replacement breaks bind mounts:**
    In Linux and WSL2, `${XDG_RUNTIME_DIR}` (`/run/user/<uid>`) is backed by a
    `tmpfs` instance. Depending on session lifecycle and distribution configuration,
    when the user session or WSL restarts, systemd may unmount and recreate
    `/run/user/<uid>` on a fresh `tmpfs` with a new device (`st_dev`) and
    directory inode (`st_ino`).
    In that case, existing containers retain the bind mount to the **old unlinked
    tmpfs directory**. Any new socket created on the new host tmpfs is completely
    invisible inside the container, causing permanent `ENOENT` or
    `ECONNREFUSED` on connect attempts until the container is recreated.

### 2. Parent Directory Mounts and Propagation Limits

*   **Mounting a parent directory only observes child changes while the parent remains valid:**
    Mounting `/run/user/<uid>` into the container does not survive destruction
    and recreation of the `/run/user/<uid>` mount point itself.
*   **Nested mount propagation is unsupported on Docker Desktop:**
    Docker Desktop bind mount propagation (`rshared`) across the Windows / WSL2
    VM boundary is officially unsupported (see [Docker bind propagation documentation](https://docs.docker.com/engine/storage/bind-mounts/#configure-bind-propagation)).
    Measurements on real hosts running Docker Desktop 29.5.2 verified that
    `/run/user/1000` is mounted as `private,slave`. That bind propagation is
    unsupported across VM boundaries according to official Docker documentation
    is evidence; actual replacement and propagation behavior remains UNVERIFIED on host.

### 3. systemd Unit Relationships Are Not Directory Watchers

*   systemd unit directives (`After=`, `BindsTo=`, `PartOf=`) govern **unit
    state transitions** (active, inactive, failed), NOT filesystem modifications
    (see [systemd.unit documentation](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html)).
*   The systemd user manager itself does not necessarily restart when the
    runtime directory is recreated, so unit-level restart triggers do not fire
    on tmpfs recreation.

### 4. Third-Candidate Architectural Boundary (mcp-launcher)

*   Relocating the mint socket outside `/run/user/<uid>` (e.g. to a persistent
    directory like `~/.local/state/shiori`; `/var/run` is normally a symlink to
    `/run` and tmpfs-backed, not a persistent fallback) belongs strictly to
    **mcp-launcher**, which owns the host socket unit and minter service.
*   Shiori must not attempt to relocate or manage the minter's socket path.
    A launcher-side relocation is strictly a fallback candidate if Shiori's
    reconciliation mechanism fails, which would require an external issue
    and investigation.

### 5. Root-Owned Missing-Source Poisoning Footgun

*   **Docker bind mount creation as root:**
    When Docker evaluates a bind mount whose host source path does not exist,
    the Docker daemon (running with root privileges) creates the missing host
    directory owned by `root:root` (UID 0) with mode `0755`.
*   **Refusal without automatic destructive mutation:**
    If this occurs, the host user's non-root `mcp-token` unit cannot bind its
    socket inside the root-owned directory, and the reconciler strictly refuses
    to proceed (`source_stat.st_uid != os.geteuid()`), logging a warning and waiting.
    The reconciler intentionally never runs `chmod` or recursive deletions
    (`rm -rf`) against runtime directories automatically.
*   **Operator Remediation:**
    An operator must safely inspect the host path (`ls -ld <source>`), confirm
    that it is an empty accidental directory created by Docker, remove only that
    specific known empty directory (`rmdir <source>`), and restart the socket unit
    (`systemctl --user restart mcp-token.socket`). Never perform recursive
    `rm -rf` on `${XDG_RUNTIME_DIR}` or runtime directories.

---

## Mint Socket Mount Reconciliation (shiori #461)

To automatically recover from WSL runtime tmpfs recreation without manual
intervention or blind periodic restarts, Shiori implements a host-side
reconciler:

### Architecture & Design

1.  **Periodic Reconciler (`scripts/reconcile_mint_mount.py`):**
    A small host stdlib Python script invoked periodically (~120s) by systemd
    user timer `shiori-mint-mount.timer` (`shiori-mint-mount.service`, `Type=oneshot`).
    - Serialized by `Type=oneshot` and a non-blocking `flock` on `${XDG_STATE_HOME:-$HOME/.local/state}/shiori/mint-mount.json.lock`.
    - No sleep loops or daemon processes.
    - **Metadata-only checking**: The timer periodically checks filesystem mount
      generations and container state. It does **not** mint tokens, connect to the
      socket, or refresh credentials, and is strictly distinct from any token refresh timer.
2.  **State Tracking (`mint-mount.json`):**
    Stored at `${XDG_STATE_HOME:-$HOME/.local/state}/shiori/mint-mount.json`
    (mode `0600`, parent directory `0700`, atomically replaced):
    - `success`: `{ source_path, dev, ino, container_id, timestamp }`
    - `last_attempt`: `{ source_path, dev, ino, container_id, timestamp }`
3.  **One-Pass Decision Logic:**
    - Checks `systemctl --user is-active --quiet shiori.service`: if inactive, no-op.
    - Inspects `docker compose ps -q app`: must return exactly one running container.
    - Inspects container: confirms `State.Running` and validates bind mount `/run/shiori`.
    - Resolves `docker compose config --format json`: verifies `GITHUB_TOKEN_SOCKET`
      starts with `/run/shiori/` without path traversal (`..`).
    - Validates host mount source: must exist, be a directory, and be owned by
      the effective user UID (`os.geteuid()`).
    - Validates host socket: must exist, be a Unix socket (`S_ISSOCK`), owned
      by effective UID, and contained inside the mount source.
    - **Generation comparison (Explicit 3 Cases):**
      - **Case 1 (Same generation + same container ID)**: **NOOP** (even if the socket inode alone changed).
      - **Case 2 (Same generation + new container ID)**: **ADOPT** current container ID. Atomically updates `success.container_id` to the running container ID and exits without invoking `docker compose up`, `restart`, or any other container mutation. This avoids unnecessary second downtime when external workflows (e.g., `scripts/update.sh` running `docker compose up -d app`) have already recreated the container with the current runtime directory mounted. In `--dry-run`, reports would-adopt without mutating state. Clears any stale `last_attempt` record.
      - **Case 3 (Changed generation OR missing/corrupt baseline)**: **RECREATE** app container alone via `docker compose up -d --force-recreate --no-deps app` with pre-mutation re-verification, generation-keyed retry cooldown (~120s), and postcheck verification before atomically committing new `success` state.
4.  **Failure Handling & Cooldown:**
    - If recreation fails, prior `success` state is preserved.
    - `last_attempt` generation (`source_path`, `dev`, `ino`) and timestamp are persisted.
    - **Cooldown keyed on filesystem generation**: Cooldown compares filesystem
      generation `(path, dev, ino)` independent of container ID. Even if the container
      ID changed during a failed recreate or timeout, retry for that generation is
      throttled to at most once per 120 seconds.
    - Clock rollbacks do not suppress retries indefinitely.
    - Eventual retry is allowed once cooldown expires; retries are not permanently suppressed.
5.  **Postcheck Verification:**
    - Re-inspects the fresh running app container.
    - Confirms the host source generation and socket readiness remained unchanged mid-command.
    - Only then commits the new `success` state atomically.
6.  **Docker Desktop VM Path Handling & Host Acceptance Blocker:**
    - Arbitrary prefix trust has been removed: exact path matching and host-side
      `samefile` verification are supported.
    - Unverified opaque VM mappings (e.g. `/run/desktop/...` paths that cannot be
      resolved on the host) fail closed with an explicit log message. Suffix
      equivalence is not invented or guessed.
    - If actual Docker Desktop mount sources are opaque and cannot be verified on
      host, this is a documented host acceptance blocker pending human verification.
      Shiori does not claim automatic recovery for unverified opaque VM paths and
      does not modify the launcher.
7.  **Secrets & Operation Boundaries:**
    - **Never mints, connects, or receives on the socket** (`connect(2)` itself mints!).
    - Shell=False and bounded subprocess timeouts for all commands.
    - No captured environment, compose config dumps, or stderr dumps that could leak tokens or secrets.
    - Subprocess errors, timeouts, and state decode failures log only terse error classes without secret payloads.

---

## WSL Human Acceptance Procedure & Safety Boundaries (Status: PENDING)

> [!IMPORTANT]
> Automated tests in `tests/test_reconcile_mint_mount.py` verify CLI contract
> boundaries, state persistence, cooldown suppression, and error handling
> using fake command wrappers. **They do NOT prove real live `connect()` after
> actual host runtime directory recreation.**
> Real WSL host acceptance must be performed and confirmed by a human operator
> before merging; status is currently PENDING.

### Acceptance Verification Plan (Human Operator)

Execute all runtime reproduction steps manually on the host. Do NOT run
reproduction scripts inside automated sandboxes.

#### Path Selection: Automatic Recovery vs. Optional Restart Comparison

Before triggering the first runtime-generation replacement, decide which path to execute:
*   **Standard Path (Automatic Recovery Acceptance)**: Follow **Step 1** through **Step 4** in linear sequence. The reconciler timer remains enabled and automatically force-recreates the app container upon detecting the new runtime generation.
*   **Optional Path (Independent Restart Comparison)**: Follow the **Optional Independent Comparison Procedure** below **BEFORE** terminating the distro.

> [!WARNING]
> **Order Dependency**: The optional comparison procedure MUST be chosen and prepared **BEFORE** the first runtime-generation replacement.
> In the standard path, Step 3 waits for automatic recreation to fire. Stopping the timer after Step 3 has already recreated the container is too late.

---

### Standard Path: Automatic Recovery Verification (Default)

#### Step 1: Pre-Restart Baseline Capture

In the running WSL environment with Shiori and mcp-launcher active:

```bash
# 1. Capture host runtime mount & directory stat
findmnt /run/user/1000
stat /run/user/1000/mcp-token

# 2. Capture app container ID and mounts
docker compose ps app
docker inspect $(docker compose ps -q app) --format 'ID: {{.Id}} StartedAt: {{.State.StartedAt}} Mounts: {{json .Mounts}}'

# 3. Capture initial reconciler state
systemctl --user status shiori-mint-mount.timer
cat ${XDG_STATE_HOME:-$HOME/.local/state}/shiori/mint-mount.json
```

#### Step 2: Controlled WSL Restart

*   Keep Docker Desktop running in Windows.
*   Do **NOT** casually `rm -rf /run/user/1000` or kill the user manager.
*   From a separate Windows PowerShell terminal, terminate the WSL distro:
    ```powershell
    wsl --terminate <DistroName>
    ```
    *(Avoid `wsl --shutdown` if Docker Desktop VM shares the WSL backend and would be killed prematurely).*

#### Step 3: Post-Restart Verification

Open a new WSL shell and verify:

```bash
# 1. Verify app container survived in Docker Desktop
docker compose ps app

# 2. Verify mcp-token socket is created on the new runtime tmpfs
systemctl --user status mcp-token.socket
stat /run/user/1000/mcp-token
# Verify st_dev or st_ino changed compared to Step 1

# 3. Observe reconciler timer and service execution
systemctl --user list-timers 'shiori-*'
journalctl --user -u shiori-mint-mount -f
# Wait <= 5 minutes for the timer tick to fire and recreate the app container
```

#### Step 4: Verification of Live Socket Connect

After the reconciler logs successful recreation:

```bash
# 1. Verify container ID was updated
docker compose ps app
cat ${XDG_STATE_HOME:-$HOME/.local/state}/shiori/mint-mount.json

# 2. Verify connect(2) from inside the container without minting or printing tokens:
docker compose exec app python3 -c "
import socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(2.0)
s.connect('/run/shiori/mint.sock')
s.close()
print('CONNECT_OK')
"
```

> [!WARNING]
> Under no circumstances execute `recv()` or print the output of the socket!
> Connecting and closing verifies reachability (`connect(2)` itself triggers minting on socket activation);
> reading consumes and prints tokens, which leaks credentials into logs.

---

### Optional Independent Comparison Procedure: Manual `docker compose restart app` vs Re-creation (HUMAN OPERATOR ONLY)

An open question is whether a simple in-place `docker compose restart app` is sufficient to heal stale bind mounts on Docker Desktop / WSL without a full `--force-recreate`, preserving the container ID and layer state while updating `StartedAt`.

To evaluate this without racing the automatic reconciler or invalidating acceptance, execute this comparison **BEFORE the first runtime-generation replacement**:

#### Step C1: Disable Timer & Boundedly Wait Before Distro Termination

1.  **Disable Timer**:
    Run `systemctl --user disable --now shiori-mint-mount.timer` BEFORE distro termination so that it remains disabled after user-manager restart.
    ```bash
    systemctl --user disable --now shiori-mint-mount.timer
    ```
2.  **Wait Boundedly for In-Flight Service (Do NOT Kill It)**:
    Do **NOT** stop `shiori-mint-mount.service` with `systemctl stop`: that kills an in-flight Docker command.
    Instead, wait boundedly using host human commands until the service is inactive or failed:
    ```bash
    # Check boundedly (e.g. repeat every few seconds for up to 30-60s):
    systemctl --user is-active shiori-mint-mount.service
    ```
    *   **Abort on Stalled Service**: If the service stays `activating` or `active` after the bounded wait, **abort the comparison** and capture diagnostic status rather than kill it:
        ```bash
        systemctl --user status shiori-mint-mount.service
        journalctl --user -u shiori-mint-mount -n 50
        ```
    *   **Timer State Restoration on Abort**: Restore prior timer settings:
        - For the normal expected enabled state:
          ```bash
          systemctl --user enable --now shiori-mint-mount.timer
          ```
        - If the timer was originally disabled, preserve the disabled state (do not enable).
        Do not proceed with the comparison if aborted.
3.  **Record Pre-Restart Baseline (Old ID + Generation)**:
    ```bash
    docker compose ps app
    docker inspect $(docker compose ps -q app) --format 'ID: {{.Id}} StartedAt: {{.State.StartedAt}}'
    stat /run/user/1000/mcp-token
    ```

#### Step C2: Controlled Generation Recreation

Perform human-only controlled generation recreation with the same WSL caveats:
*   Keep Docker Desktop running in Windows.
*   Do **NOT** casually `rm -rf /run/user/1000` or kill the user manager.
*   From Windows PowerShell, terminate the WSL distro:
    ```powershell
    wsl --terminate <DistroName>
    ```

#### Step C3: Stale Precondition & Connect Baseline Verification

Open a new WSL shell:
1.  **Verify New Generation & Survived Container**:
    ```bash
    # 1. Verify generation differs from Step C1 (st_dev or st_ino changed):
    stat /run/user/1000/mcp-token

    # 2. Verify timer remains disabled (reconciler did not run):
    systemctl --user is-active shiori-mint-mount.timer

    # 3. Verify same app ID survived and reconciler did not mutate it:
    docker inspect $(docker compose ps -q app) --format 'ID: {{.Id}} StartedAt: {{.State.StartedAt}}'
    ```
    The container ID and `StartedAt` must exactly match the baseline recorded in Step C1.
2.  **Connect Baseline in Stale State (MUST FAIL)**:
    Run the connect-only test from inside the container:
    ```bash
    docker compose exec app python3 -c "
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect('/run/shiori/mint.sock')
    s.close()
    print('CONNECT_OK')
    "
    ```
    *   `connect(2)` closes immediately, no `recv()`, triggers mint on socket activation, no tokens logged.
    *   **The connect baseline MUST fail** in this stale state (e.g. `ConnectionRefusedError`, `FileNotFoundError`, or timeout). This failure proves that the stale mount actually broke connectivity, making the subsequent restart test meaningful.
    *   **Inconclusive State**: If the connect baseline unexpectedly **succeeds**, the test state is inconclusive (the mount was not stale or the environment behaved differently); **do NOT propose a mechanism switch on that evidence**.

#### Step C4: Manual `docker compose restart app` & Evaluation

1.  **Perform Manual Restart**:
    Only AFTER verifying the connect baseline failure, execute:
    ```bash
    docker compose restart app
    ```
2.  **Record Container State**:
    ```bash
    docker inspect $(docker compose ps -q app) --format 'ID: {{.Id}} StartedAt: {{.State.StartedAt}}'
    ```
    Verify the container ID remains identical while `StartedAt` has changed.
3.  **Execute Connect-Only Test**:
    ```bash
    docker compose exec app python3 -c "
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect('/run/shiori/mint.sock')
    s.close()
    print('CONNECT_OK')
    "
    ```
4.  **Reporting & PR Procedure**:
    *   If restart succeeds (prints `CONNECT_OK`): The human operator reports the result to the orchestrator.
    *   The orchestrator will propose switching from `--force-recreate` to `restart` on PR #465 and await the user's explicit decision.
    *   The actual codebase implementation in `scripts/reconcile_mint_mount.py` **remains unchanged** (`--force-recreate`) until user approval is received.

#### Step C5: Transition to Independent Automatic Recovery Acceptance

To test automatic recovery independently without "compare-result laundering":
1.  **Recreate Generation AGAIN**:
    With the timer still disabled, perform another controlled generation recreation (e.g. `wsl --terminate <DistroName>`).
2.  **Prove Stale Precondition & No Unrelated Restart**:
    In the new WSL shell, verify:
    - `stat /run/user/1000/mcp-token` shows a newly changed generation.
    - Container ID and `StartedAt` survived without mutation.
    - Connect baseline fails (confirming stale mount).
3.  **Enable and Start Reconciler Timer**:
    ```bash
    systemctl --user enable --now shiori-mint-mount.timer
    ```
4.  **Observe Automatic Recovery**:
    Monitor journalctl without performing any manual `docker compose restart` or `docker compose up`:
    ```bash
    journalctl --user -u shiori-mint-mount -f
    ```
    Verify automatic reconciliation recreates the app container and completes live socket connect (Step 4).

#### Step 5: Untested Branches & Safety Rollback

*   **User manager stays live while runtime directory is replaced:**
    This specific OS edge case remains **UNVERIFIED** unless separately
    reproduced on host. Do not claim it as tested.
*   **Emergency Rollback / Manual Recovery:**
    If the reconciler encounters issues:
    ```bash
    systemctl --user disable --now shiori-mint-mount.timer
    docker compose up -d --force-recreate --no-deps app
    ```
    No database operations or data mutations occur.

---

## Migration Plan

| Phase | Task | Status |
| --- | --- | --- |
| **Phase 0** | Align GCP VMs to use `use_github_app=true` (retains Strategy A). | Complete |
| **Phase 1** | Remove legacy `McpTokenProvider` and clean scripts, consolidating configurations into Strategy A and B. | Current PR |
| **Phase 2** | Implement `TokenSocketProvider` (consumer side, this repo) and the host-side socket-activated minter. Retract Strategy C -- Token-File Share (`refresh-token.sh`, `shiori-refresh.{service,timer}`). Note: WSL runtime tmpfs reconciliation (#461) acceptance remains pending. | **Complete** -- consumer side: Shiori PR #242; host side: mcp-launcher#42 (WSL #461 acceptance pending) |
| **Phase 3** | Deploy to GCP VMs and retract Strategy A (removing PEM files from VM disks). | Post Phase 2 |

---

## Downstream Application

*   **Sunaba (GCP VM)**: Currently runs `mcp-token` via sudoers (`GITHUB_TOKEN_COMMAND="sudo -n -u mcpsecrets ... mcp-token"`). Transitioning to socket activation removes sudoer dependencies.
*   **dev-infra**: Updates deployment configurations to configure systemd socket templates rather than writing PEM keys to `.env` files.


---

## Impact on Shiori Sync (#236)

The on-demand token supply (#204, #243) was a prerequisite for the pull-type sync model (#236). Previously, Shiori had a background auto-sync loop that ran every N seconds. When the host suspended (WSL) or shut down (GCP VM), the auto-sync loop would die silently and require self-repair on resume — which was unreliable (#234).

In the pull-type model (#236):
- Tokens are pulled on-demand from the mint socket whenever a search/clone-refresh operation runs.
- There is no background loop; every sync happens inline (Phase 1: clone refresh) or in the background (Phase 2: re-indexing) when a tool call triggers it.
- The "token expired after resume" window that previously required the auto-sync loop's silent retry is no longer an issue: the first tool call after resume pulls a fresh token from the mint socket inline, then refreshes the clone.

This is why #204 was marked as a hard dependency for #236: without on-demand token minting, the first search after resume would fail with a 401 instead of pulling a fresh token.
