# Detailed Design: Runtime Environment & Deployment

## 1. Purpose

Provide a reproducible runtime setup using Docker Compose to isolate the database and MCP server from the host machine.

---

## 2. Docker Compose Layout (2 Daemons + 1 One-shot Profile)

*   `db`: PostgreSQL daemon running `pgvector` and `pgroonga`.
*   `app`: The primary MCP server daemon. Runs background sync polling if `SHIORI_SYNC_INTERVAL_SECONDS` is set.
*   `ingest` (profile: `ingest`): One-shot ingestion container. Runs when triggered and terminates immediately.

### Separation of Concerns
By separating the database and server into two containers:
*   We prevent database instances from restarts during code updates.
*   We respect the one-process-per-container rule (PID 1).
*   We can use official base images.

---

## 3. Data Persistence

*   Database storage is mapped to a **named volume** so that index records and embeddings are preserved when containers are rebuilt.
*   Embedding weights are cached in a named volume (`HF_HOME=/models`).
*   Repository clones are mapped to a **bind mount** (`./data:/data`) rather than a named volume. This allows other developer tools (like `sunaba`) to copy from the clones directory directly without permission blocks.

---

## 4. Ingestion Triggers

Index states are updated through three channels:

1.  **Background Sync Polling (Primary)**: The `app` process polls the API using an in-process thread when `SHIORI_SYNC_INTERVAL_SECONDS` is set (recommended: 10s).
2.  **CLI Command**: The `python -m shiori ingest` command is used for initial setup and cron schedules.
3.  **MCP Tool**: Triggers sync on-demand.

### Concurrent Sync Guard
Concurrent sync operations are serialized using database-level advisory locks (`pg_try_advisory_lock`). If a sync job is already active, subsequent triggers are skipped and report a `skipped` state without modifying database records.

Authentication is configured via `build_token_provider()`. Under Docker Compose, the container pulls a host-minted short-lived token from the mint socket (`GITHUB_TOKEN_SOCKET`); the GitHub App private key stays in the host keyring and is never mounted into the container (the in-container App-PEM mount, Strategy A, was retired in #243). (See [Setup Guide](../guides/setup.md) for details).
