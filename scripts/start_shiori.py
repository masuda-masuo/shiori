#!/usr/bin/env python3
"""Start Shiori after Docker is ready, with systemd-aware retry exit codes.

The readiness helper exits 1 when its Docker wait budget is exhausted; systemd
retries that startup. It exits 2 for invalid configuration; the unit marks 2
with RestartPreventExitStatus so systemd does not retry it. This helper must
run as the main ExecStart process because RestartPreventExitStatus does not
apply to ExecStartPre commands.
"""

from __future__ import annotations

import subprocess
import sys

import wait_for_docker

COMPOSE_UP = ["/usr/bin/docker", "compose", "up", "-d", "app"]


def main() -> int:
    """Wait for Docker and start the app, normalizing retryable failures to 1."""
    helper_status = wait_for_docker.main()
    if helper_status == 2:
        return 2
    if helper_status != 0:
        return 1

    try:
        result = subprocess.run(COMPOSE_UP, check=False)  # noqa: S603 - fixed argv, no shell
    except OSError as exc:
        # Do not expose exception text: it may contain local paths or details.
        sys.stderr.write(
            f"[start_shiori] docker compose could not be started "
            f"({type(exc).__name__})\n"
        )
        sys.stderr.flush()
        return 1

    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
