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

# Start the DB and MCP server (builds images on first run)
docker compose up -d --build

# Ingest and create the search index (takes time to download the model on the first run)
# For private repositories, configure authentication first (see options below).
docker compose run --build --rm ingest
```

The `ingest` service runs a one-shot ingestion job. **Always pass `--build` when running Compose commands after editing source code** to ensure changes are built into the image.

---

## Authentication Configuration

`build_token_provider()` resolves credentials in the order: **App > TokenCommand > PAT (static) > anonymous**. 

Choosing a method depends on **where the tokens are consumed** and **who is responsible for refreshing them**:

1.  **GitHub App Private Key (Recommended for Remote VMs)**:
    Mount your `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY_PATH` as read-only Docker secrets in a `docker-compose.override.yml`. Because the key resides in the container, **the container process refreshes tokens natively**. Useful for VMs that shut down automatically.
2.  **Token File Sharing (Recommended for WSL2 / Desktop)**:
    The host machine runs `mcp-token` to retrieve credentials from the OS keyring and write a short-lived token to `runtime/github-token` (run via `shiori-refresh.timer` every 5 minutes). The container mounts this folder (`./runtime:/run/shiori:ro`) and reads the token using `GITHUB_TOKEN_COMMAND=cat /run/shiori/github-token`. **No private keys are placed inside the container or on disk.**
    *   *Note*: Suspends and sleep states on the host pause the timer, which can cause temporary `401 Unauthorized` sync errors upon waking up until the timer fires.

If running the server process directly on the host (bypassing Docker), configure: `GITHUB_TOKEN_COMMAND=mcp-token github`.

If token resolution fails, Shiori raises a `RuntimeError` and logs it in `shiori_status.last_error` rather than silently falling back to anonymous access. You can inspect the active provider (e.g. `app`, `static`, `token_command`) via the `token_provider` field in `shiori_status`.

---

## Persistent WSL2 / Linux Daemon (systemd)

For permanent local usage, deploy Shiori as a systemd user unit. Run the installation script:

```bash
./scripts/install-systemd.sh
```

This script replaces the `@SHIORI_DIR@` placeholder in the unit templates under `scripts/` with your repository's absolute path and places them in `~/.config/systemd/user/`:

| Unit File | Target Unit | Description |
| --- | --- | --- |
| `scripts/shiori.service` | `shiori.service` | Runs `refresh-token.sh` and starts the app container (`docker compose up -d app`). |
| `scripts/shiori-refresh.service` | `shiori-refresh.service` | Executes `refresh-token.sh` once (triggered by the timer). |
| `scripts/shiori-refresh.timer` | `shiori-refresh.timer` | Triggers the refresh service every 5 minutes to keep tokens fresh. |

### Pitfalls
*   **No Auto-Build**: `shiori.service` does not rebuild Docker images automatically. After updating code, rebuild manually:
    ```bash
    docker compose build app
    systemctl --user restart shiori.service
    ```
*   **Token Expiry**: App installation tokens expire after 60 minutes. If the `shiori-refresh.timer` is disabled or fails to run, repository synchronization will fail after roughly one hour.

---

## Indexing Channels

| Channel | Mechanism | Use Case |
| --- | --- | --- |
| `SHIORI_SYNC_INTERVAL_SECONDS` | In-process background thread | Automated sync polling (e.g., checks every 10 seconds). |
| CLI Command | `python -m shiori ingest` | Initial index builds and cron schedule jobs. |

---

## Changing Embedding Models

If you change the vector model in your configuration, rebuild the entire index:

```bash
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

*   **`EMBEDDING_DIM=... but model produces ...`**: Model dimension mismatch. Align `.env`'s `EMBEDDING_DIM` value and run `ingest --rebuild`.
*   **0 Search Results**: Confirm that `ingest` finished successfully and check that `SHIORI_REPOS` matches active paths.
*   **Stale Code Execution**: Docker Compose does not automatically rebuild images on `run` or `up`. Rebuild manually using `docker compose build` or pass the `--build` flag.
*   **VCS Rate Limits**: Authenticate using `GITHUB_TOKEN` to lift GitHub API rate limits.
