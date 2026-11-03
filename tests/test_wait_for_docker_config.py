"""Focused configuration-validation tests for wait_for_docker.py (shiori #467).

The approved helper must reject invalid numeric overrides concisely instead of
retrying forever.  These tests only exercise the new validation path; the
behavioural acceptance suite lives in ``test_wait_for_docker.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "wait_for_docker.py"


def _run_with(
    overrides: dict[str, str], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("SHIORI_WAIT_DOCKER_"):
            del env[key]
    # The helper must fail on configuration before it ever probes Docker.
    env["SHIORI_WAIT_DOCKER_BIN"] = str(tmp_path / "docker-never-created")
    env.update(overrides)
    return subprocess.run(  # noqa: S603 - fixed argv runs the helper under test
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "abc", ""])
def test_invalid_budget_fails_concisely(tmp_path: Path, value: str) -> None:
    started = time.monotonic()
    proc = _run_with({"SHIORI_WAIT_DOCKER_BUDGET_SECONDS": value}, tmp_path)
    output = proc.stdout + proc.stderr

    assert proc.returncode != 0, output
    assert "SHIORI_WAIT_DOCKER_BUDGET_SECONDS" in output, output
    assert "Traceback (most recent call last)" not in output, output
    assert time.monotonic() - started < 2.0


@pytest.mark.parametrize(
    "name",
    [
        "SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS",
        "SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS",
    ],
)
def test_invalid_query_and_retry_overrides_fail_concisely(
    tmp_path: Path, name: str
) -> None:
    proc = _run_with({name: "not-a-number"}, tmp_path)
    output = proc.stdout + proc.stderr

    assert proc.returncode != 0, output
    assert name in output, output
    assert "Traceback (most recent call last)" not in output, output


@pytest.mark.parametrize(
    ("env_var", "secret_value", "expected_category"),
    [
        (
            "SHIORI_WAIT_DOCKER_BUDGET_SECONDS",
            "ghp_super_secret_token_budget_12345",
            "not a number",
        ),
        (
            "SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS",
            "sk-secret-api-key-timeout-67890",
            "not a number",
        ),
        (
            "SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS",
            "Bearer-secret-retry-token-abcdef",
            "not a number",
        ),
    ],
)
def test_invalid_numeric_overrides_do_not_leak_raw_secrets(
    tmp_path: Path, env_var: str, secret_value: str, expected_category: str
) -> None:
    """Distinctive secret strings in numeric overrides never appear in output."""
    proc = _run_with({env_var: secret_value}, tmp_path)
    output = proc.stdout + proc.stderr

    assert proc.returncode != 0, output
    assert env_var in output, output
    assert expected_category in output, output
    assert secret_value not in output, output
    assert "Traceback (most recent call last)" not in output, output


def test_custom_docker_bin_path_does_not_leak_into_logs(tmp_path: Path) -> None:
    """A secret-looking SHIORI_WAIT_DOCKER_BIN path never appears in helper output."""
    secret_path = tmp_path / "secret-vault-path-XYZ987" / "docker-bin"
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("SHIORI_WAIT_DOCKER_"):
            del env[key]
    env["SHIORI_WAIT_DOCKER_BIN"] = str(secret_path)
    env["SHIORI_WAIT_DOCKER_BUDGET_SECONDS"] = "0.2"
    env["SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS"] = "0.1"
    env["SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS"] = "0.05"

    proc = subprocess.run(  # noqa: S603 - fixed argv runs the helper under test
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    output = proc.stdout + proc.stderr

    assert str(secret_path) not in output, output
    assert "secret-vault-path-XYZ987" not in output, output
    assert "waiting for Docker server to answer" in output, output
    assert "docker CLI missing (ENOENT)" in output, output
    assert "Traceback (most recent call last)" not in output, output

