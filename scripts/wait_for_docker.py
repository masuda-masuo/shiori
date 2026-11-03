#!/usr/bin/env python3
"""Wait until the Docker *server* answers (shiori #467).

This host-side helper is imported and called by the main
``ExecStart=/usr/bin/python3 @SHIORI_DIR@/scripts/start_shiori.py`` wrapper.
It exits 1 when the Docker wait budget is exhausted, which systemd retries
through the wrapper, and exits 2 for invalid configuration, which systemd
does not retry through ``RestartPreventExitStatus=2``. That directive applies
only to the main process, not to ``ExecStartPre``, so this helper must remain
behind the main-process wrapper for exit 2 to stop automatic retries.

The socket existing is not enough: the daemon may still be starting up.  The
helper probes the server with ``docker version --format '{{.Server.Version}}'``
and treats the probe as successful only when the CLI exits 0 *and* prints a
non-empty server version.  A missing CLI, a missing socket, an API error and a
query timeout are all retryable within a single monotonic budget.

Defaults (approved in #467):

* total budget: 120 seconds
* per-query timeout: 5 seconds
* retry interval: 2 seconds

The budget, query timeout and retry interval are each clamped to the remaining
budget so a single attempt can never overrun the deadline.  Exhaustion exits
non-zero so systemd can apply ``Restart=on-failure``.  A hung probe is killed
and reaped before the next attempt.

The helper is stdlib-only and never uses a shell.  It inherits the caller's
environment (Docker context/host/config) unchanged.  Docker's stdout/stderr and
the inherited environment are never copied into the journal; only start,
success and exhaustion lines plus a concise failure category are logged.

Test seams (all optional; the defaults above apply when unset):

* ``SHIORI_WAIT_DOCKER_BIN``
* ``SHIORI_WAIT_DOCKER_BUDGET_SECONDS``
* ``SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS``
* ``SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS``

Numeric overrides must be finite positive numbers; invalid configuration
returns exit 2 concisely. Exit 1 means the Docker wait was exhausted and the
main wrapper returns 1 so systemd retries. Exit 2 means bad configuration and
the wrapper returns 2 so the unit's ``RestartPreventExitStatus=2`` prevents a
retry. The directive only applies to the main process, not ``ExecStartPre``.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time

DEFAULT_DOCKER_BIN = "/usr/bin/docker"
DEFAULT_BUDGET_SECONDS = 120.0
DEFAULT_QUERY_TIMEOUT_SECONDS = 5.0
DEFAULT_RETRY_INTERVAL_SECONDS = 2.0

PROBE_ARGS = ("version", "--format", "{{.Server.Version}}")

LOG_PREFIX = "[wait_for_docker]"


class ConfigError(Exception):
    """Raised when an environment override is not a usable number."""


def log(message: str) -> None:
    sys.stderr.write(f"{LOG_PREFIX} {message}\n")
    sys.stderr.flush()


def _env_positive_number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    text = raw.strip()
    if not text:
        raise ConfigError(f"{name} must be a finite positive number (empty value)")
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise ConfigError(
            f"{name} must be a finite positive number (not a number)"
        ) from None
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be a finite positive number (non-finite value)")
    if value <= 0:
        raise ConfigError(f"{name} must be a finite positive number (must be > 0)")
    return value


def _probe(docker_bin: str, timeout: float) -> tuple[bool, str | None]:
    """Run one bounded Docker server probe.

    Returns ``(True, None)`` on success and ``(False, category)`` otherwise.
    The category is a short, non-secret description suitable for the journal.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [docker_bin, *PROBE_ARGS],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return False, "docker CLI missing (ENOENT)"
    except PermissionError:
        return False, "docker CLI not executable (EACCES)"
    except subprocess.TimeoutExpired:
        # subprocess.run() kills and reaps the child on timeout.
        return False, "query timed out"
    except OSError as exc:
        return False, f"docker CLI unavailable ({type(exc).__name__})"

    if proc.returncode != 0:
        return False, f"docker API error (exit {proc.returncode})"
    if not proc.stdout.strip():
        return False, "empty server version"
    return True, None


def main() -> int:
    try:
        budget = _env_positive_number(
            "SHIORI_WAIT_DOCKER_BUDGET_SECONDS", DEFAULT_BUDGET_SECONDS
        )
        query_timeout = _env_positive_number(
            "SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS", DEFAULT_QUERY_TIMEOUT_SECONDS
        )
        retry_interval = _env_positive_number(
            "SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS", DEFAULT_RETRY_INTERVAL_SECONDS
        )
    except ConfigError as exc:
        log(f"invalid configuration: {exc}")
        return 2

    docker_bin = os.environ.get("SHIORI_WAIT_DOCKER_BIN") or DEFAULT_DOCKER_BIN

    log(
        "waiting for Docker server to answer "
        f"(budget {budget:g}s, query timeout {query_timeout:g}s, "
        f"retry {retry_interval:g}s)"
    )

    deadline = time.monotonic() + budget
    last_category: str | None = None
    attempts = 0

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        attempts += 1
        ok, category = _probe(docker_bin, min(query_timeout, remaining))
        if ok:
            log(f"success: Docker server ready after {attempts} attempt(s)")
            return 0

        # Log each distinct failure category once instead of once per retry.
        if category != last_category:
            log(f"Docker not ready: {category}")
            last_category = category

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(retry_interval, remaining))

    detail = last_category if last_category is not None else "no attempt was made"
    log(
        f"exhausted: Docker server did not become ready within {budget:g}s "
        f"(last: {detail})"
    )
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
