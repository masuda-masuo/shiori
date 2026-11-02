"""Acceptance tests for scripts/reconcile_mint_mount.py (shiori #461).

Six behavioural scenarios drive the real CLI as a subprocess with one fake
``docker``/``systemctl`` executable on PATH.  The fake is a symlink to a
single Python script configured through a JSON fixture; host directories and
Unix sockets in the tests are real, so ownership, ``S_ISSOCK`` and inode
behaviour are not mocked.

Coverage: (1) initial ready run recreates, then unchanged is a no-op;
(2) changed source directory recreates once; (3) socket-only replacement does
not recreate; (4) incomplete preconditions never recreate; (5) a failed
recreate does not advance success and suppresses immediate retry; (6)
``--dry-run`` mutates neither Docker nor successful state; (7) generation
changed mid-command aborts commit; (8) failed postcheck does not advance
success; (9) external container ID change adopts without recreation; (10) traversal
rejected; (11) Docker Desktop opaque VM path fails closed; (12) unrelated /
ambiguous Desktop path rejected; (13) cooldown keyed on filesystem generation
independent of container ID; (14) cooldown expiry enables eventual retry;
(15) wrong owner mount source rejected; (16) root-owned missing source poisoning
rejected without destructive mutation; (17) wrong owner socket rejected;
(18) terse logging without secret payloads; (19) custom nested socket paths;
(20) lock contention exits cleanly; (21) 0600/0700 permissions; (22) unit and timer wiring;
(23) dry-run adoption mutates neither Docker nor successful state; (24) adoption
not blocked by prior cooldown.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "reconcile_mint_mount.py"

FAKE_BODY = r'''
import json, os, sys
from pathlib import Path

path = Path(os.environ["RECONCILE_FIXTURE"])
prog = Path(sys.argv[0]).name
args = sys.argv[1:]
fixture = json.loads(path.read_text())
fixture.setdefault("calls", []).append(
    {"prog": prog, "args": args, "cwd": os.getcwd()}
)

def save():
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(fixture))
    os.replace(tmp, path)

save()

if prog == "systemctl":
    active = fixture.get("systemd_active", True)
    raise SystemExit(0 if "is-active" not in args or active else 3)

if prog != "docker":
    raise SystemExit(0)

if args and args[0] == "inspect":
    target = args[-1]
    running = target in fixture.get("container_ids", [])
    if target == "c_stopped":
        running = False
    mount_src = fixture.get("desktop_mount_source") or fixture["mount_source"]
    print(json.dumps([{
        "Id": target,
        "State": {"Running": running},
        "Mounts": [{
            "Type": "bind",
            "Source": mount_src,
            "Destination": "/run/shiori",
            "RW": True,
        }],
    }]))
    raise SystemExit(0)

if not args or args[0] != "compose":
    raise SystemExit(0)

if "config" in args:
    print(json.dumps({
        "name": "shiori-test",
        "services": {"app": {
            "environment": {"GITHUB_TOKEN_SOCKET": fixture.get("token_socket", "")},
            "volumes": [
                {"type": "bind", "source": fixture["mount_source"],
                 "target": "/run/shiori", "read_only": False},
                {"type": "bind", "source": "/tmp/shiori-data",
                 "target": "/data", "read_only": False},
            ],
        }},
    }))
    raise SystemExit(0)

if "ps" in args:
    ids = fixture.get("container_ids", [])
    if ids:
        print("\n".join(ids))
    raise SystemExit(0)

if "up" in args:
    mutate = fixture.get("mutate_during_up")
    if mutate:
        p = Path(mutate)
        if p.is_socket() or p.is_file():
            p.unlink()
        elif p.is_dir():
            preserved = p.with_name(p.name + "-midup")
            p.rename(preserved)
            p.mkdir()
            make_sock = p / "mint.sock"
            import socket as s
            sock = s.socket(s.AF_UNIX, s.SOCK_STREAM)
            try:
                sock.bind(str(make_sock))
            finally:
                sock.close()
    attempts = sum(
        1 for c in fixture["calls"]
        if c["prog"] == "docker" and "up" in c["args"]
    )
    code = int(fixture.get("recreate_exit", 0))
    if code == 0:
        if fixture.get("postcheck_not_running"):
            fixture["container_ids"] = ["c_stopped"]
        else:
            fixture["container_ids"] = ["c" + str(attempts + 1)]
        save()
    raise SystemExit(code)

raise SystemExit(0)
'''


def make_socket(path: Path) -> None:
    """Create a real AF_UNIX socket file at *path* (and leave it there)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
    finally:
        sock.close()
    assert stat.S_ISSOCK(path.stat().st_mode), path


class Scene:
    """A compose project, a real host mount source/socket, and the fake CLI."""

    def __init__(self, root: Path) -> None:
        self.project = root / "project"
        self.project.mkdir()
        (self.project / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: shiori-test\n", encoding="utf-8"
        )

        self.source = root / "source"
        self.source.mkdir()
        self.socket_rel = "mint.sock"
        self.socket_path = self.source / self.socket_rel
        make_socket(self.socket_path)

        self.state_path = root / "state" / "mint-mount.json"
        self.fixture_path = root / "fixture.json"
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        fake = self.bin_dir / "fake_cmd.py"
        fake.write_text("#!" + sys.executable + "\n" + FAKE_BODY, encoding="utf-8")
        fake.chmod(0o755)
        (self.bin_dir / "docker").symlink_to(fake)
        (self.bin_dir / "systemctl").symlink_to(fake)

        home = root / "home"
        home.mkdir()
        self.env = os.environ.copy()
        for key in (
            "GITHUB_TOKEN",
            "GITHUB_TOKEN_COMMAND",
            "GITHUB_TOKEN_SOCKET",
            "SHIORI_MINT_SOCKET_DIR",
            "XDG_STATE_HOME",
        ):
            self.env.pop(key, None)
        # Only the fake commands are visible: no host Docker/systemctl can run.
        self.env["PATH"] = str(self.bin_dir)
        self.env["HOME"] = str(home)
        self.env["RECONCILE_FIXTURE"] = str(self.fixture_path)
        self.env["GITHUB_TOKEN_SOCKET"] = "/run/shiori/" + self.socket_rel

        self.fixture_path.write_text(
            json.dumps(
                {
                    "systemd_active": True,
                    "container_ids": ["c1"],
                    "mount_source": str(self.source),
                    "token_socket": "/run/shiori/" + self.socket_rel,
                    "recreate_exit": 0,
                    "calls": [],
                }
            ),
            encoding="utf-8",
        )

    def load(self) -> dict:
        return json.loads(self.fixture_path.read_text(encoding="utf-8"))

    def update(self, **changes) -> None:
        data = self.load()
        data.update(changes)
        self.fixture_path.write_text(json.dumps(data), encoding="utf-8")

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv invokes the CLI under test
            [sys.executable, str(SCRIPT), "--state-file", str(self.state_path), *args],
            cwd=self.project,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    @property
    def calls(self) -> list[dict]:
        return self.load().get("calls", [])

    @property
    def recreate_calls(self) -> list[dict]:
        return [
            c for c in self.calls
            if c["prog"] == "docker" and "compose" in c["args"] and "up" in c["args"]
        ]

    @property
    def mutating_calls(self) -> list[dict]:
        return [
            c for c in self.calls
            if c["prog"] == "docker"
            and any(w in c["args"] for w in ("up", "start", "restart", "stop"))
        ]

    def assert_recreate_contract(self, call: dict) -> None:
        args = call["args"]
        assert args[0] == "compose", args
        assert "up" in args, args
        assert "-d" in args, args
        assert "--force-recreate" in args, args
        assert "--no-deps" in args, args
        assert "app" in args, args
        assert not ({"db", "ingest"} & set(args)), args
        assert Path(call["cwd"]).resolve() == self.project.resolve(), call["cwd"]


def test_initial_ready_run_recreates_then_unchanged_run_is_noop(tmp_path: Path) -> None:
    scene = Scene(tmp_path)

    first = scene.run()
    assert first.returncode == 0, first.stderr
    assert len(scene.recreate_calls) == 1
    scene.assert_recreate_contract(scene.recreate_calls[0])
    assert scene.state_path.is_file()

    second = scene.run()
    assert second.returncode == 0, second.stderr
    assert len(scene.recreate_calls) == 1
    assert len(scene.mutating_calls) == 1


def test_changed_directory_recreates_once(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    first = scene.run()
    assert first.returncode == 0, first.stderr
    assert len(scene.recreate_calls) == 1

    # Preserve old directory elsewhere so its inode is not reused; recreate
    # directory and socket at SAME original mount path.
    old = scene.source
    preserved = tmp_path / "source-preserved"
    old.rename(preserved)
    old.mkdir()
    make_socket(scene.socket_path)

    assert preserved.is_dir() and old.is_dir()
    assert preserved.stat().st_ino != old.stat().st_ino

    second = scene.run()
    assert second.returncode == 0, second.stderr
    assert len(scene.recreate_calls) == 2
    scene.assert_recreate_contract(scene.recreate_calls[1])

    third = scene.run()
    assert third.returncode == 0, third.stderr
    assert len(scene.recreate_calls) == 2
    assert len(scene.mutating_calls) == 2


def test_socket_only_replacement_does_not_recreate(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    first = scene.run()
    assert first.returncode == 0, first.stderr
    assert len(scene.recreate_calls) == 1

    old_inode = scene.socket_path.stat().st_ino
    replacement = scene.source / "replacement.sock"
    make_socket(replacement)
    os.replace(replacement, scene.socket_path)
    assert stat.S_ISSOCK(scene.socket_path.stat().st_mode)
    assert scene.socket_path.stat().st_ino != old_inode

    second = scene.run()
    assert second.returncode == 0, second.stderr
    assert len(scene.recreate_calls) == 1
    assert len(scene.mutating_calls) == 1


@pytest.mark.parametrize(
    "scenario",
    [
        "missing_source",
        "missing_socket",
        "inactive_systemd",
        "inactive_container",
        "non_socket_config",
    ],
)
def test_incomplete_preconditions_never_recreate(tmp_path: Path, scenario: str) -> None:
    scene = Scene(tmp_path)
    if scenario == "missing_source":
        scene.update(mount_source=str(tmp_path / "missing-source"))
    elif scenario == "missing_socket":
        scene.socket_path.unlink()
    elif scenario == "inactive_systemd":
        scene.update(systemd_active=False)
    elif scenario == "inactive_container":
        scene.update(container_ids=[])
    elif scenario == "non_socket_config":
        scene.env.pop("GITHUB_TOKEN_SOCKET", None)
        scene.update(token_socket="")
    else:  # pragma: no cover - guards a typo in the parameter list
        raise AssertionError(scenario)

    proc = scene.run()
    assert proc.returncode == 0, proc.stderr
    assert scene.mutating_calls == []


def test_failed_recreate_keeps_prior_success_and_suppresses_immediate_retry(
    tmp_path: Path,
) -> None:
    scene = Scene(tmp_path)
    first = scene.run()
    assert first.returncode == 0, first.stderr
    assert len(scene.recreate_calls) == 1

    old = scene.source
    preserved = tmp_path / "source-preserved"
    old.rename(preserved)
    old.mkdir()
    make_socket(scene.socket_path)
    scene.update(recreate_exit=1)

    scene.run()
    assert len(scene.recreate_calls) == 2
    assert len(scene.mutating_calls) == 2

    # Immediate retry for the same attempted generation is suppressed.
    scene.run()
    assert len(scene.recreate_calls) == 2
    assert len(scene.mutating_calls) == 2

    # Restore the original absolute path and inode. If the failed attempt had
    # advanced successful state, this run would recreate again.
    scene.socket_path.unlink()
    old.rmdir()
    preserved.rename(old)
    scene.update(recreate_exit=0)
    restored = scene.run()
    assert restored.returncode == 0, restored.stderr
    assert len(scene.recreate_calls) == 2
    assert len(scene.mutating_calls) == 2


def test_dry_run_does_not_mutate_docker_or_success_state(tmp_path: Path) -> None:
    scene = Scene(tmp_path)

    dry = scene.run("--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert scene.mutating_calls == []

    # A real run must still recreate: dry-run must not have recorded success.
    real = scene.run()
    assert real.returncode == 0, real.stderr
    assert len(scene.recreate_calls) == 1
    assert len(scene.mutating_calls) == 1


def test_generation_changed_during_command_does_not_commit_success(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    first = scene.run()
    assert first.returncode == 0, first.stderr
    assert len(scene.recreate_calls) == 1

    # Inode changes initially to trigger recreate
    old = scene.source
    preserved = tmp_path / "source-preserved"
    old.rename(preserved)
    old.mkdir()
    make_socket(scene.socket_path)

    # During up, mutate source directory so postcheck detects mid-command change
    scene.update(mutate_during_up=str(scene.source))
    run_mid = scene.run()
    assert run_mid.returncode != 0
    assert len(scene.recreate_calls) == 2

    # Postcheck failed, so success state was not advanced.
    state = json.loads(scene.state_path.read_text(encoding="utf-8"))
    assert state.get("success", {}).get("ino") == preserved.stat().st_ino


def test_failed_postcheck_container_not_running_does_not_commit_success(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    first = scene.run()
    assert first.returncode == 0, first.stderr
    assert len(scene.recreate_calls) == 1

    # Inode changes to trigger recreate
    old = scene.source
    preserved = tmp_path / "source-preserved"
    old.rename(preserved)
    old.mkdir()
    make_socket(scene.socket_path)

    # docker compose up exits 0, but inspect postcheck reports container not running
    scene.update(postcheck_not_running=True)
    failed_post = scene.run()
    assert failed_post.returncode != 0

    state = json.loads(scene.state_path.read_text(encoding="utf-8"))
    assert state.get("success", {}).get("ino") == preserved.stat().st_ino


def test_container_id_change_adopts_without_recreate(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    first = scene.run()
    assert first.returncode == 0, first.stderr
    assert len(scene.recreate_calls) == 1
    assert len(scene.mutating_calls) == 1

    first_state = json.loads(scene.state_path.read_text(encoding="utf-8"))
    first_success = first_state.get("success", {})
    assert first_success.get("container_id") == "c2"

    # Container ID externally changed (e.g. update.sh docker compose up -d app)
    scene.update(container_ids=["c_external"])
    second = scene.run()
    assert second.returncode == 0, second.stderr
    assert "Adopted existing running container c_external" in second.stderr
    assert "(previous container: c2)" in second.stderr
    # Must NOT have made additional mutating Docker calls
    assert len(scene.recreate_calls) == 1
    assert len(scene.mutating_calls) == 1

    # success.container_id updated to new ID, generation fields unchanged
    second_state = json.loads(scene.state_path.read_text(encoding="utf-8"))
    second_success = second_state.get("success", {})
    assert second_success.get("container_id") == "c_external"
    assert second_success.get("source_path") == first_success.get("source_path")
    assert second_success.get("dev") == first_success.get("dev")
    assert second_success.get("ino") == first_success.get("ino")

    # Next tick is a complete NOOP
    third = scene.run()
    assert third.returncode == 0, third.stderr
    assert len(scene.recreate_calls) == 1
    assert len(scene.mutating_calls) == 1


def test_dry_run_adoption_does_not_mutate_state_or_docker(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    first = scene.run()
    assert first.returncode == 0, first.stderr
    assert len(scene.recreate_calls) == 1
    assert len(scene.mutating_calls) == 1

    state_before = scene.state_path.read_text(encoding="utf-8")
    parsed_before = json.loads(state_before)
    assert parsed_before.get("success", {}).get("container_id") == "c2"

    # Container ID externally changed
    scene.update(container_ids=["c_external"])

    # Dry run should report would-adopt and not mutate Docker or state file
    dry = scene.run("--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "would adopt" in dry.stderr.lower()
    assert "(previous container: c2)" in dry.stderr
    assert len(scene.recreate_calls) == 1
    assert len(scene.mutating_calls) == 1

    state_after = scene.state_path.read_text(encoding="utf-8")
    assert state_after == state_before

    # Subsequent real run adopts successfully
    real = scene.run()
    assert real.returncode == 0, real.stderr
    assert "(previous container: c2)" in real.stderr
    assert len(scene.recreate_calls) == 1
    assert len(scene.mutating_calls) == 1
    state_final = json.loads(scene.state_path.read_text(encoding="utf-8"))
    assert state_final.get("success", {}).get("container_id") == "c_external"


def test_adoption_not_blocked_by_prior_cooldown(tmp_path: Path) -> None:
    """Cooldown from previous attempts does not prevent adopting known successful generation."""
    scene = Scene(tmp_path)
    first = scene.run()
    assert first.returncode == 0, first.stderr
    assert len(scene.recreate_calls) == 1

    state = json.loads(scene.state_path.read_text(encoding="utf-8"))
    state["last_attempt"] = {
        "source_path": state["success"]["source_path"],
        "dev": state["success"]["dev"],
        "ino": state["success"]["ino"],
        "container_id": "c1",
        "timestamp": 2000000000.0,
    }
    scene.state_path.write_text(json.dumps(state), encoding="utf-8")

    scene.update(container_ids=["c_adopted"])
    second = scene.run()
    assert second.returncode == 0, second.stderr
    assert len(scene.recreate_calls) == 1
    assert len(scene.mutating_calls) == 1

    new_state = json.loads(scene.state_path.read_text(encoding="utf-8"))
    assert new_state.get("success", {}).get("container_id") == "c_adopted"
    assert "last_attempt" not in new_state


def test_socket_path_traversal_rejected(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    escape_path = "/run/shiori/../../escape"
    scene.env["GITHUB_TOKEN_SOCKET"] = escape_path
    scene.update(token_socket=escape_path)
    proc = scene.run()
    assert proc.returncode == 0, proc.stderr
    assert scene.mutating_calls == []


def test_docker_desktop_vm_path_mapping_fails_closed(tmp_path: Path) -> None:
    """Opaque Docker Desktop VM paths fail closed pending host verification.

    Correction note: The prior test asserted broad prefix acceptance of any
    path starting with '/run/desktop/'. This was a security and reliability flaw
    (arbitrary prefix trust without source verification). Unverified opaque VM
    mappings now fail closed with an explicit log message rather than inventing
    unverified path suffix equivalence.
    """
    scene = Scene(tmp_path)
    desktop_path = f"/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu{scene.source}"
    scene.update(desktop_mount_source=desktop_path)

    proc = scene.run()
    assert proc.returncode == 0, proc.stderr
    assert scene.mutating_calls == []


def test_unrelated_or_ambiguous_desktop_path_rejected(tmp_path: Path) -> None:
    """Unrelated or ambiguous paths starting with desktop prefixes are strictly rejected."""
    scene = Scene(tmp_path)
    # 1. Completely unrelated Desktop VM path
    scene.update(desktop_mount_source="/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu/other/directory")
    proc = scene.run()
    assert proc.returncode == 0, proc.stderr
    assert scene.mutating_calls == []

    # 2. Ambiguous Desktop prefix path
    scene.update(desktop_mount_source="/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts")
    proc2 = scene.run()
    assert proc2.returncode == 0, proc2.stderr
    assert scene.mutating_calls == []


def test_cooldown_keyed_by_generation_independent_of_container_id(tmp_path: Path) -> None:
    """Cooldown is keyed by filesystem generation, independent of container ID.

    Regression test for high review finding: previously, last_attempt compared
    container_id. When docker compose up recreated the container with a new ID
    but postcheck failed, the subsequent tick saw the new container ID, which
    did not match last_attempt["container_id"], bypassing cooldown retry throttling.
    Cooldown must throttle retries per filesystem generation even if the container ID changed.
    """
    scene = Scene(tmp_path)
    first = scene.run()
    assert first.returncode == 0, first.stderr
    assert len(scene.recreate_calls) == 1

    # Preserve old source directory to trigger recreate with new inode
    old = scene.source
    preserved = tmp_path / "source-preserved"
    old.rename(preserved)
    old.mkdir()
    make_socket(scene.socket_path)

    # First attempt: docker compose up succeeds and produces a new container ID ("c2"),
    # but postcheck fails because the new container is reported as not running.
    scene.update(postcheck_not_running=True)
    failed_post = scene.run()
    assert failed_post.returncode != 0
    assert len(scene.recreate_calls) == 2

    # Now on the immediate next tick, the container is running and has a different ID ("c_recovered").
    # Cooldown on filesystem generation must prevent immediate recreation even though
    # the container ID changed.
    scene.update(postcheck_not_running=False, container_ids=["c_recovered"])
    next_tick = scene.run()
    assert next_tick.returncode == 0, next_tick.stderr
    # Must NOT have recreated: recreate_calls remains 2
    assert len(scene.recreate_calls) == 2

    # Later, once the 120s cooldown expires, recreation is permitted and retried
    state = json.loads(scene.state_path.read_text(encoding="utf-8"))
    state["last_attempt"]["timestamp"] -= 130.0
    scene.state_path.write_text(json.dumps(state), encoding="utf-8")

    retry = scene.run()
    assert retry.returncode == 0, retry.stderr
    assert len(scene.recreate_calls) == 3

    # Success is now recorded with the new generation
    new_state = json.loads(scene.state_path.read_text(encoding="utf-8"))
    assert "success" in new_state
    assert new_state["success"]["ino"] == old.stat().st_ino


def test_cooldown_expiry_allows_eventual_retry(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    first = scene.run()
    assert first.returncode == 0, first.stderr
    assert len(scene.recreate_calls) == 1

    old = scene.source
    preserved = tmp_path / "source-preserved"
    old.rename(preserved)
    old.mkdir()
    make_socket(scene.socket_path)
    scene.update(recreate_exit=1)

    # Failed attempt records last_attempt
    scene.run()
    assert len(scene.recreate_calls) == 2

    # Immediate retry is suppressed
    scene.run()
    assert len(scene.recreate_calls) == 2

    # Expire cooldown by winding last_attempt timestamp back by 130s
    state = json.loads(scene.state_path.read_text(encoding="utf-8"))
    state["last_attempt"]["timestamp"] -= 130.0
    scene.state_path.write_text(json.dumps(state), encoding="utf-8")

    # Now retry is allowed and attempted
    scene.run()
    assert len(scene.recreate_calls) == 3


def test_wrong_owner_mount_source_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mount source owned by another UID is rejected at unit boundary using effective own UID."""
    spec = importlib.util.spec_from_file_location("reconcile_mint_mount", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    scene = Scene(tmp_path)
    monkeypatch.chdir(scene.project)
    for k, v in scene.env.items():
        monkeypatch.setenv(k, v)

    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    real_stat = os.stat
    target_str = str(scene.source.resolve())

    def fake_stat(path, *args, **kwargs) -> os.stat_result:
        st = real_stat(path, *args, **kwargs)
        if os.path.abspath(os.fspath(path)) == target_str:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 99999, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
        return st

    monkeypatch.setattr(mod.os, "stat", fake_stat)

    rc = mod._run_reconciliation(scene.state_path, dry_run=False)
    assert rc == 0
    assert scene.mutating_calls == []


def test_root_owned_missing_source_poisoning_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Root-owned (uid 0) mount source caused by Docker bind creation is refused without mutation."""
    spec = importlib.util.spec_from_file_location("reconcile_mint_mount", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    scene = Scene(tmp_path)
    monkeypatch.chdir(scene.project)
    for k, v in scene.env.items():
        monkeypatch.setenv(k, v)

    # Effective uid is 1000, but Docker daemon created missing source as root (uid 0)
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    real_stat = os.stat
    target_str = str(scene.source.resolve())

    def fake_stat(path, *args, **kwargs) -> os.stat_result:
        st = real_stat(path, *args, **kwargs)
        if os.path.abspath(os.fspath(path)) == target_str:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
        return st

    monkeypatch.setattr(mod.os, "stat", fake_stat)

    rc = mod._run_reconciliation(scene.state_path, dry_run=False)
    assert rc == 0
    assert scene.mutating_calls == []
    # Verify no chmod or deletion was performed: source directory still exists
    assert scene.source.is_dir()


def test_wrong_owner_socket_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Socket owned by another UID is rejected at unit boundary."""
    spec = importlib.util.spec_from_file_location("reconcile_mint_mount", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    scene = Scene(tmp_path)
    monkeypatch.chdir(scene.project)
    for k, v in scene.env.items():
        monkeypatch.setenv(k, v)

    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    real_stat = os.stat
    target_source = str(scene.source.resolve())
    target_socket = str(scene.socket_path.resolve())

    def fake_stat(path, *args, **kwargs) -> os.stat_result:
        st = real_stat(path, *args, **kwargs)
        p = os.path.abspath(os.fspath(path))
        if p == target_source:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 1000, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
        if p == target_socket:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 99999, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
        return st

    monkeypatch.setattr(mod.os, "stat", fake_stat)

    rc = mod._run_reconciliation(scene.state_path, dry_run=False)
    assert rc == 0
    assert scene.mutating_calls == []


def test_logging_terse_and_free_of_secret_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Error logging reports terse error classes and never prints subprocess stdout/stderr or config secrets."""
    spec = importlib.util.spec_from_file_location("reconcile_mint_mount", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    secret = "SUPER_SECRET_TOKEN_DO_NOT_LEAK_ghp_1234567890abcdef"

    # Simulate get_compose_config failing with a subprocess error carrying secret stdout/stderr
    def fake_compose_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["docker", "compose", "config", secret], timeout=30, output=secret, stderr=secret)

    monkeypatch.setattr(mod.subprocess, "run", fake_compose_run)

    cfg = mod.get_compose_config()
    assert cfg is None
    captured = capsys.readouterr()
    assert secret not in captured.err
    assert secret not in captured.out
    assert "TimeoutExpired" in captured.err

    # Simulate state loading exception
    corrupt_state = tmp_path / "corrupt_state.json"
    corrupt_state.write_text("NOT_JSON " + secret, encoding="utf-8")
    state = mod.load_state(corrupt_state)
    assert state == {}
    captured2 = capsys.readouterr()
    assert secret not in captured2.err
    assert "JSONDecodeError" in captured2.err


def test_custom_socket_relative_path(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    nested_sock = scene.source / "custom" / "path" / "token.sock"
    make_socket(nested_sock)
    nested_target = "/run/shiori/custom/path/token.sock"
    scene.env["GITHUB_TOKEN_SOCKET"] = nested_target
    scene.update(token_socket=nested_target)

    first = scene.run()
    assert first.returncode == 0, first.stderr
    assert len(scene.recreate_calls) == 1


def test_lock_contention_exits_cleanly(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    lock_file = scene.state_path.with_name(scene.state_path.name + ".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Reconciler cannot acquire flock, should exit 0 with no mutation
        proc = scene.run()
        assert proc.returncode == 0, proc.stderr
        assert scene.mutating_calls == []
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_state_file_and_parent_permissions(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    first = scene.run()
    assert first.returncode == 0, first.stderr
    assert scene.state_path.is_file()
    assert stat.S_IMODE(scene.state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(scene.state_path.parent.stat().st_mode) == 0o700


def test_systemd_unit_and_timer_wiring() -> None:
    service_path = REPO_ROOT / "scripts" / "systemd" / "shiori-mint-mount.service"
    timer_path = REPO_ROOT / "scripts" / "systemd" / "shiori-mint-mount.timer"
    install_script = REPO_ROOT / "scripts" / "install-systemd.sh"

    service_txt = service_path.read_text(encoding="utf-8")
    timer_txt = timer_path.read_text(encoding="utf-8")
    install_txt = install_script.read_text(encoding="utf-8")

    assert "Type=oneshot" in service_txt
    assert "WorkingDirectory=@SHIORI_DIR@" in service_txt
    assert "ExecStart=/usr/bin/python3 scripts/reconcile_mint_mount.py" in service_txt
    assert "\nRequires=" not in service_txt
    assert "\nWants=" not in service_txt

    timer_directives: set[str] = set()
    in_timer_section = False
    for line in timer_txt.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_timer_section = stripped == "[Timer]"
        elif in_timer_section and stripped and not stripped.startswith("#") and "=" in stripped:
            timer_directives.add(stripped)

    assert {"OnActiveSec=10s", "OnUnitActiveSec=2m", "AccuracySec=10s"} <= timer_directives
    assert not any(
        directive.startswith(("OnBootSec=", "Persistent="))
        for directive in timer_directives
    )
    assert "WantedBy=timers.target" in timer_txt

    assert "shiori-mint-mount.service" in install_txt
    assert "shiori-mint-mount.timer" in install_txt
    assert "systemctl --user enable --now shiori-mint-mount.timer" in install_txt
    assert "/usr/bin/python3" in install_txt
