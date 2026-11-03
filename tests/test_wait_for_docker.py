"""Acceptance tests for scripts/wait_for_docker.py (shiori #467, option A).

The helper is a new stdlib-only host script imported and called by
``scripts/start_shiori.py``, the main-process entrypoint for ``shiori.service``.

It must wait until the Docker *server* answers, not merely until the socket
appears.  The probe is ``docker version --format '{{.Server.Version}}'``;
success requires status 0 AND a non-empty Server.Version.  The approved
contract is a 120 second monotonic total budget, at most 5 seconds per query,
and a 2 second retry interval, each clamped to the remaining budget.  Missing
CLI, missing socket, API failure and query timeout are retryable; exhaustion
exits non-zero.  Logs are start/success/exhaustion lines plus a concise
failure category, never raw Docker output or environment/secrets, and a timed
out child must not be left behind.

Expected public seams for the implementer
-----------------------------------------
The tests run the real script as a subprocess.  To keep the suite fast and
deterministic the helper must honour these optional environment variables
(defaults are the approved values above):

``SHIORI_WAIT_DOCKER_BIN``
    Docker executable to run (default ``/usr/bin/docker``).
``SHIORI_WAIT_DOCKER_BUDGET_SECONDS``
    Total monotonic budget (default ``120``).
``SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS``
    Per-query cap (default ``5``).
``SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS``
    Retry interval (default ``2``).

Fractional values are accepted.  The helper must still inherit the caller's
Docker context/environment unchanged (``DOCKER_HOST``, ``DOCKER_CONTEXT``,
``DOCKER_CONFIG`` and every other variable the CLI needs).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "wait_for_docker.py"
SERVICE = REPO_ROOT / "scripts" / "shiori.service"

#: The exact probe the helper must make.  The format string is passed as one
#: argv element; no shell is involved.
PROBE_ARGS = ["version", "--format", "{{.Server.Version}}"]

#: Phrases that mark a final exhaustion line.  Bare "budget"/"timeout" are
#: excluded so a start line mentioning the configured limits cannot satisfy it.
EXHAUSTION_RE = re.compile(
    r"(?i)(exhaust|deadline|gave up|time limit|timed out|not ready|"
    r"did not become ready)"
)

#: A fake ``docker`` driven by a JSON fixture.  It records every invocation
#: (argv, selected environment, monotonic start time, pid) and can succeed,
#: fail, print arbitrary output, or hang while ignoring SIGTERM.
FAKE_DOCKER_BODY = r'''
import json, os, signal, sys, time
from pathlib import Path

config_path = Path(os.environ["FAKE_DOCKER_CONFIG"])
config = json.loads(config_path.read_text(encoding="utf-8"))
counter_path = Path(config["counter_path"])
try:
    attempt = int(counter_path.read_text(encoding="utf-8").strip())
except (OSError, ValueError):
    attempt = 0
counter_path.write_text(str(attempt + 1), encoding="utf-8")
attempts = config["attempts"]
spec = attempts[min(attempt, len(attempts) - 1)]

record = {
    "argv": sys.argv[1:],
    "pid": os.getpid(),
    "t": time.monotonic(),
    "env": {key: os.environ.get(key) for key in config.get("env_keys", [])},
}
with open(config["calls_path"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\n")

if spec.get("record_pid"):
    with open(config["pid_path"], "a", encoding="utf-8") as handle:
        handle.write(str(os.getpid()) + "\n")

if spec.get("ignore_sigterm"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

if spec.get("stdout"):
    sys.stdout.write(spec["stdout"])
    sys.stdout.flush()
if spec.get("stderr"):
    sys.stderr.write(spec["stderr"])
    sys.stderr.flush()

delay = float(spec.get("sleep", 0))
if delay:
    time.sleep(delay)

raise SystemExit(int(spec.get("exit", 0)))
'''


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _fake_docker_source() -> str:
    return "#!" + sys.executable + "\n" + FAKE_DOCKER_BODY


def _require_script() -> None:
    assert SCRIPT.is_file(), (
        "scripts/wait_for_docker.py is missing; the Docker readiness helper "
        "required by shiori.service (issue #467) has not been implemented"
    )


@dataclass
class HelperRun:
    proc: subprocess.CompletedProcess[str]
    elapsed: float

    @property
    def output(self) -> str:
        return self.proc.stdout + self.proc.stderr


class HelperScene:
    """A fake docker executable plus the fixture files it reads and writes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.docker_bin = root / "docker"
        self.config_path = root / "fake-docker.json"
        self.calls_path = root / "calls.jsonl"
        self.counter_path = root / "attempts.txt"
        self.pid_path = root / "pids.txt"

    def _write_config(self, attempts: list[dict], env_keys: tuple[str, ...]) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "attempts": attempts,
                    "calls_path": str(self.calls_path),
                    "counter_path": str(self.counter_path),
                    "pid_path": str(self.pid_path),
                    "env_keys": list(env_keys),
                }
            ),
            encoding="utf-8",
        )

    def configure(self, attempts: list[dict], env_keys: tuple[str, ...] = ()) -> None:
        self._write_config(attempts, env_keys)
        _write_executable(self.docker_bin, _fake_docker_source())

    def create_fake_later(
        self, attempts: list[dict], delay: float, env_keys: tuple[str, ...] = ()
    ) -> threading.Thread:
        """Create the fake binary only after *delay* seconds (CLI-missing test)."""
        self._write_config(attempts, env_keys)
        body = _fake_docker_source()

        def worker() -> None:
            time.sleep(delay)
            tmp = self.docker_bin.with_name(self.docker_bin.name + ".tmp")
            _write_executable(tmp, body)
            os.replace(tmp, self.docker_bin)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread

    def env(self, **overrides: str) -> dict[str, str]:
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("SHIORI_WAIT_DOCKER_"):
                del env[key]
        # Do not let a host Docker context influence the probe; the tests that
        # care about inheritance set these explicitly.
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"):
            env.pop(key, None)
        env["FAKE_DOCKER_CONFIG"] = str(self.config_path)
        env["SHIORI_WAIT_DOCKER_BIN"] = str(self.docker_bin)
        env.update(overrides)
        return env

    def calls(self) -> list[dict]:
        if not self.calls_path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.calls_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def pids(self) -> list[int]:
        if not self.pid_path.is_file():
            return []
        return [
            int(line)
            for line in self.pid_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def _run_helper(env: dict[str, str], timeout: float = 15.0) -> HelperRun:
    _require_script()
    started = time.monotonic()
    proc = subprocess.run(  # noqa: S603 - fixed argv runs the helper under test
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return HelperRun(proc=proc, elapsed=time.monotonic() - started)


def _assert_start_log(run: HelperRun) -> None:
    assert re.search(r"(?i)(waiting|start|begin)", run.output), run.output


def _assert_success_log(run: HelperRun) -> None:
    assert re.search(r"(?i)(success|ready)", run.output), run.output


def _assert_exhaustion_log(run: HelperRun) -> None:
    assert EXHAUSTION_RE.search(run.output), run.output


def _assert_no_traceback(run: HelperRun) -> None:
    assert "Traceback (most recent call last)" not in run.output, run.output


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _child_pids(pid: int) -> set[int]:
    """Direct children of *pid*, including zombies that were not reaped."""
    children_file = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        return {int(token) for token in children_file.read_text().split()}
    except OSError:
        pass
    children: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        # Field 3 (state) and 4 (ppid) follow the parenthesised comm field.
        rest = stat.rsplit(")", 1)[-1].split()
        if len(rest) >= 2 and rest[0] == "Z" and int(rest[1]) == pid:
            children.add(int(entry.name))
    return children


def _assert_pids_gone(pids: list[int]) -> None:
    assert pids, "the fake docker never recorded a PID"
    deadline = time.monotonic() + 1.0
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if _pid_exists(pid)]
        if remaining:
            time.sleep(0.02)
    assert not remaining, f"hung docker child processes were not reaped: {remaining}"


# ===================================================================
# Probe success / failure semantics
# ===================================================================


def test_success_requires_nonempty_server_version_and_does_not_retry(
    tmp_path: Path,
) -> None:
    """Status 0 plus a non-empty Server.Version is the only success signal.

    The helper must probe with ``docker version --format '{{.Server.Version}}'``
    exactly once, log start/success, and never echo the raw version or Docker's
    stderr.
    """
    scene = HelperScene(tmp_path)
    version = "27.5.1-DISTINCTIVE-SERVER-VERSION"
    raw_stderr = "RAW_SUCCESS_STDERR_MUST_NOT_LEAK"
    scene.configure(
        [{"stdout": version + "\n", "stderr": raw_stderr + "\n", "exit": 0}]
    )
    env = scene.env(
        SHIORI_WAIT_DOCKER_BUDGET_SECONDS="30",
        SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS="1",
        SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS="0.05",
    )

    run = _run_helper(env)

    assert run.proc.returncode == 0, run.output
    calls = scene.calls()
    assert len(calls) == 1, calls
    assert calls[0]["argv"] == PROBE_ARGS, calls[0]["argv"]
    _assert_start_log(run)
    _assert_success_log(run)
    assert version not in run.output
    assert raw_stderr not in run.output
    _assert_no_traceback(run)


def test_helper_is_stdlib_only_runs_with_site_disabled(tmp_path: Path) -> None:
    """The helper must run under ``python3 -S`` (no site-packages)."""
    scene = HelperScene(tmp_path)
    scene.configure([{"stdout": "27.5.1\n", "exit": 0}])
    env = scene.env(
        SHIORI_WAIT_DOCKER_BUDGET_SECONDS="30",
        SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS="1",
        SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS="0.05",
    )
    _require_script()
    started = time.monotonic()
    proc = subprocess.run(  # noqa: S603 - fixed argv runs the helper under test
        [sys.executable, "-S", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert re.search(r"(?i)(success|ready)", output), output
    assert "Traceback (most recent call last)" not in output, output
    assert time.monotonic() - started < 30.0
    assert len(scene.calls()) == 1, scene.calls()


def test_default_bin_is_usr_bin_docker_not_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without an override, the probe uses the absolute default, never PATH."""
    def missing_cli(index: int, timeout: float, clock: "FakeClock") -> tuple[int, str, float, str]:
        raise FileNotFoundError

    result, clock, timeouts, output = _run_controlled(
        monkeypatch,
        capsys,
        {
            "SHIORI_WAIT_DOCKER_BUDGET_SECONDS": "0.3",
            "SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS": "0.1",
            "SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS": "0.05",
        },
        missing_cli,
    )

    assert result == 1
    assert clock.argvs[0] == ["/usr/bin/docker", *PROBE_ARGS]
    assert len(timeouts) > 1
    assert clock.sleeps == pytest.approx([0.05] * len(timeouts))
    assert "missing" in output
    assert "exhausted:" in output
    assert "success:" not in output


@pytest.mark.parametrize("first_stdout", ["", " \n\t\n"])
def test_empty_or_whitespace_output_is_not_success_and_retries(
    tmp_path: Path, first_stdout: str
) -> None:
    """A zero exit status with an empty/whitespace Server.Version is a failure."""
    scene = HelperScene(tmp_path)
    scene.configure(
        [
            {"stdout": first_stdout, "exit": 0},
            {"stdout": "27.5.1\n", "exit": 0},
        ]
    )
    env = scene.env(
        SHIORI_WAIT_DOCKER_BUDGET_SECONDS="30",
        SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS="1",
        SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS="0.05",
    )

    run = _run_helper(env)

    assert run.proc.returncode == 0, run.output
    assert len(scene.calls()) == 2, scene.calls()
    _assert_success_log(run)


def test_nonzero_status_with_version_output_is_not_success_and_retries(
    tmp_path: Path,
) -> None:
    """A non-zero exit is a failure even when stdout looks like a version."""
    scene = HelperScene(tmp_path)
    scene.configure(
        [
            {"stdout": "27.5.1\n", "exit": 1},
            {"stdout": "27.5.1\n", "exit": 0},
        ]
    )
    env = scene.env(
        SHIORI_WAIT_DOCKER_BUDGET_SECONDS="30",
        SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS="1",
        SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS="0.05",
    )

    run = _run_helper(env)

    assert run.proc.returncode == 0, run.output
    assert len(scene.calls()) == 2, scene.calls()
    _assert_success_log(run)


# ===================================================================
# API failure / empty output exhaustion
# ===================================================================


def test_persistent_api_failure_exhausts_budget_without_leaking_raw_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw_stdout = "RAW_DOCKER_STDOUT_MUST_NOT_LEAK"
    raw_stderr = "RAW_DOCKER_STDERR_MUST_NOT_LEAK"
    env_secret = "ENV_SECRET_MUST_NOT_LEAK"

    def fail_probe(index: int, timeout: float, clock: "FakeClock") -> tuple[int, str, float, str]:
        return 1, raw_stdout, 0.25, raw_stderr

    result, clock, timeouts, output = _run_controlled(
        monkeypatch,
        capsys,
        {
            "SHIORI_WAIT_DOCKER_BUDGET_SECONDS": "3.5",
            "SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS": "1",
            "SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS": "1",
            "SHIORI_TEST_ENV_SECRET": env_secret,
        },
        fail_probe,
    )

    assert result == 1
    assert timeouts == [1.0, 1.0, 1.0]
    assert clock.sleeps == [1.0, 1.0, 0.75]
    assert clock.now == 3.5
    _assert_exhaustion_log(HelperRun(subprocess.CompletedProcess([], result, "", output), 0))
    assert raw_stdout not in output
    assert raw_stderr not in output
    assert env_secret not in output
    _assert_no_traceback(HelperRun(subprocess.CompletedProcess([], result, "", output), 0))
    assert len([line for line in output.splitlines() if line.strip()]) <= 12


def test_persistent_empty_output_exhausts_budget_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def empty_probe(index: int, timeout: float, clock: "FakeClock") -> tuple[int, str, float, str]:
        return 0, "", 0.1, ""

    result, clock, timeouts, output = _run_controlled(
        monkeypatch,
        capsys,
        {
            "SHIORI_WAIT_DOCKER_BUDGET_SECONDS": "1",
            "SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS": "0.4",
            "SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS": "0.2",
        },
        empty_probe,
    )

    assert result == 1
    assert timeouts == pytest.approx([0.4, 0.4, 0.4, 0.1])
    assert clock.sleeps == pytest.approx([0.2, 0.2, 0.2])
    assert clock.now == pytest.approx(1)
    assert "empty server version" in output
    assert "exhausted:" in output
    assert "success:" not in output


def test_absent_socket_error_is_retried_and_recovers(tmp_path: Path) -> None:
    """A missing Docker socket is a retryable API failure, not an immediate exit."""
    scene = HelperScene(tmp_path)
    raw_socket_error = "RAW_SOCKET_ERROR_MUST_NOT_LEAK"
    scene.configure(
        [
            {
                "stderr": (
                    "Cannot connect to the Docker daemon at "
                    "unix:///var/run/docker.sock. " + raw_socket_error + "\n"
                ),
                "exit": 1,
            },
            {"stdout": "27.5.1\n", "exit": 0},
        ]
    )
    env = scene.env(
        SHIORI_WAIT_DOCKER_BUDGET_SECONDS="30",
        SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS="1",
        SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS="0.05",
    )

    run = _run_helper(env)

    assert run.proc.returncode == 0, run.output
    assert len(scene.calls()) == 2, scene.calls()
    assert raw_socket_error not in run.output
    _assert_success_log(run)


# ===================================================================
# Missing CLI recovery / exhaustion
# ===================================================================


def test_missing_cli_is_retried_and_recovers_when_it_appears(tmp_path: Path) -> None:
    """An absent docker executable is retryable within the same budget."""
    scene = HelperScene(tmp_path)
    thread = scene.create_fake_later(
        [{"stdout": "27.5.1\n", "exit": 0}], delay=0.2
    )
    env = scene.env(
        SHIORI_WAIT_DOCKER_BUDGET_SECONDS="30",
        SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS="1",
        SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS="0.1",
    )

    run = _run_helper(env)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert run.proc.returncode == 0, run.output
    assert len(scene.calls()) >= 1, scene.calls()
    _assert_success_log(run)


def test_missing_cli_exhausts_budget_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing CLI is retried at the controlled interval until deadline."""
    def missing_cli(index: int, timeout: float, clock: "FakeClock") -> tuple[int, str, float, str]:
        raise FileNotFoundError

    result, clock, timeouts, output = _run_controlled(
        monkeypatch,
        capsys,
        {
            "SHIORI_WAIT_DOCKER_BUDGET_SECONDS": "0.3",
            "SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS": "0.1",
            "SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS": "0.05",
        },
        missing_cli,
    )

    assert result == 1
    assert len(timeouts) > 1
    assert clock.sleeps == pytest.approx([0.05] * (len(timeouts) - 1) + [0.05])
    assert "docker CLI missing" in output
    assert "exhausted:" in output
    assert "Traceback (most recent call last)" not in output


def test_query_timeout_is_retried_and_recovers(tmp_path: Path) -> None:
    """A genuinely hung child is killed, reaped, and followed by recovery."""
    scene = HelperScene(tmp_path)
    scene.configure(
        [
            {"sleep": 60, "ignore_sigterm": True, "record_pid": True},
            {"stdout": "27.5.1\n", "exit": 0},
        ]
    )
    env = scene.env(
        SHIORI_WAIT_DOCKER_BUDGET_SECONDS="30",
        SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS="5",
        SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS="0.2",
    )

    run = _run_helper(env, timeout=45)

    assert run.proc.returncode == 0, run.output
    assert len(scene.calls()) == 2, scene.calls()
    _assert_success_log(run)
    _assert_pids_gone(scene.pids())


def test_default_query_timeout_is_five_seconds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Default budget, query timeout, and retry interval are exact clock contracts."""
    spec = importlib.util.spec_from_file_location("wait_for_docker_defaults", SCRIPT)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    assert helper.DEFAULT_BUDGET_SECONDS == 120.0
    assert helper.DEFAULT_QUERY_TIMEOUT_SECONDS == 5.0
    assert helper.DEFAULT_RETRY_INTERVAL_SECONDS == 2.0

    def recover(index: int, timeout: float, clock: "FakeClock") -> tuple[int, str, float, str]:
        return (1, "", 0.0, "") if index == 0 else (0, "27.5.1", 0.0, "")

    result, clock, timeouts, output = _run_controlled(
        monkeypatch, capsys, {}, recover
    )

    assert result == 0, output
    assert "budget 120s, query timeout 5s, retry 2s" in output
    assert timeouts == [5.0, 5.0]
    assert clock.sleeps == [2.0]
    assert clock.now == 2.0


def test_query_timeout_exhausts_budget_and_reaps_hung_child(tmp_path: Path) -> None:
    """A real ignored-SIGTERM child is reaped while the helper remains active."""
    scene = HelperScene(tmp_path)
    scene.configure([{"sleep": 60, "ignore_sigterm": True, "record_pid": True}])
    env = scene.env(
        SHIORI_WAIT_DOCKER_BUDGET_SECONDS="15",
        SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS="5",
        SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS="0.2",
    )
    _require_script()
    proc = subprocess.Popen(  # noqa: S603 - fixed argv runs the helper under test
        [sys.executable, str(SCRIPT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out = ""
    err = ""
    remaining: list[int] = []
    try:
        readiness_deadline = time.monotonic() + 10
        while time.monotonic() < readiness_deadline and not scene.pids():
            time.sleep(0.05)
        pids = scene.pids()
        assert pids, "the fake docker never recorded a PID"
        first_pid = pids[0]

        reap_deadline = time.monotonic() + 10
        while time.monotonic() < reap_deadline and first_pid in _child_pids(proc.pid):
            assert proc.poll() is None, "helper exited before the reaping check"
            time.sleep(0.05)
        assert proc.poll() is None, "helper exited before the reaping check"
        assert first_pid not in _child_pids(proc.pid), (
            f"child {first_pid} was killed but not reaped (still a child)"
        )

        out, err = proc.communicate(timeout=20)
        remaining = [pid for pid in scene.pids() if _pid_exists(pid)]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        for pid in scene.pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    combined = out + err
    assert proc.returncode != 0, combined
    assert EXHAUSTION_RE.search(combined), combined
    assert re.search(r"(?i)(timeout|timed out|exceeded|hung)", combined), combined
    assert "Traceback (most recent call last)" not in combined, combined
    assert remaining == [], f"helper left fake docker processes running: {remaining}"
    _assert_pids_gone(scene.pids())


# ===================================================================
# Remaining-budget clamping
# ===================================================================


def test_remaining_budget_clamps_query_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def timeout_probe(index: int, timeout: float, clock: "FakeClock") -> tuple[int, str, float, str]:
        clock.advance(timeout)
        raise subprocess.TimeoutExpired(["docker"], timeout)

    result, clock, timeouts, output = _run_controlled(
        monkeypatch,
        capsys,
        {
            "SHIORI_WAIT_DOCKER_BUDGET_SECONDS": "3",
            "SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS": "5",
            "SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS": "4",
        },
        timeout_probe,
    )

    assert result == 1
    assert timeouts == [3.0]
    assert clock.sleeps == []
    assert clock.now == 3
    assert "query timed out" in output
    assert "exhausted:" in output


def test_remaining_budget_clamps_retry_sleep(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_probe(index: int, timeout: float, clock: "FakeClock") -> tuple[int, str, float, str]:
        return 1, "", 0.25, ""

    result, clock, timeouts, output = _run_controlled(
        monkeypatch,
        capsys,
        {
            "SHIORI_WAIT_DOCKER_BUDGET_SECONDS": "1",
            "SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS": "5",
            "SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS": "4",
        },
        fail_probe,
    )

    assert result == 1
    assert timeouts == [1.0]
    assert clock.sleeps == [0.75]
    assert clock.now == 1
    assert "exhausted:" in output


def test_default_retry_interval_is_two_seconds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_probe(index: int, timeout: float, clock: "FakeClock") -> tuple[int, str, float, str]:
        return 1, "", 0.0, ""

    result, clock, timeouts, output = _run_controlled(
        monkeypatch,
        capsys,
        {
            "SHIORI_WAIT_DOCKER_BUDGET_SECONDS": "5",
            "SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS": "1",
        },
        fail_probe,
    )

    assert result == 1
    assert timeouts == [1.0] * 3
    assert clock.sleeps == [2.0, 2.0, 1.0]
    assert clock.now == 5
    assert "retry 2s" in output


# ===================================================================
# Inherited Docker context / environment
# ===================================================================


def test_inherited_docker_environment_is_passed_through(tmp_path: Path) -> None:
    """The probe must see the caller's Docker context and environment unchanged."""
    scene = HelperScene(tmp_path)
    keys = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
    scene.configure([{"stdout": "27.5.1\n", "exit": 0}], env_keys=keys)
    expected = {
        "DOCKER_HOST": "unix:///tmp/shiori-test-docker.sock",
        "DOCKER_CONTEXT": "shiori-test-context",
        "DOCKER_CONFIG": "/tmp/shiori-test-docker-config",
    }
    env = scene.env(
        SHIORI_WAIT_DOCKER_BUDGET_SECONDS="30",
        SHIORI_WAIT_DOCKER_QUERY_TIMEOUT_SECONDS="1",
        SHIORI_WAIT_DOCKER_RETRY_INTERVAL_SECONDS="0.05",
        **expected,
    )

    run = _run_helper(env)

    assert run.proc.returncode == 0, run.output
    calls = scene.calls()
    assert len(calls) == 1, calls
    assert calls[0]["env"] == expected, calls[0]["env"]


# ===================================================================
# Unit configuration (option A)
# ===================================================================


def _parse_systemd_sections(text: str) -> dict[str, dict[str, list[str]]]:
    sections: dict[str, dict[str, list[str]]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        sections[current].setdefault(key.strip(), []).append(value.strip())
    return sections


def test_unit_config_uses_option_a_and_the_helper() -> None:
    """The unit owns the wait, retries on failure, and has no start-rate cap.

    Option A: ``StartLimitIntervalSec=0`` and no ``StartLimitBurst`` directive.
    Existing compose operations and ``Type=oneshot``/``RemainAfterExit`` stay.
    """
    text = SERVICE.read_text(encoding="utf-8")
    sections = _parse_systemd_sections(text)
    unit = sections["Unit"]
    service = sections["Service"]

    assert unit.get("StartLimitIntervalSec") == ["0"], unit
    all_directives = {key for section in sections.values() for key in section}
    assert "StartLimitBurst" not in all_directives, all_directives

    assert service.get("Type") == ["oneshot"], service
    assert service.get("RemainAfterExit") == ["yes"], service
    assert service.get("WorkingDirectory") == ["@SHIORI_DIR@"], service
    assert service.get("ExecStartPre") is None, service
    assert service.get("ExecStart") == [
        "/usr/bin/python3 @SHIORI_DIR@/scripts/start_shiori.py"
    ], service
    assert service.get("RestartPreventExitStatus") == ["2"], service
    assert service.get("ExecStop") == ["/usr/bin/docker compose stop app"], service
    assert service.get("ExecReload") == ["/usr/bin/docker compose restart app"], service
    assert service.get("Restart") == ["on-failure"], service
    assert service.get("RestartSec") == ["30s"], service
    assert service.get("TimeoutStartSec") == ["5min"], service

    assert SCRIPT.is_file(), (
        "scripts/wait_for_docker.py is missing; the startup wrapper imports it"
    )
    assert (REPO_ROOT / "scripts" / "start_shiori.py").is_file(), (
        "scripts/start_shiori.py is missing; the unit runs it as its main process"
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.argvs: list[list[str]] = []

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


def _run_controlled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, str],
    behavior,
) -> tuple[int, FakeClock, list[float], str]:
    """Run helper logic with a fake monotonic clock and scripted probe."""
    for key in tuple(os.environ):
        if key.startswith("SHIORI_WAIT_DOCKER_") or key == "SHIORI_TEST_ENV_SECRET":
            monkeypatch.delenv(key, raising=False)
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)

    spec = importlib.util.spec_from_file_location("wait_for_docker_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    clock = FakeClock()
    timeouts: list[float] = []

    def run_probe(argv, *, stdout, stderr, text, timeout, check):
        index = len(timeouts)
        clock.argvs.append(list(argv))
        timeouts.append(timeout)
        result = behavior(index, timeout, clock)
        if isinstance(result, BaseException):
            raise result
        clock.advance(result[2])
        return subprocess.CompletedProcess(argv, result[0], result[1], result[3])

    helper.time = type("FakeTime", (), {"monotonic": clock.monotonic, "sleep": clock.sleep})()
    helper.subprocess = type(
        "FakeSubprocess",
        (),
        {
            "PIPE": subprocess.PIPE,
            "TimeoutExpired": subprocess.TimeoutExpired,
            "run": staticmethod(run_probe),
        },
    )()

    result = helper.main()
    output = capsys.readouterr().err
    return result, clock, timeouts, output
