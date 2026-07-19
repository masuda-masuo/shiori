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
docker compose run --rm ingest

# Or specify seed per run (overrides env):
./scripts/ingest.sh run --backfill-since 2023-06-01
```

After this completes, the index contains issues and docs from the seed date onward. The repos are **frozen** — no periodic sync runs. To refresh manually:

```bash
docker compose run --rm ingest
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
docker compose run --rm ingest
```

### 5. Sync habits

When you need up-to-date indices during development:

```bash
# Quick: fetch only (API + git pull, no embedding)
./scripts/ingest.sh fetch

# Full: fetch + index
./scripts/ingest.sh run

# Single repo (fast when others are frozen):
./scripts/ingest.sh fetch --repo masuda-masuo/shiori
```

Because of per-repo PostgreSQL advisory locks, you can run a fetch for a dev repo in one terminal while a reference backfill runs in another — no kill-and-restart needed.

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
| `scripts/shiori.service` | `shiori.service` | Starts the app container (`docker compose up -d app`). |

Shiori does **not** ship a mint-socket unit. Install it separately via mcp-launcher's `scripts/install-mint-socket.sh`.

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
./scripts/ingest.sh run --rebuild
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
