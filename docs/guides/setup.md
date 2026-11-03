# Setup Guide

## Prerequisites

- Docker and Docker Compose.
- (To index private repositories) A GitHub Personal Access Token (PAT) or credential configuration with repository read access.

---

## Repo Roles Overview

Shiori distinguishes two roles for repositories (see the [design doc](../design/01_data_ingestion_and_sync.md#10-repo-roles-dev-vs-reference-issue-303) for full rationale):

| Role | `SHIORI_DEV_REPOS` | Code indexing | PR reviews | Backfill | Sync style |
|---|---|---|---|---|---|
| **Reference** (read-only) | Not listed | Clone-only (`shiori_grep`) | Skipped | Bounded (`SHIORI_REF_BACKFILL_SINCE`) | One-shot, then frozen |
| **Development** (writable) | Listed | Tree-sitter chunks in DB | Fetched in parallel | Full history | On demand during dev |

Both roles share the same `SHIORI_REPOS` variable. The role is determined by whether the repo appears in `SHIORI_DEV_REPOS`.

---

## Onboarding Track A: Reference (Read-Only) Setup

Use this if you want to add external OSS repositories as a **knowledge corpus** — the agent searches them for patterns and conventions but never modifies them.

### 1. Environment

```bash
cp .env.example .env
```

Edit `.env` with minimal settings:

```env
SHIORI_REPOS=astral-sh/ruff,golang/go        # your reference repos
# No SHIORI_DEV_REPOS needed
# No GitHub App credentials needed

# (Optional) Limit backfill depth for large repos:
SHIORI_REF_BACKFILL_SINCE=2024-01-01
```

For public repos, a plain PAT with no scopes (or even no token at all) is sufficient — rate limits are higher with a token but a well-known public repo works anonymously.

### 2. Start services

```bash
docker compose up -d --build
```

### 3. One-shot bounded ingest

```bash
# Uses SHIORI_REF_BACKFILL_SINCE as the seed (if set)
docker compose run --rm ingest --repo owner/repo

# Or specify seed per run (overrides env):
./scripts/ingest.sh run --backfill-since 2023-06-01 --repo owner/repo
```

After this completes, the index contains issues and docs from the seed date onward. The repos are **frozen** — no periodic sync runs. To refresh manually:

```bash
docker compose run --rm ingest --repo owner/repo
```

### 4. Verify

```bash
# Connect an MCP client and call shiori_status — each repo should show
# role=ref and non-zero indexed counts.
```

### What works for reference repos

- `shiori_search` (issues, docs, grep-only code)
- `shiori_grep` (clone is on disk)
- `shiori_read_file`, `shiori_read_issue`
- `shiori_list_tree`

### What is skipped

- Code tree-sitter chunking (no code embeddings in DB)
- PR review submissions
- Background sync

---

## Onboarding Track B: Development (Writable) Setup

Use this for repositories an AI agent actively clones, edits, and creates PRs for (e.g. `masuda-masuo/shiori`, `masuda-masuo/sunaba`).

### 1. Authentication

Development repos need write-scoped credentials (GitHub App or PAT with `repo` scope) because the token is used in contexts that may involve PR creation, though the ingest itself is read-only.

The token resolution order is: **TokenSocket > TokenCommand > PAT (static) > anonymous**.

#### Option B1: Mint Socket (recommended for containers)

A host-side systemd socket-activated service mints short-lived tokens from the OS keyring. Set in `.env`:

```env
GITHUB_TOKEN_SOCKET=/run/shiori/mint.sock
```

The compose file mounts the socket directory. Install the mint service via mcp-launcher's `scripts/install-mint-socket.sh` (issue #204 / mcp-launcher#42).

#### Option B2: TokenCommand (host-side only)

```env
GITHUB_TOKEN_COMMAND=mcp-token github
```

#### Option B3: Static PAT (simple, less secure)

```env
GITHUB_TOKEN=ghp_xxx
```

> **Retired:** mounting the App private key as a Docker secret (Strategy A) was removed in #243. The GitHub App key stays host-side; use the mint socket for containers.

If token resolution fails, Shiori raises a `RuntimeError` and logs it in `shiori_status.last_error` rather than silently falling back to anonymous access.

### 2. Environment

```bash
cp .env.example .env
```

Minimum `.env` for dev-only setup:

```env
SHIORI_REPOS=masuda-masuo/shiori
SHIORI_DEV_REPOS=masuda-masuo/shiori
```

Mixed setup (dev + reference):

```env
SHIORI_REPOS=masuda-masuo/shiori,astral-sh/ruff
SHIORI_DEV_REPOS=masuda-masuo/shiori
SHIORI_REF_BACKFILL_SINCE=2024-01-01
```

### 3. Start services

```bash
docker compose up -d --build
```

### 4. Initial ingest

```bash
# Dev repo gets full backfill (all history)
# Ref repos are bounded by SHIORI_REF_BACKFILL_SINCE
docker compose run --rm ingest --repo owner/repo
```

### 5. Sync habits

When you need up-to-date indices during development:

```bash
# Quick: fetch only (API + git pull, no embedding)
./scripts/ingest.sh fetch --repo owner/repo

# Full: fetch + index
./scripts/ingest.sh run --repo owner/repo

# Single repo (fast when others are frozen):
./scripts/ingest.sh fetch --repo masuda-masuo/shiori
```

Because of per-repo PostgreSQL advisory locks, you can run a fetch for a dev repo in one terminal while a reference backfill runs in another — no kill-and-restart needed.

`ingest.sh` also picks the compute device itself: it runs with GPU (adding `docker-compose.gpu.yml`) when a GPU is visible to the host (`nvidia-smi -L` succeeds) and the NVIDIA container toolkit is installed; otherwise it falls back to CPU. The chosen device and the reason are written to the run log as `device=<gpu|cpu> reason="..."` (in `logs/ingest/<lane>/`, or wherever `SHIORI_LOG_DIR` points). To force a device without detection, set `SHIORI_GPU=1` for GPU or `SHIORI_GPU=0` for CPU — any non-empty value other than `1` forces CPU.

### 6. Adding a new large reference repo to an existing setup

```bash
# Seed to limit backfill depth
./scripts/ingest.sh run --backfill-since 2023-01-01 --repo new-owner/new-repo
```

Add the repo to `SHIORI_REPOS` in `.env` before running (allowlist validation).

---

## Authentication Configuration Details

`build_token_provider()` resolves credentials in the order: **TokenSocket > TokenCommand > PAT (static) > anonymous**.

The token is always minted **host-side** by `mcp-token` (from the GitHub App key in the OS keyring); shiori only pulls the short-lived result. **The App private key never enters the container or disk.**

1. **Mint Socket (containers: WSL2, Desktop, and remote VMs)**: A host-side systemd socket-activated service mints a short-lived token from the OS keyring on every connection and streams it back (pull-based; no periodic refresh timer). Set `GITHUB_TOKEN_SOCKET=/run/shiori/mint.sock` in `.env`; the compose file mounts the socket's host directory (`${SHIORI_MINT_SOCKET_DIR:-${XDG_RUNTIME_DIR}/mcp-token}`) at `/run/shiori`. **No private keys are placed inside the container or on disk.**
   - *Note*: because it is pull-based (connect-on-demand, not a push timer), there is no clock-drift window across host sleep/resume.
2. **TokenCommand (server run directly on the host, no Docker)**: Configure `GITHUB_TOKEN_COMMAND=mcp-token github`; the host process mints straight from the OS keyring.

You can inspect the active provider (e.g. `token_socket`, `static`, `token_command`) via the `token_provider` field in `shiori_status`.

---

## Persistent WSL2 / Linux Daemon (systemd)

For permanent local usage, deploy Shiori as a systemd user unit:

```bash
./scripts/install-systemd.sh
```

This replaces the `@SHIORI_DIR@` placeholder in the unit template with your repository's absolute path:

| Unit File | Target Unit | Description |
| --- | --- | --- |
| `scripts/shiori.service` | `shiori.service` | Runs the startup wrapper, which waits for the Docker server and starts the app container; retries retryable failures (#467). |
| `scripts/systemd/shiori-ingest-dev.{service,timer}` | `shiori-ingest-dev.{service,timer}` | Periodic steady sync for dev repositories (~15 min). |
| `scripts/systemd/shiori-ingest-ref.{service,timer}` | `shiori-ingest-ref.{service,timer}` | Periodic steady sync for reference repositories (daily). |
| `scripts/systemd/shiori-mint-mount.{service,timer}` | `shiori-mint-mount.{service,timer}` | Reconciles `/run/shiori` mint socket bind mount metadata (~120s) across WSL tmpfs runtime recreations (#461). Checks metadata only; does not mint tokens. |

### Prerequisites & Dependencies

- **Python 3 on Host**: `shiori.service`'s startup wrapper (including its Docker readiness wait) and `shiori-mint-mount.service` require `/usr/bin/python3` on the host.
- **Mint Socket Unit**: Shiori does **not** ship a mint-socket unit. Install it separately via mcp-launcher's `scripts/install-mint-socket.sh` (see [design doc 15](../design/15_token_supply_path.md)).

### Docker readiness wait and restart policy (#467)

The repository-managed unit runs one startup wrapper as its main process:

```ini
ExecStart=/usr/bin/python3 @SHIORI_DIR@/scripts/start_shiori.py
Restart=on-failure
RestartPreventExitStatus=2
RestartSec=30s
TimeoutStartSec=5min
StartLimitIntervalSec=0
```

The stdlib-only wrapper calls `wait_for_docker.py` first. The helper probes
`docker version --format '{{.Server.Version}}'` using the service's inherited
Docker context/environment. A socket alone is not enough; the probe must exit
0 with a non-empty server version. The helper's exit 1 means its Docker wait
budget was exhausted, so the wrapper exits 1 and systemd retries after
`RestartSec=30s`. Exit 2 means invalid configuration; the wrapper preserves 2,
and `RestartPreventExitStatus=2` tells systemd not to retry it. Compose errors
are normalized to wrapper exit 1, including a compose exit 2, so transient
startup failures remain retryable.

The wrapper is the main `ExecStart` process because systemd 255's
`RestartPreventExitStatus=` applies only to the main process, not to
`ExecStartPre` commands. Keeping the helper as `ExecStartPre` would therefore
make its exit 2 retry. See the
[systemd 255 service documentation](https://www.freedesktop.org/software/systemd/man/255/systemd.service.html#RestartPreventExitStatus=).

Each helper attempt waits up to **120 seconds** in total. Each Docker query is
capped at **5 seconds**, with failed probes retried every **2 seconds**; query
and sleep lengths are clamped to the remaining budget. Missing CLI, missing
socket, API errors and query timeouts are retryable within the budget, and a
hung query is killed and reaped. The helper logs start, success and exhaustion
lines plus a concise failure category; it does not copy Docker output or the
inherited environment into the journal. Invalid numeric
`SHIORI_WAIT_DOCKER_*` values are reported without exposing raw values or
secrets.

`StartLimitIntervalSec=0` (option A) disables the start-rate limit, with no
`StartLimitBurst`, so retryable failures continue indefinitely.
`RemainAfterExit=yes` keeps a successful run `active(exited)`; it does not
restart on success. An explicit `systemctl --user stop` is not undone by
`Restart=on-failure`, and the mint-mount reconciler continues to skip a
non-active service. `TimeoutStartSec=5min` bounds the entire startup wrapper,
Docker wait, compose command, and container startup. Initial image pulls or
builds can exceed this limit on a slow or cold host; run
`docker compose pull` / `docker compose build app` before starting the
service in that case.

### Migrating from the host-only `wait-docker.conf` (#467)

Older WSL2 setups used an unmanaged host-only drop-in at
`${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/shiori.service.d/wait-docker.conf`.
If it remains, the old shell wait and the new unit wait both run. The installer
refuses to install anything while that file exists; it never deletes or
rewrites unmanaged drop-ins.

Run the migration in a safe maintenance window when Docker is known to work:

1. Back up the current unit and the old drop-in:

   ```bash
   systemctl --user cat shiori.service > ~/shiori.service.before-467
   cp "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/shiori.service.d/wait-docker.conf" \
      ~/wait-docker.conf.before-467
   ```

2. Confirm no startup is in progress:

   ```bash
   systemctl --user show shiori.service -p ActiveState -p SubState
   ```

   Do not continue while the unit is `activating`.

3. Stop the service. The mint-mount reconciler skips non-active services, so
   this manual stop is preserved for the duration of the maintenance window:

   ```bash
   systemctl --user stop shiori.service
   ```

4. Remove **only** the old `wait-docker.conf` file. Do not remove other
   drop-ins or the `shiori.service.d` directory:

   ```bash
   rm -- "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/shiori.service.d/wait-docker.conf"
   ```

5. Pull the updated checkout and run the installer. Its normal `enable --now`
   behaviour starts the service again; the maintenance stop is temporary:

   ```bash
   git pull --ff-only
   ./scripts/install-systemd.sh
   ```

   To validate before merge, run `gh pr checkout <PR番号>` instead of
   `git pull --ff-only`, then run `./scripts/install-systemd.sh` from that
   PR checkout. After the PR is merged, switch back to main with
   `git switch main`, run `git pull --ff-only`, and rerun
   `./scripts/install-systemd.sh` to update the installation from main.

6. Inspect the effective unit. Check that the main `ExecStart` runs the
   checkout wrapper and that the unit has no duplicate Docker readiness wait
   wired as `ExecStartPre`. Existing unrelated custom `ExecStartPre` commands
   may remain; do not remove them.

   ```bash
   systemctl --user daemon-reload
   systemctl --user cat shiori.service
   systemctl --user show shiori.service -p ExecStart -p RestartPreventExitStatus
   ```

   Expected (paths replaced with your checkout):

   ```ini
   ExecStart=/usr/bin/python3 /path/to/shiori/scripts/start_shiori.py
   Restart=on-failure
   RestartPreventExitStatus=2
   RestartSec=30s
   TimeoutStartSec=5min
   StartLimitIntervalSec=0
   ```

7. Run the installer a second time and confirm it does not recreate the old
   drop-in or introduce a second Docker wait:

   ```bash
   ./scripts/install-systemd.sh
   systemctl --user cat shiori.service
   ```

Never blank-reset `ExecStartPre` to erase user customisations. An empty
`ExecStartPre=` clears every `ExecStartPre` for the unit, including unrelated
user settings; remove only the known old Docker-wait drop-in instead.

### Real-machine validation (USER-run, pending: 検証待ち)

These procedures are run by the human operator on the real WSL2/systemd host
after installing the reviewed checkout during a maintenance window. They have
**not** been executed for this change; results remain **検証待ち (pending)**. Record the actual `systemctl --version`
(the reference environment is systemd 255.4), the final unit text, and each
observed result. Do not use `wsl --terminate` for these checks.

#### T1: startup failure -> retry -> automatic active after Docker returns

1. **Before** stopping Docker Desktop, stop the service while Docker still
   works and confirm it is inactive. Starting an already `active(exited)` unit
   does not re-run the main `ExecStart` wrapper and would not test anything:

   ```bash
   systemctl --user stop shiori.service
   systemctl --user is-active shiori.service   # inactive
   ```

2. Stop Docker Desktop on the Windows side. Do not stop the WSL distro and do
   not use `wsl --terminate`. Record the test start time.

3. Request a start without blocking:

   ```bash
   systemctl --user start --no-block shiori.service
   ```

4. Observe the journal and the retry counters:

   ```bash
   journalctl --user -u shiori.service --since "<test start>" --no-pager
   systemctl --user show shiori.service \
     -p ActiveState -p SubState -p Result -p NRestarts
   ```

5. Confirm the wrapper logs the helper's Docker wait exhaustion after about
   120 seconds and exits 1, then confirm the unit is retried after
   `RestartSec=30s`. With restart enabled the unit may not remain
   in a stable `failed` state; it can move straight to
   `activating (auto-restart)`. **Do not judge by a momentary `is-failed`
   result**; use the journal's failure/restart entries and the increase in
   `NRestarts`. Invalid helper configuration exits 2 and is not retried
   because of `RestartPreventExitStatus=2`; compose failures (including
   compose exit 2) are normalized to retryable exit 1.

6. After at least one retry has been observed, start Docker Desktop on the
   Windows side and wait until the Ubuntu integration is back and the Docker
   API answers.

7. Without any manual `systemctl start/restart` or `docker compose up/restart`,
   confirm that `shiori.service` becomes `active(exited)` with
   `Result=success` and the app works. In the journal, follow the wrapper's
   successful helper wait into the successful compose start.

8. If Docker Desktop asks for an integration dialog or a manual recovery click,
   record that fact and its timestamp separately. Distinguish restoring the
   Docker connection itself from whether Shiori recovered afterwards without a
   manual start.

#### T2: explicit stop stays stopped

With Docker available and the service normally active, run:

```bash
systemctl --user stop shiori.service
systemctl --user is-active shiori.service   # inactive
```

Wait at least **90 seconds** (three times `RestartSec`), then check
`NRestarts` and the journal: there must be no automatic restart and the unit
must remain inactive. Confirm that a mint-mount timer run also skips the
non-active service and does not start the app. If possible, separately stop the
service while it is `activating (auto-restart)` and record that the retry
stops. Any `systemctl --user start` used to return to normal operation after
the test is a manual post-test action, not part of the test.

#### T3: a successful start does not loop

With Docker available, start the service once and confirm `active(exited)` and
`Result=success`. Observe for at least **3 minutes** and confirm that
`ActiveEnterTimestamp` does not change, `NRestarts` does not increase, and
`ExecStart` is not repeated in the journal. The mint-mount timer performs
periodic metadata checks in a different unit; do not confuse those with a
`shiori.service` restart. When the app is available, also record its
`StartedAt` as supporting evidence that it was not restarted without reason.

#### T4: historical comment correction (deferred)

The old host drop-in comment says "oneshot cannot be combined with
`Restart=on-failure`". The systemd 255 documentation says the values forbidden
for `Type=oneshot` are `Restart=always` and `Restart=on-success`, so that
comment is not authoritative. However, rewriting it and claiming real-machine
verification is **deferred until T1-T3 evidence exists**; its status is
**検証待ち**. Do not rewrite the historical comment, do not claim real-machine
verification, and do not treat the documentation reading as a substitute for
the T1-T3 results.

### Mint Socket Mount Reconciliation (WSL2 / Linux)

On WSL2 and Linux, `/run/user/<uid>` is backed by `tmpfs`. Depending on configuration and session lifecycle, when WSL or the user session restarts, the runtime directory may be recreated on a fresh `tmpfs` inode, leaving existing containers with a stale bind mount to the dead filesystem node.

The `shiori-mint-mount.timer` invokes `scripts/reconcile_mint_mount.py` for
metadata checks only; it does not mint tokens or refresh credentials.
`OnActiveSec=10s` schedules the first check 10 seconds after timer activation
(with `AccuracySec=10s`), then `OnUnitActiveSec=2m` schedules repeat checks
two minutes after each service run. The timer has no calendar schedule and no
`Persistent=true` calendar catch-up behavior.

- Inspects the host source generation (`st_dev`, `st_ino`, path) and container ID against `${XDG_STATE_HOME:-$HOME/.local/state}/shiori/mint-mount.json`.
- When filesystem generation changes (or on initial baseline), recreates the app container alone (`docker compose up -d --force-recreate --no-deps app`) without touching the database or ingest jobs; when generation is unchanged but container ID changed (e.g. external `docker compose up -d app`), adopts the new container ID without downtime.
- Implements a 120-second retry cooldown keyed on filesystem generation and atomic flock to avoid restart loops.
- Keeps the existing `--force-recreate` recovery command. A separate manual restart comparison also recovered, but preserving the container ID did not justify changing the proven recreation path.
- Real WSL acceptance against the installed timer without a local override is pending human verification; #461 remains open.

**WSL timer reactivation observations:** C1 deliberately disabled the
mint-mount timer for the comparison. In C3, after `wsl --terminate Ubuntu`,
the runtime generation changed from dev 136 to 63 while the timer was disabled;
the same container ID remained and its socket connect failed (111). C3 was not
a timer malfunction. In C4, a manual app restart restored the connection and
printed `CONNECT_OK` with that same container ID. C5 was a separate run at
generation 116 after the timer had been re-enabled: only in C5 was the original
`OnBootSec` timer observed active with no `NEXT` trigger and no initial run.
After a temporary `OnActiveSec=10s` drop-in was added, the 03:22 timer run
automatically force-recreated the app and the socket check printed
`CONNECT_OK`. This describes the observed WSL path and does not claim that
systemd always ignores overdue `OnBootSec` timers. A local diagnostic drop-in
may exist at
`~/.config/systemd/user/shiori-mint-mount.timer.d/activation.conf`.

#### Final WSL acceptance (after merge and deploy)

Run this procedure in the main deployment checkout after the PR is merged.
The commands below are instructions for the human operator; this documentation
change does not perform host operations.

1. Pull the merged change and install the copied user units:

   ```bash
   git pull --ff-only
   ./scripts/install-systemd.sh
   systemctl --user cat shiori-mint-mount.timer
   ```

   The installer copies unit files into `~/.config/systemd/user`, reloads the
   user manager, and enables/starts Shiori services and timers. It may trigger
   the app baseline, so take the pre-termination generation measurement only
   after installation and the checks below. In the first `cat` output, inspect
   the installed **base unit file** and confirm its `[Timer]` section contains
   `OnActiveSec=10s` and no `OnBootSec`. Seeing `OnActiveSec=10s` only in a
   temporary drop-in is insufficient.

2. Remove only the known local diagnostic drop-in, reload, and inspect the
   effective unit again:

   ```bash
   rm -- ~/.config/systemd/user/shiori-mint-mount.timer.d/activation.conf
   systemctl --user daemon-reload
   systemctl --user cat shiori-mint-mount.timer
   ```

   Confirm the second output no longer lists the `activation.conf` drop-in
   file or section, while the base unit still has `OnActiveSec=10s` and no
   `OnBootSec`. Do not remove the drop-in directory or any runtime directory.

3. Restart the installed timer and confirm `shiori-mint-mount.timer` has a
   `NEXT` time:

   ```bash
   systemctl --user restart shiori-mint-mount.timer
   systemctl --user list-timers 'shiori-*' --no-pager
   ```

4. Record the current runtime generation and app container ID after
   installation, keep Docker Desktop running, then run
   `wsl --terminate Ubuntu` from Windows:

   ```bash
   stat /run/user/1000/mcp-token
   docker compose ps app
   ```

   Reopen the WSL shell and wait three minutes. Do not manually repair the
   mount or restart the app during this observation. Do not remove or replace
   runtime directories.

5. Confirm the generation changed, inspect the reconciler journal, and run a
   connect-only request from the app container:

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

   Compare the source generation with the recorded pre-termination values and
   confirm the reconciler ran and the container was recreated. Connecting
   triggers minting; do not call `recv()` or print socket data. Acceptance
   remains **UNVERIFIED** until this procedure succeeds against the installed
   timer without the override.

Useful monitoring commands:
```bash
systemctl --user list-timers 'shiori-*' --no-pager
systemctl --user status shiori-mint-mount.timer
journalctl --user -u shiori-mint-mount -f
```

Manual recovery fallback (if needed):
```bash
docker compose up -d --force-recreate --no-deps app
```

### Pitfalls

- **No Auto-Build**: `shiori.service` does not rebuild Docker images. After code updates:
  ```bash
  docker compose build app
  systemctl --user restart shiori.service
  ```
- **Token Expiry**: App installation tokens expire after 60 minutes. Ensure the mint socket service is running.

---

## Customizing the Embedding Model

The embedding model (`intfloat/multilingual-e5-small`) is baked into the Docker image at build time. Runtime env var override is not supported.

To use a different model, fork the repository and edit `docker/app/Dockerfile`:

```dockerfile
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('your-model-name')" || echo "WARNING: model pre-download failed, will lazy-load at runtime"
```

Then rebuild:

```bash
docker compose build app
./scripts/ingest.sh run --rebuild --repo owner/repo
```

---

## Start from Published Images on a GPU-less Host

On a small or GPU-less host (e.g. a lightweight cloud VM), you can run Shiori using prebuilt images published to GHCR without building images from source:

1. In `.env`, specify the prebuilt CPU app image, DB image, and CPU torch index URL:
   ```env
   SHIORI_APP_IMAGE=ghcr.io/masuda-masuo/shiori/app:cpu
   SHIORI_DB_IMAGE=ghcr.io/masuda-masuo/shiori/db:latest
   SHIORI_TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
   ```
   Setting `SHIORI_TORCH_INDEX_URL` is required so that a pull failure does not build CUDA torch under the CPU tag during fallback builds.

2. (Optional) On memory-constrained hosts, tune down PostgreSQL memory allocations:
   ```env
   SHIORI_PG_SHARED_BUFFERS=256MB
   SHIORI_PG_EFFECTIVE_CACHE_SIZE=1GB
   ```

3. Start services without `--build`:
   ```bash
   docker compose up -d
   ```

---

## MCP Client Connection

Connect MCP clients using Streamable HTTP at `http://localhost:8765/mcp`.

### Example: Claude Desktop / Claude Code

```bash
claude mcp add --transport http shiori http://localhost:8765/mcp
```

### Alternative: Local Python stdio Connection (Debug)

```bash
pip install -e .
DATABASE_URL=postgresql://shiori:shiori@localhost:5432/shiori \
  python -m shiori serve --transport stdio
```

---

## Troubleshooting

- **0 Search Results**: Confirm that `ingest` finished successfully and `SHIORI_REPOS` matches active paths.
- **Stale Code Execution**: Docker Compose does not automatically rebuild images on `run` or `up`. Rebuild manually using `docker compose build` or pass `--build`.
- **VCS Rate Limits**: Authenticate using `GITHUB_TOKEN` to lift GitHub API rate limits.
- **Sync stuck on a repo**: See if another process is holding the lock — `pg_locks` with `classid = 1363021135` (0x5348494F) and `objid = hashtext(repo)`.
