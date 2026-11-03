"""Acceptance tests for scripts/start_shiori.py (PR469 review correction).

The wrapper's public contract is its main() return code and the service entrypoint.
The chosen test seam imports the script by path, patches its sibling
wait_for_docker.main dependency, and patches subprocess.run at the compose
boundary. The service's main ExecStart is also checked as text. No Docker or
systemd process is started.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
SCRIPT = SCRIPTS / "start_shiori.py"
SERVICE = SCRIPTS / "shiori.service"
COMPOSE_UP = ["/usr/bin/docker", "compose", "up", "-d", "app"]


def _load_wrapper(monkeypatch: pytest.MonkeyPatch) -> Any:
    assert SCRIPT.is_file(), (
        "scripts/start_shiori.py is missing; the main-process startup wrapper "
        "required by the PR469 restart correction has not been implemented"
    )
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "_start_shiori_acceptance", SCRIPT
    )
    assert spec is not None and spec.loader is not None, (
        "scripts/start_shiori.py could not be loaded as a Python entrypoint"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("helper_status", "expected_status"),
    [(2, 2), (1, 1), (7, 1)],
    ids=["configuration-error", "docker-wait-exhausted", "unexpected-helper-error"],
)
def test_helper_failure_propagates_without_running_compose(
    monkeypatch: pytest.MonkeyPatch, helper_status: int, expected_status: int
) -> None:
    wrapper = _load_wrapper(monkeypatch)
    monkeypatch.setattr(wrapper.wait_for_docker, "main", lambda: helper_status)

    def unexpected_compose(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("compose must not run when Docker readiness did not succeed")

    monkeypatch.setattr(wrapper.subprocess, "run", unexpected_compose)

    assert wrapper.main() == expected_status


def test_helper_success_runs_exact_compose_command_and_returns_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper = _load_wrapper(monkeypatch)
    monkeypatch.setattr(wrapper.wait_for_docker, "main", lambda: 0)
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def compose(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(wrapper.subprocess, "run", compose)

    assert wrapper.main() == 0
    assert len(calls) == 1
    assert calls[0][0] == (COMPOSE_UP,)
    assert calls[0][1].get("shell", False) is not True


@pytest.mark.parametrize("compose_status", [1, 2, 19])
def test_any_compose_nonzero_status_is_retryable_failure(
    monkeypatch: pytest.MonkeyPatch, compose_status: int
) -> None:
    wrapper = _load_wrapper(monkeypatch)
    monkeypatch.setattr(wrapper.wait_for_docker, "main", lambda: 0)
    monkeypatch.setattr(
        wrapper.subprocess,
        "run",
        lambda *args, **kwargs: types.SimpleNamespace(returncode=compose_status),
    )

    assert wrapper.main() == 1


def test_compose_execution_oserror_is_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper = _load_wrapper(monkeypatch)
    monkeypatch.setattr(wrapper.wait_for_docker, "main", lambda: 0)

    def missing_docker(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("environment-specific detail")

    monkeypatch.setattr(wrapper.subprocess, "run", missing_docker)

    assert wrapper.main() == 1


def test_compose_inherits_cwd_and_environment_without_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wrapper = _load_wrapper(monkeypatch)
    monkeypatch.setattr(wrapper.wait_for_docker, "main", lambda: 0)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SHIORI_TEST_INHERITED_VALUE", "visible-to-compose")
    observed: dict[str, Any] = {}

    def compose(*args: Any, **kwargs: Any) -> Any:
        observed["argv"] = args[0]
        observed["kwargs"] = kwargs
        observed["cwd"] = os.getcwd()
        observed["env_value"] = os.environ.get("SHIORI_TEST_INHERITED_VALUE")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(wrapper.subprocess, "run", compose)

    assert wrapper.main() == 0
    assert observed["argv"] == COMPOSE_UP
    assert observed["cwd"] == str(tmp_path)
    assert observed["env_value"] == "visible-to-compose"
    assert "cwd" not in observed["kwargs"]
    assert "env" not in observed["kwargs"]
    assert observed["kwargs"].get("shell", False) is not True


def test_entrypoint_runs_under_python3_site_disabled_without_real_compose() -> None:
    assert SCRIPT.is_file(), (
        "scripts/start_shiori.py is missing; the stdlib-only startup entrypoint "
        "has not been implemented"
    )
    child = (
        "import runpy, subprocess, sys, types; "
        "sys.modules['wait_for_docker'] = types.SimpleNamespace(main=lambda: 0); "
        "subprocess.run = lambda *args, **kwargs: "
        "types.SimpleNamespace(returncode=0); "
        "sys.argv = [sys.argv[0], sys.argv[1]]; "
        "runpy.run_path(sys.argv[1], run_name='__main__')"
    )
    proc = subprocess.run(  # noqa: S603 - fixed argv runs the entrypoint under test
        [sys.executable, "-S", "-c", child, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Traceback (most recent call last)" not in proc.stdout + proc.stderr


def test_service_runs_wrapper_as_main_and_marks_exit_two_prevent_restart() -> None:
    service = SERVICE.read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/python3 @SHIORI_DIR@/scripts/start_shiori.py" in service
    assert "RestartPreventExitStatus=2" in service
    assert "ExecStartPre=/usr/bin/python3 @SHIORI_DIR@/scripts/wait_for_docker.py" not in service
    assert "ExecStart=/usr/bin/docker compose up -d app" not in service
