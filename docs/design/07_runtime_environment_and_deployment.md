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
*   Embedding weights are baked into the app image at build time (`docker/app/Dockerfile`). The default model is pre-downloaded to `/models` during `docker build` and loaded with `HF_HUB_OFFLINE=1` at runtime, eliminating startup-time HuggingFace requests (issue #238).
*   Repository clones are mapped to a **bind mount** (`./data:/data`) rather than a named volume. This allows other developer tools (like `sunaba`) to copy from the clones directory directly without permission blocks.

---

## 4. Ingestion Triggers

Index states are updated through two channels:

1.  **Background Sync Polling (Primary)**: The `app` process polls the API using an in-process thread when `SHIORI_SYNC_INTERVAL_SECONDS` is set (recommended: 10s).
2.  **CLI Command**: The `python -m shiori ingest` command is used for initial setup and cron schedules.

### Concurrent Sync Guard
Concurrent sync operations are serialized using database-level advisory locks (`pg_try_advisory_lock`). If a sync job is already active, subsequent triggers are skipped and report a `skipped` state without modifying database records.

Authentication is configured via `build_token_provider()`. Under Docker Compose, the container pulls a host-minted short-lived token from the mint socket (`GITHUB_TOKEN_SOCKET`); the GitHub App private key stays in the host keyring and is never mounted into the container (the in-container App-PEM mount, Strategy A, was retired in #243). (See [Setup Guide](../guides/setup.md) for details).

---

## 5. GPU-Accelerated Ingestion (Issue #383)

`scripts/ingest.sh` decides between CPU and GPU on every run and adds the
`docker-compose.gpu.yml` overlay when GPU is chosen. The overlay gives both
`app` and `ingest` a `deploy.resources.reservations.devices` NVIDIA
reservation (1 GPU), and adds `runtime: nvidia` to `ingest` so the one-shot
container receives the GPU under `docker compose run`.

**Autodetection** (unset `SHIORI_GPU`) selects GPU only when both
prerequisites hold: a GPU is visible to the host (`nvidia-smi -L` succeeds;
the script checks PATH and `/usr/lib/wsl/lib` for WSL) **and** the NVIDIA
container toolkit is installed (`nvidia-container-runtime` /
`nvidia-container-cli` / `nvidia-ctk` on PATH). If either is missing the run
stays on CPU without the overlay. A failed or hanging probe falls back to
CPU and never aborts the run (`nvidia-smi` is bounded with `timeout 5`).

**Explicit setting beats autodetection**: `SHIORI_GPU=1` forces GPU (no
probe; the overlay is added unconditionally, so the run fails at startup if
the toolkit is absent), any other non-empty value (e.g. `SHIORI_GPU=0`)
forces CPU (no probe), and unset means autodetect. The chosen device and the
reason are written to the run log on every path as a fixed greppable line
`device=<gpu|cpu> reason="..."` (the log file is the source of truth; the
journal retains only ~30 minutes, see the execution-log notes in
`scripts/ingest.sh`, issue #372).

The overlay also pins the ONNX off-switch on the `ingest` service
(`SHIORI_ONNX_MODEL_PATH=""`), so GPU runs keep using
SentenceTransformer/CUDA rather than the CPU-only ONNX path -- see the
"empty-string off-switch" paragraph of §4 in
[03_embeddings_and_cross_lingual_search.md](03_embeddings_and_cross_lingual_search.md)
for the semantics.
