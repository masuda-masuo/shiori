"""shiori configuration.

Everything reads from environment variables. Passed via docker compose `.env`."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _repos_from_env() -> list[str]:
    raw = os.environ.get("SHIORI_REPOS", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def _index_bot_logins_from_env() -> set[str]:
    """Return SHIORI_INDEX_BOT_LOGINS as a lowercase set."""
    raw = os.environ.get("SHIORI_INDEX_BOT_LOGINS", "")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _code_extensions_from_env() -> set[str]:
    """Return SHIORI_CODE_EXTENSIONS as a lowercase set."""
    raw = os.environ.get("SHIORI_CODE_EXTENSIONS", "")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _code_exclude_globs_from_env() -> list[str]:
    """Return SHIORI_CODE_EXCLUDE_GLOBS as a list."""
    raw = os.environ.get("SHIORI_CODE_EXCLUDE_GLOBS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _dev_repos_from_env() -> set[str]:
    """Return dev repos (code-indexed) from SHIORI_DEV_REPOS.

    Backward compat: if SHIORI_DEV_REPOS is unset and SHIORI_INDEX_CODE=true,
    all repos are treated as dev. SHIORI_INDEX_CODE is deprecated.
    """
    raw = os.environ.get("SHIORI_DEV_REPOS", "")
    if raw:
        return {s.strip() for s in raw.split(",") if s.strip()}
    index_code = os.environ.get("SHIORI_INDEX_CODE", "").lower() in (
        "1", "true", "yes"
    )
    if index_code:
        return set(_repos_from_env())
    return set()


def _allow_rebuild_from_env() -> bool:
    """Return SHIORI_ALLOW_REBUILD as a bool."""
    return os.environ.get("SHIORI_ALLOW_REBUILD", "").lower() in (
        "1", "true", "yes"
    )


#: Default pending-work volume that triggers the bulk path (issue #376).
#: A backlog of this many un-indexed/stale items across the targeted repos
#: means batching (ChunkBuffer) pays off; routine incremental runs stay far
#: below it and keep today's item-at-a-time path.
DEFAULT_BULK_PENDING_THRESHOLD: int = 10_000


def _bulk_pending_threshold_from_env() -> int:
    """Return SHIORI_BULK_PENDING_THRESHOLD as a positive int (issue #376).

    Unset, empty, unparseable, or non-positive values fall back to
    ``DEFAULT_BULK_PENDING_THRESHOLD`` -- the process must never crash on a
    bad value.
    """
    raw = os.environ.get("SHIORI_BULK_PENDING_THRESHOLD", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BULK_PENDING_THRESHOLD
    if value <= 0:
        return DEFAULT_BULK_PENDING_THRESHOLD
    return value


# Default embedding model baked into the image (docker/app/Dockerfile).
# To change, fork the image and rebuild. Runtime env var override removed (#255).
DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
EMBEDDING_DIM: int = 384


@dataclass
class Settings:
    # Postgres (pgvector + pgroonga)
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL", "postgresql://shiori:shiori@db:5432/shiori"
        )
    )
    # For private repos. Works unset with public repos only (stricter rate limits).
    github_token: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN") or None
    )
    # External command to obtain token (e.g. "mcp-token github").
    # Gets called periodically; stdout is the token. Optional.
    github_token_command: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN_COMMAND") or None
    )
    # Unix socket path for on-demand token minting (e.g. "/run/shiori/mint.sock").
    # The socket is served by a host-side systemd socket-activated service
    # that runs mcp-token github on each connection. Preferred over TokenCommand.
    github_token_socket: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN_SOCKET") or None
    )
    # Target repos. "owner/name" comma-separated, multiple allowed.
    repos: list[str] = field(default_factory=_repos_from_env)
    # Clone destination and working data.
    data_dir: str = field(
        default_factory=lambda: os.environ.get("SHIORI_DATA_DIR", "/data")
    )
    # Max chunk characters (character-based. See detailed design/02 decisions).
    chunk_max_chars: int = field(
        default_factory=lambda: int(os.environ.get("SHIORI_CHUNK_MAX_CHARS", "1200"))
    )
    # Search result defaults (see detailed design/05 and 06 decisions).
    default_top_k: int = field(
        default_factory=lambda: int(os.environ.get("SHIORI_TOP_K", "8"))
    )
    snippet_chars: int = field(
        default_factory=lambda: int(os.environ.get("SHIORI_SNIPPET_CHARS", "400"))
    )
    # Pull-type sync debounce interval (seconds). Max seconds between Phase 1
    # (clone refresh) pulls for the same repo. 0 disables (always pull).
    # Formerly the background auto-sync interval; repurposed in #236.
    sync_interval_seconds: int = field(
        default_factory=lambda: int(
            os.environ.get("SHIORI_SYNC_INTERVAL_SECONDS", "0")
        )
    )
    # --- Steady sync (issue #347): EXPECTED host-timer cadence ---
    # These document the cadence of the two host-level systemd user timers
    # (scripts/systemd/) that call the CLI with --only-dev / --only-ref --
    # there is no in-server sync loop reading these values on a schedule.
    # shiori_status uses them only to derive a role-aware staleness
    # threshold (2x the expected interval; see tools/status.py).
    dev_sync_interval_seconds: int = field(
        default_factory=lambda: int(
            os.environ.get("SHIORI_DEV_SYNC_INTERVAL_SECONDS", "900")
        )
    )
    ref_sync_interval_seconds: int = field(
        default_factory=lambda: int(
            os.environ.get("SHIORI_REF_SYNC_INTERVAL_SECONDS", "86400")
        )
    )
    # MCP server (streamable HTTP)
    mcp_host: str = field(
        default_factory=lambda: os.environ.get("SHIORI_MCP_HOST", "0.0.0.0")
    )
    mcp_port: int = field(
        default_factory=lambda: int(os.environ.get("SHIORI_MCP_PORT", "8765"))
    )
    # Allowlist for indexing bot logins (comma-separated. Issue #25).
    # Posts via GitHub App get [bot] suffix, but bots acting on
    # behalf of users can be allowlisted here for indexing.
    index_bot_logins: set[str] = field(default_factory=_index_bot_logins_from_env)
    # --- Source code indexing (detailed design/10, decision 7) ---
    # Dev repos get code indexed. Reference repos (in SHIORI_REPOS but not
    # in SHIORI_DEV_REPOS) are clone-only (grep-able via shiori_grep).
    # SHIORI_INDEX_CODE is deprecated; removed in a future release.
    dev_repos: set[str] = field(default_factory=_dev_repos_from_env)
    # Code file extensions (lowercase). Empty/unset = all code extensions.
    code_extensions: set[str] = field(default_factory=_code_extensions_from_env)
    # Exclude glob patterns (comma-separated). E.g. "**/test_*, **/migrations/*"
    code_exclude_globs: list[str] = field(
        default_factory=_code_exclude_globs_from_env
    )
    # Allow rebuild=True from CLI ingest (full TRUNCATE). Issue #63.
    # Default false. Set true only when operationally required.
    # CLI path (python -m shiori ingest --rebuild) always allowed regardless.
    allow_rebuild: bool = field(default_factory=_allow_rebuild_from_env)
    # Fetch-phase concurrency cap. Max number of repos fetched simultaneously.
    # The actual worker count is max(1, min(len(targets), this value)).
    fetch_concurrency: int = field(
        default_factory=lambda: int(
            os.environ.get("SHIORI_FETCH_CONCURRENCY", "4")
        )
    )
    # Backfill seed for ref repos (not in SHIORI_DEV_REPOS) whose cursor is None.
    # YYYY-MM-DD format. CLI --backfill-since overrides this for all targets.
    # Dev repos are never seeded by this env var (always full backfill).
    ref_backfill_since: str | None = field(
        default_factory=lambda: os.environ.get("SHIORI_REF_BACKFILL_SINCE") or None
    )
    # --- Circuit breaker: stop retrying a repo after N consecutive failures (issue #345) ---
    # Set to 0 to disable the circuit breaker entirely.
    cb_threshold: int = field(
        default_factory=lambda: int(
            os.environ.get("SHIORI_CB_THRESHOLD", "5")
        )
    )
    # Base backoff in seconds. The actual backoff grows exponentially:
    #   min(cb_max_backoff, cb_base_backoff * 2^(failures - 1))
    cb_base_backoff: float = field(
        default_factory=lambda: float(
            os.environ.get("SHIORI_CB_BASE_BACKOFF", "60")
        )
    )
    # Maximum backoff cap in seconds. Prevents exponential growth from
    # parking a repo for days.
    cb_max_backoff: float = field(
        default_factory=lambda: float(
            os.environ.get("SHIORI_CB_MAX_BACKOFF", "3600")
        )
    )
    # --- Rate-limit handling (issue #345) ---
    # Maximum seconds to wait on a rate-limit response (Retry-After or
    # x-ratelimit-reset). A bogus reset value cannot park the process
    # beyond this cap.
    rate_limit_max_wait: float = field(
        default_factory=lambda: float(
            os.environ.get("SHIORI_RATE_LIMIT_MAX_WAIT", "60")
        )
    )
    # Maximum number of retries when rate-limited before giving up and
    # recording a failure.
    rate_limit_max_retries: int = field(
        default_factory=lambda: int(
            os.environ.get("SHIORI_RATE_LIMIT_MAX_RETRIES", "3")
        )
    )
    # --- Heavy index build knobs (issue #352) ---
    # PostgreSQL maintenance_work_mem for the session that builds HNSW/pgroonga
    # indexes. Unset (default) leaves the server's own default alone.
    maintenance_work_mem: str | None = field(
        default_factory=lambda: os.environ.get("SHIORI_MAINTENANCE_WORK_MEM") or None
    )
    # max_parallel_maintenance_workers for the same session. Default 0 forces
    # a serial HNSW build: PARALLEL HNSW allocates ~maintenance_work_mem of
    # DSM in /dev/shm, and Docker's 64MB default there overflows it ("could
    # not resize shared memory segment ... No space left on device").
    max_parallel_maintenance_workers: int = field(
        default_factory=lambda: int(
            os.environ.get("SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS", "0")
        )
    )
    # --- Bulk-path trigger (issue #376) ---
    # Pending (not-yet-indexed) items across the targeted repos at or above
    # which the invocation takes the bulk (batched) path. A single COUNT
    # over issue_items decides; nothing is truncated or re-fetched.
    # Unset/empty/unparseable/non-positive values fall back to the default.
    bulk_pending_threshold: int = field(
        default_factory=_bulk_pending_threshold_from_env
    )

    def repo_dir(self, repo: str) -> str:
        owner, name = repo.split("/", 1)
        return os.path.join(self.data_dir, "repos", f"{owner}__{name}")

def load_settings() -> Settings:
    return Settings()
