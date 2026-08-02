"""shiori configuration.

Everything reads from environment variables. Passed via docker compose `.env`."""

from __future__ import annotations

import os
import time
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


def _ingest_time_budget_from_env() -> float | None:
    """Return SHIORI_INGEST_TIME_BUDGET as a positive float or None (issue #377).

    The value is *working* seconds: the budget is enforced with
    ``time.monotonic()``, which on Linux does not advance while the machine
    is suspended, so a run frozen for hours is not charged for the freeze.

    Unset, empty (the plain ``${VAR:-}`` compose form), unparseable, or
    non-positive values mean **unbounded** -- the default behaviour.  The
    process must never crash on a bad value (``float("")`` raises, which is
    exactly the trap of the ``${VAR:-}`` compose expansion).
    """
    raw = os.environ.get("SHIORI_INGEST_TIME_BUDGET", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


class IndexBudget:
    """Working-time budget for one index run (issue #377).

    Constructed once at the top of the index run from settings, carries the
    deadline, and exposes a single predicate ``exhausted()``.  Based on
    ``time.monotonic()``: on Linux it does **not** advance while the machine
    is suspended, so the budget measures *working* time rather than
    wall-clock -- a run frozen for 18 hours is not charged for the freeze.

    ``budget_seconds`` of None/0/negative/non-numeric means unbounded (the
    default): ``exhausted()`` is always False.  ``monotonic`` is injectable
    for tests; it defaults to ``time.monotonic``.
    """

    def __init__(
        self,
        budget_seconds: float | None,
        monotonic=time.monotonic,
    ) -> None:
        self.budget_seconds = budget_seconds
        self._monotonic = monotonic
        if isinstance(budget_seconds, (int, float)) and budget_seconds > 0:
            self._deadline: float | None = monotonic() + budget_seconds
        else:
            self._deadline = None

    def exhausted(self) -> bool:
        """True once the working-time budget has been consumed.

        Monotone: once True it stays True (monotonic only moves forward).
        """
        if self._deadline is None:
            return False
        return self._monotonic() >= self._deadline


#: Default pending-work volume that triggers the bulk path (issue #376).
#: A backlog of this many un-indexed/stale items across the targeted repos
#: means batching (ChunkBuffer) pays off; routine incremental runs stay far
#: below it and keep today's item-at-a-time path.
DEFAULT_BULK_PENDING_THRESHOLD: int = 10_000


def _int_from_env(name: str, default: int, minimum: int | None = None) -> int:
    """Return env var *name* as an int, with a per-setting range policy (#397).

    Unset, empty (the plain ``${VAR:-}`` compose form), whitespace-only, or
    unparseable values fall back to *default* -- the process must never
    crash on a bad value (``int("")`` raises, which is exactly the trap of
    the ``${VAR:-}`` compose expansion).

    *minimum* is inclusive: a parsed value below it also falls back to
    *default*; ``None`` accepts any parsed integer.  Zero and negative
    values mean different things for different settings (0 is a documented
    value for some), so each caller must derive *minimum* from the code
    that consumes the setting -- see the per-field comments in
    :class:`Settings`.
    """
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def _float_from_env(
    name: str, default: float, minimum: float | None = None
) -> float:
    """Return env var *name* as a float; same contract as ``_int_from_env``.

    The ``_cb_*_backoff_from_env`` helpers below are deliberately NOT
    expressed through this one: they reject non-positive values (an
    *exclusive* zero bound), which no caller of this helper needs --
    growing an exclusive-minimum option for those three alone is not worth
    the extra surface.
    """
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


#: Default fetch-phase concurrency cap (issue #386). Used when
#: SHIORI_FETCH_CONCURRENCY is unset, empty (the plain ``${VAR:-}``
#: compose form), unparseable, or non-positive.
DEFAULT_FETCH_CONCURRENCY: int = 4

#: Default circuit-breaker consecutive-failure threshold (issue #345).
#: Used when SHIORI_CB_THRESHOLD is unset, empty (the plain ``${VAR:-}``
#: compose form), unparseable, or negative.  ``0`` is *not* a bad value: it
#: is the documented off-switch (``ingest._should_skip_repo`` disables the
#: breaker on ``cb_threshold <= 0``) and is returned as-is -- folding it
#: into the default would turn a working knob into a silent no-op now that
#: this setting really reaches the container (issue #371).
DEFAULT_CB_THRESHOLD: int = 5

#: Default circuit-breaker base backoff in seconds (issue #345). Used when
#: SHIORI_CB_BASE_BACKOFF is unset, empty (the plain ``${VAR:-}`` compose
#: form), unparseable, or non-positive.
DEFAULT_CB_BASE_BACKOFF: float = 60.0


def _cb_base_backoff_from_env() -> float:
    """Return SHIORI_CB_BASE_BACKOFF as a positive float.

    Unset, empty (the plain ``${VAR:-}`` compose form), unparseable, or
    non-positive values fall back to ``DEFAULT_CB_BASE_BACKOFF`` -- the
    process must never crash on a bad value.
    """
    raw = os.environ.get("SHIORI_CB_BASE_BACKOFF", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CB_BASE_BACKOFF
    if value <= 0:
        return DEFAULT_CB_BASE_BACKOFF
    return value


#: Default circuit-breaker backoff cap for the dev lane (issue #345).
#: Used when SHIORI_CB_MAX_BACKOFF is unset, empty (the plain ``${VAR:-}``
#: compose form), unparseable, or non-positive.
DEFAULT_CB_MAX_BACKOFF: float = 3600.0


def _cb_max_backoff_from_env() -> float:
    """Return SHIORI_CB_MAX_BACKOFF as a positive float.

    Unset, empty (the plain ``${VAR:-}`` compose form), unparseable, or
    non-positive values fall back to ``DEFAULT_CB_MAX_BACKOFF`` -- the
    process must never crash on a bad value.
    """
    raw = os.environ.get("SHIORI_CB_MAX_BACKOFF", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CB_MAX_BACKOFF
    if value <= 0:
        return DEFAULT_CB_MAX_BACKOFF
    return value


#: Default circuit-breaker backoff cap for the reference lane (issue #371):
#: 7 days. Reference repos sync once a day, so a cap below the daily
#: cadence (the dev cap of 3600s) could never let the breaker fire there;
#: the day-scale cap does. Used when SHIORI_CB_REF_MAX_BACKOFF is unset,
#: empty (the plain ``${VAR:-}`` compose form), unparseable, or
#: non-positive.
DEFAULT_CB_REF_MAX_BACKOFF: float = 604_800.0


def _cb_ref_max_backoff_from_env() -> float:
    """Return SHIORI_CB_REF_MAX_BACKOFF as a positive float.

    Unset, empty (the plain ``${VAR:-}`` compose form), unparseable, or
    non-positive values fall back to ``DEFAULT_CB_REF_MAX_BACKOFF`` -- the
    process must never crash on a bad value.
    """
    raw = os.environ.get("SHIORI_CB_REF_MAX_BACKOFF", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CB_REF_MAX_BACKOFF
    if value <= 0:
        return DEFAULT_CB_REF_MAX_BACKOFF
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
    # Positive only: with max_chars <= 0 chunking._split_long_text cannot
    # advance (_find_breakpoint returns 0, so the cut never shrinks the
    # remainder -- an infinite loop, not a documented behaviour).
    chunk_max_chars: int = field(
        default_factory=lambda: _int_from_env(
            "SHIORI_CHUNK_MAX_CHARS", 1200, minimum=1
        )
    )
    # Search result defaults (see detailed design/05 and 06 decisions).
    # Positive only: 0 would make every search return no hits (search.py
    # truncates with ranked[:k]) and a negative k would slice from the
    # tail; neither is a documented behaviour.
    default_top_k: int = field(
        default_factory=lambda: _int_from_env("SHIORI_TOP_K", 8, minimum=1)
    )
    # Positive only: search._row_to_hit slices content[:snippet_chars], so
    # 0 turns every snippet into a bare ellipsis and a negative value
    # drops the *end* of the content; neither is a documented behaviour.
    snippet_chars: int = field(
        default_factory=lambda: _int_from_env(
            "SHIORI_SNIPPET_CHARS", 400, minimum=1
        )
    )
    # Pull-type sync debounce interval (seconds). Max seconds between Phase 1
    # (clone refresh) pulls for the same repo. 0 disables (always pull).
    # Formerly the background auto-sync interval; repurposed in #236.
    # 0 is the documented "always pull" value and the default; it must pass
    # through (pipeline._ensure_phase1 clamps the debounce with
    # max(interval, _PHASE1_MIN_DEBOUNCE)). A negative value behaves
    # exactly like 0 there, so falling back to the default (0) on negative
    # input is behaviour-neutral.
    sync_interval_seconds: int = field(
        default_factory=lambda: _int_from_env(
            "SHIORI_SYNC_INTERVAL_SECONDS", 0, minimum=0
        )
    )
    # --- Steady sync (issue #347): EXPECTED host-timer cadence ---
    # These document the cadence of the two host-level systemd user timers
    # (scripts/systemd/) that call the CLI with --only-dev / --only-ref --
    # there is no in-server sync loop reading these values on a schedule.
    # shiori_status uses them only to derive a role-aware staleness
    # threshold (2x the expected interval; see tools/status.py).
    # Any parsed integer passes through (no minimum): the only consumer,
    # tools/status.py, floors the derived staleness threshold at
    # _STALE_SECONDS_FLOOR and otherwise just reports the raw value, so
    # zero/negative are already defended there and their meaning is
    # preserved rather than silently rewritten here.
    dev_sync_interval_seconds: int = field(
        default_factory=lambda: _int_from_env(
            "SHIORI_DEV_SYNC_INTERVAL_SECONDS", 900
        )
    )
    ref_sync_interval_seconds: int = field(
        default_factory=lambda: _int_from_env(
            "SHIORI_REF_SYNC_INTERVAL_SECONDS", 86400
        )
    )
    # MCP server (streamable HTTP)
    mcp_host: str = field(
        default_factory=lambda: os.environ.get("SHIORI_MCP_HOST", "0.0.0.0")
    )
    # Positive only: this is a served endpoint (mcp_server.run() passes it
    # to mcp.run(port=...)), so 0 (bind an ephemeral random port) and
    # negative (bind error) have no useful meaning here.
    mcp_port: int = field(
        default_factory=lambda: _int_from_env("SHIORI_MCP_PORT", 8765, minimum=1)
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
    # Unset/empty/unparseable/non-positive values fall back to the default.
    fetch_concurrency: int = field(
        default_factory=lambda: _int_from_env(
            "SHIORI_FETCH_CONCURRENCY", DEFAULT_FETCH_CONCURRENCY, minimum=1
        )
    )
    # Backfill seed for ref repos (not in SHIORI_DEV_REPOS) whose cursor is None.
    # YYYY-MM-DD format. CLI --backfill-since overrides this for all targets.
    # Dev repos are never seeded by this env var (always full backfill).
    ref_backfill_since: str | None = field(
        default_factory=lambda: os.environ.get("SHIORI_REF_BACKFILL_SINCE") or None
    )
    # --- Circuit breaker: stop retrying a repo after N consecutive failures (issue #345) ---
    # Unset, empty, unparseable, or non-positive values fall back to the
    # built-in defaults (the process must never crash on a bad value; see
    # the *_from_env helpers above).
    # Set SHIORI_CB_THRESHOLD=0 to disable the circuit breaker entirely.
    # minimum=0, NOT 1: zero is the documented off-switch and must pass
    # through (see DEFAULT_CB_THRESHOLD); only negative values fall back.
    cb_threshold: int = field(
        default_factory=lambda: _int_from_env(
            "SHIORI_CB_THRESHOLD", DEFAULT_CB_THRESHOLD, minimum=0
        )
    )
    # Base backoff in seconds. The actual backoff grows exponentially:
    #   min(cap, cb_base_backoff * 2^(failures - 1))
    cb_base_backoff: float = field(default_factory=_cb_base_backoff_from_env)
    # Backoff cap in seconds for the dev lane (repos in SHIORI_DEV_REPOS,
    # ~15-minute cadence): prevents exponential growth from parking a dev
    # repo for days -- a dev repo waits at most one hour.
    cb_max_backoff: float = field(default_factory=_cb_max_backoff_from_env)
    # Backoff cap in seconds for the reference lane (repos in SHIORI_REPOS
    # but NOT in SHIORI_DEV_REPOS, once-a-day cadence). The dev cap is
    # always below the daily cadence, so the breaker could never fire
    # there; the day-scale cap lets it (issue #371).
    cb_ref_max_backoff: float = field(
        default_factory=_cb_ref_max_backoff_from_env
    )
    # --- Rate-limit handling (issue #345) ---
    # Maximum seconds to wait on a rate-limit response (Retry-After or
    # x-ratelimit-reset). A bogus reset value cannot park the process
    # beyond this cap.
    # 0 is meaningful ("never sleep, retry immediately"):
    # github_errors.compute_wait_seconds clamps every wait with
    # min(..., max_wait), and time.sleep(0) is legal. A negative cap would
    # propagate into api_utils' time.sleep() and raise ValueError mid-sync,
    # so only negative values fall back.
    rate_limit_max_wait: float = field(
        default_factory=lambda: _float_from_env(
            "SHIORI_RATE_LIMIT_MAX_WAIT", 60.0, minimum=0.0
        )
    )
    # Maximum number of retries when rate-limited before giving up and
    # recording a failure.
    # 0 is meaningful ("do not retry"): api_utils._api_pages_gen raises
    # RateLimitExhausted once retries (starting at 0) >= max_retries. A
    # negative value would behave identically to 0 but is treated as a
    # misconfiguration and falls back, matching SHIORI_CB_THRESHOLD's
    # non-negative convention.
    rate_limit_max_retries: int = field(
        default_factory=lambda: _int_from_env(
            "SHIORI_RATE_LIMIT_MAX_RETRIES", 3, minimum=0
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
    # 0 is the documented default (serial build) and is injected verbatim
    # into SET max_parallel_maintenance_workers (schema.create_heavy_indexes);
    # it must pass through. Negative is outside the PostgreSQL GUC's range
    # (0..1024), would fail the SET during the index build, and falls back.
    max_parallel_maintenance_workers: int = field(
        default_factory=lambda: _int_from_env(
            "SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS", 0, minimum=0
        )
    )
    # --- Bulk-path trigger (issue #376) ---
    # Pending (not-yet-indexed) items across the targeted repos at or above
    # which the invocation takes the bulk (batched) path. A single COUNT
    # over issue_items decides; nothing is truncated or re-fetched.
    # Unset/empty/unparseable/non-positive values fall back to the default.
    bulk_pending_threshold: int = field(
        default_factory=lambda: _int_from_env(
            "SHIORI_BULK_PENDING_THRESHOLD",
            DEFAULT_BULK_PENDING_THRESHOLD,
            minimum=1,
        )
    )
    # --- Index-run time budget (issue #377) ---
    # Maximum *working* seconds (time.monotonic: machine-suspend time does
    # not count) one index run may spend before stopping at the next safe
    # boundary (between repos, and at the per-repo issue batch boundary).
    # An exhausted budget is a normal outcome: the run stops, logs, records
    # progress per repo, and exits 0 -- the next run resumes where it left
    # off via indexed_at. Unset/empty/unparseable/non-positive means
    # unbounded (the default; nothing changes for anyone who does not set
    # it). Enforced in the CLI index loops (run_index / run_ingest) only.
    ingest_time_budget: float | None = field(
        default_factory=_ingest_time_budget_from_env
    )

    def repo_dir(self, repo: str) -> str:
        owner, name = repo.split("/", 1)
        return os.path.join(self.data_dir, "repos", f"{owner}__{name}")

def load_settings() -> Settings:
    return Settings()
