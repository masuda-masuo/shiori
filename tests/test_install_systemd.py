"""Acceptance tests for scripts/install-systemd.sh (shiori #467, option A).

The installer deploys the repository-managed ``shiori.service`` and companion
timers.  For #467 it must refuse *before* copying any unit, running
``daemon-reload``, or enabling/starting anything when:

* the old host-only
  ``${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/shiori.service.d/wait-docker.conf``
  exists (the installer must never delete or rewrite that unmanaged drop-in),
* the Python interpreter used by ``ExecStartPre`` is unavailable, or
* the checkout helper ``scripts/wait_for_docker.py`` is missing.

The tests run the real installer in a temporary HOME/XDG tree.  ``systemctl``
and ``loginctl`` are recording stubs on PATH, so no host service operation is
performed.  The missing-helper case runs a temporary checkout whose
``scripts/`` directory is a copy of the repository's with
``wait_for_docker.py`` removed.

Expected public seam for the implementer
----------------------------------------
``SHIORI_PYTHON3`` overrides the interpreter path checked by the installer
(default ``/usr/bin/python3``).  The unit template keeps the approved literal
``/usr/bin/python3``; the variable exists so the unavailable case can be tested
without touching the host.  The helper is looked for at
``$SHIORI_DIR/scripts/wait_for_docker.py``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "scripts" / "install-systemd.sh"
BASH = shutil.which("bash") or "/bin/bash"

UNIT_NAMES = (
    "shiori.service",
    "shiori-ingest-dev.service",
    "shiori-ingest-dev.timer",
    "shiori-ingest-ref.service",
    "shiori-ingest-ref.timer",
    "shiori-mint-mount.service",
    "shiori-mint-mount.timer",
)

SYSTEMCTL_STUB = r'''
import os, sys
with open(os.environ["SYSTEMCTL_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\n")
raise SystemExit(0)
'''

LOGINCTL_STUB = r'''
import sys
sys.stdout.write("Linger=yes\n")
raise SystemExit(0)
'''


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _stub(path: Path, body: str) -> None:
    _write_executable(path, "#!" + sys.executable + "\n" + body)


class InstallerScene:
    """A temporary HOME/XDG tree, stub commands, and a unit install target."""

    def __init__(self, root: Path, *, xdg_config: bool = True) -> None:
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        self.config = root / "config"
        self.config.mkdir()
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        self.systemctl_log = root / "systemctl.log"
        _stub(self.bin_dir / "systemctl", SYSTEMCTL_STUB)
        _stub(self.bin_dir / "loginctl", LOGINCTL_STUB)

        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith("SHIORI_"):
                del self.env[key]
        self.env["HOME"] = str(self.home)
        self.env["USER"] = "testuser"
        self.env["PATH"] = str(self.bin_dir) + os.pathsep + self.env.get("PATH", "")
        self.env["SYSTEMCTL_LOG"] = str(self.systemctl_log)
        self.env["XDG_RUNTIME_DIR"] = str(root / "runtime")
        (root / "runtime").mkdir()
        # Even an absolute-path systemctl must not reach a live user bus.
        self.env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(root / "no-bus")

        if xdg_config:
            self.env["XDG_CONFIG_HOME"] = str(self.config)
            self.unit_dir = self.config / "systemd" / "user"
        else:
            self.env.pop("XDG_CONFIG_HOME", None)
            self.unit_dir = self.home / ".config" / "systemd" / "user"

        self.installer = INSTALLER

    def use_checkout(self, checkout: Path) -> None:
        self.installer = checkout / "scripts" / "install-systemd.sh"

    def run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv runs the installer under test
            [BASH, str(self.installer)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def systemctl_calls(self) -> list[list[str]]:
        if not self.systemctl_log.is_file():
            return []
        return [
            line.split()
            for line in self.systemctl_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def seed_dropin(self, name: str, content: str) -> Path:
        path = self.unit_dir / "shiori.service.d" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


def _checkout_without_helper(root: Path) -> Path:
    checkout = root / "checkout"
    shutil.copytree(REPO_ROOT / "scripts", checkout / "scripts")
    (checkout / "scripts" / "wait_for_docker.py").unlink(missing_ok=True)
    return checkout


def _assert_refused_without_mutation(
    scene: InstallerScene, result: subprocess.CompletedProcess[str], hint: str
) -> None:
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert scene.systemctl_calls() == [], scene.systemctl_calls()
    assert not (scene.unit_dir / "shiori.service").exists()
    for name in UNIT_NAMES:
        assert not (scene.unit_dir / name).exists(), name
    assert re.search(hint, output, re.IGNORECASE), output


def test_old_wait_docker_dropin_refuses_before_any_mutation(tmp_path: Path) -> None:
    """An old host-only wait-docker.conf stops the install before any copy."""
    scene = InstallerScene(tmp_path, xdg_config=True)
    old = scene.seed_dropin(
        "wait-docker.conf",
        "# old host-only drop-in\n[Service]\n"
        "ExecStartPre=/bin/sh -c 'for i in $(seq 1 60); do "
        "[ -S /var/run/docker.sock ] && exit 0; sleep 2; done; exit 1'\n",
    )
    custom = scene.seed_dropin(
        "custom.conf", "# unmanaged drop-in\n[Service]\nEnvironment=KEEP=1\n"
    )
    old_before = old.read_text(encoding="utf-8")
    custom_before = custom.read_text(encoding="utf-8")

    result = scene.run()

    _assert_refused_without_mutation(scene, result, r"wait-docker\.conf")
    assert old.read_text(encoding="utf-8") == old_before
    assert custom.read_text(encoding="utf-8") == custom_before
    assert (scene.unit_dir / "shiori.service.d").is_dir()
    assert sorted(path.name for path in scene.unit_dir.iterdir()) == [
        "shiori.service.d"
    ]


def test_old_wait_docker_dropin_uses_home_config_fallback(tmp_path: Path) -> None:
    """With XDG_CONFIG_HOME unset the guard checks $HOME/.config/systemd/user."""
    scene = InstallerScene(tmp_path, xdg_config=False)
    old = scene.seed_dropin(
        "wait-docker.conf", "# old host-only drop-in\n[Service]\nExecStartPre=/bin/true\n"
    )
    old_before = old.read_text(encoding="utf-8")

    result = scene.run()

    _assert_refused_without_mutation(scene, result, r"wait-docker\.conf")
    assert old.read_text(encoding="utf-8") == old_before
    assert sorted(path.name for path in scene.unit_dir.iterdir()) == [
        "shiori.service.d"
    ]


def test_old_wait_docker_dangling_symlink_refuses_before_any_mutation(
    tmp_path: Path,
) -> None:
    """A dangling wait-docker.conf symlink also refuses before any copy."""
    scene = InstallerScene(tmp_path, xdg_config=True)
    dropin_dir = scene.unit_dir / "shiori.service.d"
    dropin_dir.mkdir(parents=True, exist_ok=True)
    target = dropin_dir / "nonexistent-target.conf"
    old = dropin_dir / "wait-docker.conf"
    old.symlink_to(target)
    assert old.is_symlink()
    assert not old.exists()

    custom = scene.seed_dropin(
        "custom.conf", "# unmanaged drop-in\n[Service]\nEnvironment=KEEP=1\n"
    )
    custom_before = custom.read_text(encoding="utf-8")

    result = scene.run()

    _assert_refused_without_mutation(scene, result, r"wait-docker\.conf")
    assert old.is_symlink()
    assert os.readlink(old) == str(target)
    assert custom.read_text(encoding="utf-8") == custom_before
    assert (scene.unit_dir / "shiori.service.d").is_dir()
    assert sorted(path.name for path in scene.unit_dir.iterdir()) == [
        "shiori.service.d"
    ]
    assert sorted(path.name for path in dropin_dir.iterdir()) == [
        "custom.conf",
        "wait-docker.conf",
    ]


@pytest.mark.parametrize("kind", ["missing", "not_executable"])
def test_unavailable_python_refuses_before_any_mutation(
    tmp_path: Path, kind: str
) -> None:
    """The ExecStartPre interpreter must be available before units are copied."""
    scene = InstallerScene(tmp_path)
    python = tmp_path / "python3"
    if kind == "missing":
        # Deliberately never created.
        pass
    else:
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        python.chmod(0o644)
    scene.env["SHIORI_PYTHON3"] = str(python)

    result = scene.run()

    _assert_refused_without_mutation(scene, result, r"(python3|python|interpreter)")


def test_missing_checkout_helper_refuses_before_any_mutation(
    tmp_path: Path,
) -> None:
    """A checkout without scripts/wait_for_docker.py must not install anything."""
    scene = InstallerScene(tmp_path)
    scene.use_checkout(_checkout_without_helper(tmp_path))

    result = scene.run()

    _assert_refused_without_mutation(scene, result, r"wait_for_docker")


def test_normal_install_keeps_behaviour_and_deploys_new_unit(
    tmp_path: Path,
) -> None:
    """Normal install still copies/enables the units, now with the wait wiring."""
    scene = InstallerScene(tmp_path)
    custom = scene.seed_dropin(
        "custom.conf", "# unmanaged drop-in\n[Service]\nEnvironment=KEEP=1\n"
    )
    custom_before = custom.read_text(encoding="utf-8")

    result = scene.run()

    assert result.returncode == 0, result.stdout + result.stderr

    calls = scene.systemctl_calls()
    assert calls[0] == ["--user", "daemon-reload"], calls
    enable_calls = [call for call in calls if "enable" in call]
    assert len(enable_calls) == 3, calls
    assert any("shiori.service" in call for call in enable_calls), calls
    assert any(
        "shiori-ingest-dev.timer" in call and "shiori-ingest-ref.timer" in call
        for call in enable_calls
    ), calls
    assert any("shiori-mint-mount.timer" in call for call in enable_calls), calls

    for name in UNIT_NAMES:
        assert (scene.unit_dir / name).is_file(), name

    service_text = (scene.unit_dir / "shiori.service").read_text(encoding="utf-8")
    assert "@SHIORI_DIR@" not in service_text
    assert (
        f"ExecStartPre=/usr/bin/python3 {REPO_ROOT}/scripts/wait_for_docker.py"
        in service_text
    )
    assert "Type=oneshot" in service_text
    assert "RemainAfterExit=yes" in service_text
    assert "ExecStart=/usr/bin/docker compose up -d app" in service_text
    assert "ExecStop=/usr/bin/docker compose stop app" in service_text
    assert "ExecReload=/usr/bin/docker compose restart app" in service_text
    assert "Restart=on-failure" in service_text
    assert "RestartSec=30s" in service_text
    assert "TimeoutStartSec=5min" in service_text
    assert "StartLimitIntervalSec=0" in service_text
    assert "StartLimitBurst=" not in service_text

    assert not (scene.unit_dir / "shiori.service.d" / "wait-docker.conf").exists()
    assert custom.read_text(encoding="utf-8") == custom_before
