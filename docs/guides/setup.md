# Setup Guide

## Prerequisites

*   Docker and Docker Compose.
*   (To index private repositories) A GitHub Personal Access Token (PAT) or credential configuration with repository read access.

---

## Onboarding Steps

```bash
git clone https://github.com/<your-username>/shiori.git
cd shiori
cp .env.example .env
# Edit .env:
#   SHIORI_REPOS=owner/name      (comma-separated list of target repos)
#   GITHUB_TOKEN=ghp_xxx         (optional for public repositories)
#   GHCR_USER=your-username      (default: masuda-masuo, for ghcr.io pull in update.sh)

# Start the DB and MCP server (builds images on first run)
docker compose up -d --build

# Ingest and create the search index (takes time to download the model on the first run)
# For private repositories, configure authentication first (see options below).
docker compose run --build --rm ingest
```

The `ingest` service runs a one-shot ingestion job. **Always pass `--build` when running Compose commands after editing source code** to ensure changes are built into the image.

---

## Authentication Configuration

`build_token_provider()` resolves credentials in the order: **TokenSocket > TokenCommand > PAT (static) > anonymous**.

The token is always minted **host-side** by `mcp-token` (from the GitHub App key in the OS keyring); shiori only pulls the short-lived result. **The App private key never enters the container or disk.** Choosing a method depends on **where the token is consumed**:

1.  **Mint Socket (containers: WSL2, Desktop, and remote VMs)**:
    A host-side systemd socket-activated service mints a short-lived token from the OS keyring on every connection and streams it back (pull-based; no periodic refresh timer). This service is **not part of shiori** -- it is installed separately via mcp-launcher's `scripts/install-mint-socket.sh` (issue #204 / mcp-launcher#42). Set `GITHUB_TOKEN_SOCKET=/run/shiori/mint.sock` in `.env`; the compose file mounts the socket's host directory (`${SHIORI_MINT_SOCKET_DIR:-${XDG_RUNTIME_DIR}/mcp-token}`) at `/run/shiori`. **No private keys are placed inside the container or on disk.**
    *   *Note*: because it is pull-based (connect-on-demand, not a push timer), there is no clock-drift window across host sleep/resume -- see detailed design/15.
2.  **TokenCommand (server run directly on the host, no Docker)**:
    Configure `GITHUB_TOKEN_COMMAND=mcp-token github`; the host process mints straight from the OS keyring.

> **Retired:** mounting the App private key as a Docker secret and letting the container mint its own tokens (Strategy A) was removed in #243. The GitHub App key stays host-side; use the mint socket for containers.

If token resolution fails, Shiori raises a `RuntimeError` and logs it in `shiori_status.last_error` rather than silently falling back to anonymous access. You can inspect the active provider (e.g. `token_socket`, `static`, `token_command`) via the `token_provider` field in `shiori_status`.

---

## Persistent WSL2 / Linux Daemon (systemd)

For permanent local usage, deploy Shiori as a systemd user unit. Run the installation script:

```bash
./scripts/install-systemd.sh
```

This script replaces the `@SHIORI_DIR@` placeholder in the unit template under `scripts/` with your repository's absolute path and places it in `~/.config/systemd/user/`:

| Unit File | Target Unit | Description |
| --- | --- | --- |
| `scripts/shiori.service` | `shiori.service` | Starts the app container (`docker compose up -d app`). |

Shiori does **not** ship a mint-socket unit. If you use the Mint Socket
authentication method above (`GITHUB_TOKEN_SOCKET`), install it separately
via mcp-launcher's own `scripts/install-mint-socket.sh` (issue #204 /
mcp-launcher#42) -- it is a shared host primitive, not specific to shiori.

### Pitfalls
*   **No Auto-Build**: `shiori.service` does not rebuild Docker images automatically. After updating code, rebuild manually:
    ```bash
    docker compose build app
    systemctl --user restart shiori.service
    ```
*   **Token Expiry**: App installation tokens expire after 60 minutes. If you use the Mint Socket method and its host-side unit (mcp-launcher) is disabled or not running, repository synchronization will fail once the cached token expires.

---

## Indexing Channels

| Channel | Mechanism | Use Case |
| --- | --- | --- |
| `SHIORI_SYNC_INTERVAL_SECONDS` | In-process background thread | Automated sync polling (e.g., checks every 10 seconds). |
| CLI Command | `python -m shiori ingest` | Initial index builds and cron schedule jobs. |

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
docker compose run --build --rm ingest -- --rebuild
```

---

## MCP Client Connection

Connect MCP clients using the Streamable HTTP transport at `http://localhost:8765/mcp`.

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

*   **0 Search Results**: Confirm that `ingest` finished successfully and check that `SHIORI_REPOS` matches active paths.
*   **Stale Code Execution**: Docker Compose does not automatically rebuild images on `run` or `up`. Rebuild manually using `docker compose build` or pass the `--build` flag.
*   **VCS Rate Limits**: Authenticate using `GITHUB_TOKEN` to lift GitHub API rate limits.
