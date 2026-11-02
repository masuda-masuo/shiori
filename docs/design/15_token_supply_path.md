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
    A small host stdlib Python script invoked by the systemd user timer
    `shiori-mint-mount.timer` (`shiori-mint-mount.service`, `Type=oneshot`).
    - `OnActiveSec=10s` schedules the first check 10 seconds after the timer is
      activated; `AccuracySec=10s` permits that trigger to be coalesced within
      its accuracy window. `OnUnitActiveSec=2m` schedules later checks two
      minutes after the service last ran. This is metadata-only reconciliation,
      not a mint or credential refresh timer.
    - The timer has no `OnCalendar` schedule, so it does not use
      `Persistent=true` calendar catch-up.
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
> Final installed-unit validation must be performed by a human operator after
> this timerfix PR is merged and deployed, with the temporary drop-in removed.
> Status is currently PENDING; production deployment is not required before merge.

### Observed WSL Timer Reactivation Failure and Final Acceptance

The WSL tests on systemd 255.4 and Docker Desktop 29.5.2 showed this sequence:
C3: after `wsl --terminate Ubuntu`, the runtime generation changed from dev 136
to 63, the original timer did not trigger, and the stale container's socket
connect failed (111). C4: a manual app restart restored the connection. C5:
another generation change (dev 116) again produced connection 111 while the
original timer did not fire. With the temporary `OnActiveSec=10s` drop-in, the
03:22 timer run automatically force-recreated the app; the socket check printed
`CONNECT_OK`, and the timer showed its next trigger.

This is an observation about that WSL reactivation path, not a claim that
systemd always ignores an overdue `OnBootSec`: the
[systemd.timer documentation](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)
says an overdue boot-relative trigger may run immediately. The activation-relative
`OnActiveSec=10s` trigger gives the reactivated user timer a fresh initial
check. The selected recovery remains
`docker compose up -d --force-recreate --no-deps app`; the C5 run demonstrated
automatic recovery with that command, and no switch to in-place restart was
chosen.

The local temporary override used during diagnosis is
`${HOME}/.config/systemd/user/shiori-mint-mount.timer.d/activation.conf`.
Remove only that file for final validation. Do not remove runtime directories.
The separate case where a live user manager sees only its runtime directory
replaced remains **UNVERIFIED**; this timer change does not claim to address it.

#### Final operator acceptance checklist (pending)

After this timerfix PR is merged and deployed, the human operator will:

1. Remove only the temporary activation drop-in, reload the user manager, and
   restart the timer so the installed unit is tested without an override:

   ```bash
   rm -- ~/.config/systemd/user/shiori-mint-mount.timer.d/activation.conf
   systemctl --user daemon-reload
   systemctl --user restart shiori-mint-mount.timer
   ```

2. Confirm the timer has a next run:

   ```bash
   systemctl --user list-timers 'shiori-*' --no-pager
   ```

   Confirm `shiori-mint-mount.timer` shows a `NEXT` time.

3. Record the current runtime generation and app container, keep Docker
   Desktop running, then run `wsl --terminate Ubuntu` from Windows:

   ```bash
   stat /run/user/1000/mcp-token
   docker compose ps app
   ```

   Reopen the distro shell and wait three minutes. Do not manually repair the
   mount, restart the app, or remove/replace runtime directories during this
   automatic observation.

4. Verify the runtime generation changed, inspect the reconciler journal, and
   run a connect-only check from the app container:

   ```bash
   stat /run/user/1000/mcp-token
   journalctl --user -u shiori-mint-mount --since "5 minutes ago"
   docker compose exec app python3 -c "
   import socket
   s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
   s.settimeout(2.0)
   s.connect('/run/shiori/mint.sock')
   s.close()
   print('CONNECT_OK')
   "
   ```

   Compare the source generation with its recorded pre-termination values and
   confirm the reconciler ran and the container was recreated. Do not call
   `recv()` or print socket data: connecting triggers minting, while reading
   would expose the token.

Real-host acceptance remains **PENDING / UNVERIFIED** until this procedure is
completed against the installed unit without the temporary override. Issue
#461 remains open until the operator confirms the result.

#### Emergency rollback and manual recovery

If automatic reconciliation must be paused, disable and stop the timer, then
allow any in-flight reconciliation service to finish. Do not kill or stop the
service while it is reconciling. Wait up to its bounded service timeout. If it
is still active at that bound, abort manual recovery, capture status and journal
diagnostics, and let systemd's configured timeout handling complete first.

```bash
systemctl --user disable --now shiori-mint-mount.timer
systemctl --user status shiori-mint-mount.service
journalctl --user -u shiori-mint-mount.service --since "10 minutes ago"
```

After reconciliation has finished, manual recovery may recreate only the app
container:

```bash
docker compose up -d --force-recreate --no-deps app
```

Manual recovery invalidates automatic acceptance. Repeat the independent WSL
reproduction and final installed-unit validation later. Never remove, recreate,
or recursively change permissions on runtime directories. The case where a
live user manager remains running while only its runtime directory is replaced
is **UNVERIFIED** and outside this timer's claimed scope.

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
