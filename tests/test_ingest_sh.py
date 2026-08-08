"""Behavioural tests for scripts/ingest.sh (issue #372).

Runs the real script via subprocess with a stub ``docker`` executable on
PATH, so the required outcomes are proven by execution, not by reading the
script's source:

- every invocation leaves a log file containing what the run printed
  (stdout+stderr), however it was started (outcome 1)
- the journal destination still receives the output -- the script's own
  stdout still carries everything (outcome 2)
- the ingest exit status reaches the caller unchanged (outcome 3)
- retention is counted per lane: each invocation logs into a lane-specific
  directory and pruning touches only that lane (outcome 4)
- two runs that start at the same time write distinct files (outcome 5)
- old logs are pruned automatically (outcome 6)
- the SHIORI_BUILD branch still reaches docker compose unchanged
- the GPU overlay choice (issue #383) follows the contract: SHIORI_GPU=1
  forces the overlay on without probing; any other explicit value forces CPU
  without probing; unset auto-detects (both nvidia-smi -L and the container
  toolkit must succeed). A failed or hanging probe falls back to CPU and the
  run still exits 0. The chosen device and the reason are written to the run
  log file on every path
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INGEST_SH = REPO_ROOT / "scripts" / "ingest.sh"

# The stub records its argv (pipe-separated, one invocation per line) into a
# file named by DOCKER_STUB_ARGS_FILE, prints a line to stdout and one to
# stderr, then exits with DOCKER_STUB_EXIT (default 0).
STUB_DOCKER = """\
#!/usr/bin/env python3
import os
import sys

sys.stdout.write("stub-docker stdout line\\n")
sys.stdout.flush()
sys.stderr.write("stub-docker stderr line\\n")
sys.stderr.flush()
with open(os.environ["DOCKER_STUB_ARGS_FILE"], "a", encoding="utf-8") as f:
    f.write("|".join(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("DOCKER_STUB_EXIT", "0")))
"""

# Stub nvidia-smi. Records every execution into NVIDIA_SMI_MARKER_FILE (so
# tests can prove the probe ran / was skipped) and exits with NVIDIA_SMI_EXIT
# (default 0 = "GPU visible").
STUB_NVIDIA_SMI = """\
#!/usr/bin/env python3
import os
import sys

with open(os.environ.get("NVIDIA_SMI_MARKER_FILE", os.devnull), "a", encoding="utf-8") as f:
    f.write("ran\\n")
sys.exit(int(os.environ.get("NVIDIA_SMI_EXIT", "0")))
"""

# Stub nvidia-smi that hangs (wedged driver): the script's timeout must bound it.
HANGING_NVIDIA_SMI = "#!/usr/bin/env python3\nimport time\ntime.sleep(3600)\n"

# A toolkit binary only needs to exist on PATH -- the probe uses command -v
# and never executes it.
TOOLKIT_STUB = "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"


def write_stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def stub_bindir(env: dict[str, str]) -> Path:
    # the fixture prepends its stub dir to PATH
    return Path(env["PATH"].split(os.pathsep)[0])


def make_gpu_visible(env: dict[str, str]) -> None:
    """Host looks GPU-capable: nvidia-smi works and the toolkit is installed."""
    bindir = stub_bindir(env)
    write_stub(bindir / "nvidia-smi", STUB_NVIDIA_SMI)
    for name in ("nvidia-container-runtime", "nvidia-container-cli", "nvidia-ctk"):
        write_stub(bindir / name, TOOLKIT_STUB)


def probe_marker(env: dict[str, str]) -> Path:
    """nvidia-smi executions are recorded here; absence proves no probing."""
    marker = stub_bindir(env) / "nvidia-smi-marker.txt"
    env["NVIDIA_SMI_MARKER_FILE"] = str(marker)
    return marker


#: Everything scripts/ingest.sh needs to find on PATH. The fixture builds a
#: PATH containing only these (plus the stub dir), so the GPU probe sees the
#: host's real nvidia binaries only when a test puts them there. Without this,
#: the "toolkit absent" case silently becomes "toolkit present" on any machine
#: that has nvidia-container-toolkit installed -- which is exactly the machine
#: this feature exists for (issue #383).
HERMETIC_TOOLS = (
    "bash",        # the shebang is #!/usr/bin/env bash
    "python3",     # the stubs' shebang
    "dirname",
    "tr",
    "mkdir",
    "find",
    "mktemp",
    "tee",
    "timeout",
)


def _hermetic_bindir(tmp_path: Path) -> Path:
    """A PATH directory holding only HERMETIC_TOOLS, symlinked from the host."""
    hermetic = tmp_path / "hermetic-bin"
    hermetic.mkdir()
    for tool in HERMETIC_TOOLS:
        real = shutil.which(tool)
        assert real is not None, f"{tool} not found on PATH; cannot run ingest.sh"
        (hermetic / tool).symlink_to(real)
    return hermetic


@pytest.fixture()
def ingest_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    """Env with a stub docker on PATH; returns (env, log_dir, args_file)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker_stub = bindir / "docker"
    docker_stub.write_text(STUB_DOCKER, encoding="utf-8")
    docker_stub.chmod(0o755)
    logdir = tmp_path / "logs"
    argsfile = tmp_path / "docker-args.txt"
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{_hermetic_bindir(tmp_path)}"
    env["SHIORI_LOG_DIR"] = str(logdir)
    env["DOCKER_STUB_ARGS_FILE"] = str(argsfile)
    env.pop("DOCKER_STUB_EXIT", None)
    env.pop("SHIORI_BUILD", None)
    env.pop("SHIORI_GPU", None)
    return env, logdir, argsfile


def run_ingest(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    # check=False: the caller asserts on the return code (the wrapper's exit
    # status is the contract under test), so a non-zero exit is not an error
    # here.
    return subprocess.run(  # noqa: S603 - integration harness runs the real ingest.sh; its exit code is the contract under test
        [str(INGEST_SH), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def lane_logs(logdir: Path, lane: str) -> list[Path]:
    return sorted((logdir / lane).glob("ingest-*.log"))


def test_script_is_executable() -> None:
    # systemd units run ingest.sh directly, so the exec bit is part of the
    # deployment contract.
    assert os.access(INGEST_SH, os.X_OK)


def test_every_run_leaves_a_log_file_with_stdout_and_stderr(ingest_env: tuple) -> None:
    env, logdir, _ = ingest_env
    result = run_ingest(env, "run", "--only-ref")
    assert result.returncode == 0

    # outcome 1: a log file exists, containing stdout AND stderr of the run
    logs = lane_logs(logdir, "run-only-ref")
    assert len(logs) == 1
    content = logs[0].read_text(encoding="utf-8")
    assert "stub-docker stdout line" in content
    assert "stub-docker stderr line" in content

    # outcome 2: the journal destination still receives the same output
    assert "stub-docker stdout line" in result.stdout
    assert "stub-docker stderr line" in result.stdout
    assert result.stderr == ""


def test_fetch_lane_naming_and_arg_forwarding(ingest_env: tuple) -> None:
    env, logdir, argsfile = ingest_env
    result = run_ingest(env, "fetch", "--repo", "owner/name")
    assert result.returncode == 0

    # slashes and spaces in the arguments must not leak into the path
    logs = lane_logs(logdir, "fetch-repo-owner-name")
    assert len(logs) == 1

    args = argsfile.read_text(encoding="utf-8").splitlines()[0].split("|")
    assert args[0] == "compose"
    assert args[-3:] == ["fetch", "--repo", "owner/name"]


def test_exit_status_reaches_caller_unchanged(ingest_env: tuple) -> None:
    env, logdir, _ = ingest_env
    env["DOCKER_STUB_EXIT"] = "7"
    result = run_ingest(env, "run", "--only-ref")
    # The wrapper must not report the logger's (tee's) status instead of the
    # ingest run's: a failing run makes the systemd unit fail.
    assert result.returncode == 7

    # outcome 1 holds for failing runs too: the evidence survives
    logs = lane_logs(logdir, "run-only-ref")
    assert len(logs) == 1
    content = logs[0].read_text(encoding="utf-8")
    assert "stub-docker stdout line" in content
    assert "stub-docker stderr line" in content
    # and the journal stream still got it
    assert "stub-docker stdout line" in result.stdout


def test_two_immediate_runs_write_distinct_files(ingest_env: tuple) -> None:
    env, logdir, _ = ingest_env
    procs = [
        subprocess.Popen(  # noqa: S603 - integration harness runs the real ingest.sh
            [str(INGEST_SH), "run", "--only-dev"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for p in procs:
        p.communicate(timeout=120)
    assert [p.returncode for p in procs] == [0, 0]

    logs = lane_logs(logdir, "run-only-dev")
    assert len(logs) == 2
    assert logs[0].name != logs[1].name
    for log in logs:
        content = log.read_text(encoding="utf-8")
        # each file holds exactly one run's output, not a merged mess
        assert content.count("stub-docker stdout line") == 1


@pytest.mark.parametrize(
    ("env_extra", "expected_args"),
    [
        ({}, ["compose", "run", "--rm", "ingest", "python", "-m", "shiori", "ingest", "run", "--only-dev"]),
        (
            {"SHIORI_BUILD": "1"},
            ["compose", "run", "--build", "--rm", "ingest", "python", "-m", "shiori", "ingest", "run", "--only-dev"],
        ),
        (
            {"SHIORI_GPU": "1"},
            [
                "compose", "-f", "docker-compose.yml", "-f", "docker-compose.gpu.yml",
                "run", "--rm", "ingest", "python", "-m", "shiori", "ingest", "run", "--only-dev",
            ],
        ),
        (
            {"SHIORI_BUILD": "1", "SHIORI_GPU": "1"},
            [
                "compose", "-f", "docker-compose.yml", "-f", "docker-compose.gpu.yml",
                "run", "--build", "--rm", "ingest", "python", "-m", "shiori", "ingest", "run", "--only-dev",
            ],
        ),
    ],
)
def test_build_and_gpu_flags_reach_docker(
    ingest_env: tuple, env_extra: dict[str, str], expected_args: list[str]
) -> None:
    env, logdir, argsfile = ingest_env
    env.update(env_extra)
    result = run_ingest(env, "run", "--only-dev")
    assert result.returncode == 0

    lines = argsfile.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].split("|") == expected_args

    logs = lane_logs(logdir, "run-only-dev")
    assert len(logs) == 1


# ---- GPU auto-detection (issue #383) ----


def test_autodetect_selects_overlay_when_gpu_visible_and_toolkit_present(
    ingest_env: tuple,
) -> None:
    env, logdir, argsfile = ingest_env
    make_gpu_visible(env)
    result = run_ingest(env, "run", "--only-dev")
    assert result.returncode == 0

    lines = argsfile.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # the probe must not call the docker stub
    assert lines[0].split("|") == [
        "compose", "-f", "docker-compose.yml", "-f", "docker-compose.gpu.yml",
        "run", "--rm", "ingest", "python", "-m", "shiori", "ingest", "run", "--only-dev",
    ]

    # the choice AND the reason reach the run log file, and the journal too
    content = lane_logs(logdir, "run-only-dev")[0].read_text(encoding="utf-8")
    assert 'device=gpu reason="nvidia-smi -L ok and nvidia container toolkit found"' in content
    assert "device=gpu" in result.stdout


def test_autodetect_stays_cpu_when_gpu_probe_fails(ingest_env: tuple) -> None:
    env, logdir, argsfile = ingest_env
    make_gpu_visible(env)
    env["NVIDIA_SMI_EXIT"] = "1"  # toolkit present, but the GPU probe fails
    result = run_ingest(env, "run", "--only-dev")
    assert result.returncode == 0

    lines = argsfile.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    args = lines[0].split("|")
    assert args[0] == "compose" and "-f" not in args

    content = lane_logs(logdir, "run-only-dev")[0].read_text(encoding="utf-8")
    assert "device=cpu" in content
    assert "no GPU visible to host" in content


def test_autodetect_stays_cpu_when_toolkit_absent(ingest_env: tuple) -> None:
    # Relies on the test environment having no nvidia container toolkit on
    # PATH (true in the canonical sandbox, which is CPU-only).
    env, logdir, argsfile = ingest_env
    write_stub(stub_bindir(env) / "nvidia-smi", STUB_NVIDIA_SMI)  # GPU visible...
    # ...but no toolkit binaries: docker could not hand the GPU to a container
    result = run_ingest(env, "run", "--only-dev")
    assert result.returncode == 0

    lines = argsfile.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    args = lines[0].split("|")
    assert args[0] == "compose" and "-f" not in args

    content = lane_logs(logdir, "run-only-dev")[0].read_text(encoding="utf-8")
    assert "device=cpu" in content
    assert "toolkit" in content


def test_explicit_gpu_1_selects_overlay_without_probing(ingest_env: tuple) -> None:
    env, logdir, argsfile = ingest_env
    env["SHIORI_GPU"] = "1"
    marker = probe_marker(env)
    make_gpu_visible(env)  # stubs that would succeed if the probe ran
    result = run_ingest(env, "run", "--only-dev")
    assert result.returncode == 0

    lines = argsfile.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    args = lines[0].split("|")
    assert args[1] == "-f" and "docker-compose.gpu.yml" in args
    # the probe never ran: nvidia-smi was never executed
    assert not marker.exists()

    content = lane_logs(logdir, "run-only-dev")[0].read_text(encoding="utf-8")
    assert 'device=gpu reason="explicit SHIORI_GPU=1"' in content


def test_explicit_cpu_0_stays_cpu_without_probing(ingest_env: tuple) -> None:
    env, logdir, argsfile = ingest_env
    env["SHIORI_GPU"] = "0"
    marker = probe_marker(env)
    make_gpu_visible(env)  # stubs that would succeed if the probe ran
    result = run_ingest(env, "run", "--only-dev")
    assert result.returncode == 0

    lines = argsfile.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    args = lines[0].split("|")
    assert args[0] == "compose" and "-f" not in args
    # the probe never ran: nvidia-smi was never executed
    assert not marker.exists()

    content = lane_logs(logdir, "run-only-dev")[0].read_text(encoding="utf-8")
    assert 'device=cpu reason="explicit SHIORI_GPU=0"' in content


def test_hanging_gpu_probe_stays_cpu_and_exits_zero(ingest_env: tuple) -> None:
    env, logdir, argsfile = ingest_env
    write_stub(stub_bindir(env) / "nvidia-smi", HANGING_NVIDIA_SMI)
    write_stub(stub_bindir(env) / "nvidia-container-runtime", TOOLKIT_STUB)
    result = run_ingest(env, "run", "--only-dev")
    assert result.returncode == 0

    lines = argsfile.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    args = lines[0].split("|")
    assert args[0] == "compose" and "-f" not in args

    content = lane_logs(logdir, "run-only-dev")[0].read_text(encoding="utf-8")
    assert "device=cpu" in content
    assert "no GPU visible to host" in content


def test_old_logs_are_pruned_per_lane(ingest_env: tuple) -> None:
    env, logdir, _ = ingest_env
    old = time.time() - 40 * 24 * 3600
    lane_dir = logdir / "run-only-ref"
    lane_dir.mkdir(parents=True)
    old_log = lane_dir / "ingest-AAAAAA.log"
    old_log.write_text("old", encoding="utf-8")
    os.utime(old_log, (old, old))
    # a foreign file of the same age must survive: pruning only owns
    # ingest-*.log files
    unrelated = lane_dir / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")
    os.utime(unrelated, (old, old))

    result = run_ingest(env, "run", "--only-ref")
    assert result.returncode == 0

    assert not old_log.exists()
    assert unrelated.exists()
    # only the new run's file remains
    logs = lane_logs(logdir, "run-only-ref")
    assert len(logs) == 1


def test_prune_does_not_touch_other_lanes(ingest_env: tuple) -> None:
    """The dev lane's churn must not evict the ref lane's logs."""
    env, logdir, _ = ingest_env
    old = time.time() - 40 * 24 * 3600
    ref_dir = logdir / "run-only-ref"
    ref_dir.mkdir(parents=True)
    old_ref = ref_dir / "ingest-BBBBBB.log"
    old_ref.write_text("old ref", encoding="utf-8")
    os.utime(old_ref, (old, old))

    result = run_ingest(env, "run", "--only-dev")
    assert result.returncode == 0

    assert old_ref.exists()
    assert len(lane_logs(logdir, "run-only-ref")) == 1


def test_fresh_logs_are_not_pruned(ingest_env: tuple) -> None:
    env, logdir, _ = ingest_env
    recent = time.time() - 10 * 24 * 3600
    lane_dir = logdir / "run-only-ref"
    lane_dir.mkdir(parents=True)
    recent_log = lane_dir / "ingest-CCCCCC.log"
    recent_log.write_text("recent", encoding="utf-8")
    os.utime(recent_log, (recent, recent))

    result = run_ingest(env, "run", "--only-ref")
    assert result.returncode == 0

    assert recent_log.exists()
    assert len(lane_logs(logdir, "run-only-ref")) == 2


def test_log_dir_env_override(ingest_env: tuple, tmp_path: Path) -> None:
    env, _, _ = ingest_env
    custom = tmp_path / "custom-logs"
    env["SHIORI_LOG_DIR"] = str(custom)
    result = run_ingest(env, "run", "--only-ref")
    assert result.returncode == 0
    assert len(lane_logs(custom, "run-only-ref")) == 1


def test_invocation_without_arguments_still_leaves_a_log(ingest_env: tuple) -> None:
    env, logdir, _ = ingest_env
    result = run_ingest(env)
    assert result.returncode == 0
    assert len(lane_logs(logdir, "default")) == 1
