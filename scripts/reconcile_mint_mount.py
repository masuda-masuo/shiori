#!/usr/bin/env python3
"""Host stdlib reconciliation script for shiori's mint socket mount (shiori #461).

Inspects the host mount source directory for GITHUB_TOKEN_SOCKET, compares its
filesystem generation (path, device, inode) and the running app container ID with
the persisted success state:
1. Same generation and same container ID: NOOP.
2. Same generation and new container ID: adopts current container ID into success
   state without recreation or downtime (e.g. after update.sh compose up -d app).
3. Changed generation or missing/corrupt state: re-creates the app container via
   `docker compose up -d --force-recreate --no-deps app` with postcheck verification
   and generation-keyed retry cooldown.

Only acts when shiori.service is active and all preconditions are satisfied.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

SUBPROCESS_TIMEOUT_SHORT = 15
SUBPROCESS_TIMEOUT_MEDIUM = 30
SUBPROCESS_TIMEOUT_LONG = 60
RETRY_COOLDOWN_SECONDS = 120.0


def log(msg: str) -> None:
    sys.stderr.write(f"[reconcile_mint_mount] {msg}\n")


def acquire_lock(state_path: Path) -> int | None:
    """Acquire a non-blocking flock on state_path.lock. Returns fd or None."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            state_path.parent.chmod(0o700)
        except OSError:
            pass
        lock_path = state_path.with_name(f"{state_path.name}.lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            lock_path.chmod(0o600)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (BlockingIOError, OSError):
        return None


def load_state(state_path: Path) -> dict:
    if not state_path.is_file():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        log(f"Warning: Corrupt or unreadable state file {state_path} ({type(exc).__name__}); treating as empty")
    return {}


def save_state(state_path: Path, state_data: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        state_path.parent.chmod(0o700)
    except OSError:
        pass
    tmp_path = state_path.with_name(f"{state_path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state_data, f, indent=2)
        f.flush()
        os.fsync(fd)
    os.replace(tmp_path, state_path)
    try:
        state_path.chmod(0o600)
    except OSError:
        pass


def is_systemd_active() -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "shiori.service"],  # noqa: S607
            timeout=SUBPROCESS_TIMEOUT_SHORT,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f"systemctl is-active check failed: {type(exc).__name__}")
        return False


def get_running_container_ids() -> list[str]:
    try:
        proc = subprocess.run(
            ["docker", "compose", "ps", "-q", "app"],  # noqa: S607
            timeout=SUBPROCESS_TIMEOUT_SHORT,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f"docker compose ps failed: {type(exc).__name__}")
        return []


def inspect_container(container_id: str) -> dict | None:
    try:
        proc = subprocess.run(  # noqa: S603
            ["docker", "inspect", container_id],  # noqa: S607
            timeout=SUBPROCESS_TIMEOUT_SHORT,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
    except Exception as exc:  # noqa: BLE001
        log(f"docker inspect failed for {container_id}: {type(exc).__name__}")
    return None


def get_compose_config() -> dict | None:
    try:
        proc = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],  # noqa: S607
            timeout=SUBPROCESS_TIMEOUT_MEDIUM,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            log(f"docker compose config exited with code {proc.returncode}")
            return None
        data = json.loads(proc.stdout)
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        log(f"docker compose config failed: {type(exc).__name__}")
    return None


def is_acceptable_mount_source(inspect_source: str, compose_source: Path) -> bool:
    """Validate container bind mount source against compose source.

    Exact path matching and samefile equivalence on host are supported. Arbitrary
    prefix trust is removed; unverified opaque VM paths fail closed.
    """
    if inspect_source == str(compose_source):
        return True
    try:
        p = Path(inspect_source)
        if p.exists() and p.samefile(compose_source):
            return True
    except OSError:
        pass
    return False


def main() -> int:
    default_state = (
        Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
        / "shiori"
        / "mint-mount.json"
    )
    parser = argparse.ArgumentParser(description="Reconcile shiori mint socket mount.")
    parser.add_argument(
        "--state-file",
        type=Path,
        default=default_state,
        help="Path to the persistent state file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check and log actions without mutating Docker or updating success state.",
    )
    args = parser.parse_args()

    state_path = args.state_file.resolve()

    lock_fd = acquire_lock(state_path)
    if lock_fd is None:
        log("State lock held by another process; skipping run.")
        return 0

    try:
        return _run_reconciliation(state_path, dry_run=args.dry_run)
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        except OSError:
            pass


def _run_reconciliation(state_path: Path, *, dry_run: bool) -> int:
    # 1. systemctl is-active
    if not is_systemd_active():
        log("shiori.service is not active; skipping.")
        return 0

    # 2. docker compose ps -q app returns exactly one running container
    container_ids = get_running_container_ids()
    if len(container_ids) != 1:
        log(f"Expected exactly 1 running app container, got {len(container_ids)}; skipping.")
        return 0
    container_id = container_ids[0]

    # 3. inspect container confirms running & has /run/shiori mount
    inspect_data = inspect_container(container_id)
    if not inspect_data:
        log("Failed to inspect container; skipping.")
        return 0
    if not inspect_data.get("State", {}).get("Running"):
        log(f"Container {container_id} is not running in inspect; skipping.")
        return 0

    mounts = inspect_data.get("Mounts", [])
    shiori_mount = next(
        (m for m in mounts if isinstance(m, dict) and m.get("Destination") == "/run/shiori"),
        None,
    )
    if not shiori_mount:
        log("No /run/shiori mount found in container inspect; skipping.")
        return 0
    inspect_source = shiori_mount.get("Source", "")

    # 4. resolve compose config
    compose_config = get_compose_config()
    if not compose_config:
        log("Failed to retrieve compose config; skipping.")
        return 0

    app_service = compose_config.get("services", {}).get("app")
    if not isinstance(app_service, dict):
        log("No app service found in compose config; skipping.")
        return 0

    env_data = app_service.get("environment", {})
    if isinstance(env_data, dict):
        token_socket = env_data.get("GITHUB_TOKEN_SOCKET")
    elif isinstance(env_data, list):
        token_socket = next(
            (
                item.split("=", 1)[1]
                for item in env_data
                if isinstance(item, str) and item.startswith("GITHUB_TOKEN_SOCKET=")
            ),
            None,
        )
    else:
        token_socket = None

    if not token_socket:
        log("GITHUB_TOKEN_SOCKET not set; non-socket mode, nothing to reconcile.")
        return 0

    # Socket must be /run/shiori/<relative> without escape
    if not token_socket.startswith("/run/shiori/"):
        log("GITHUB_TOKEN_SOCKET does not start with /run/shiori/; skipping.")
        return 0
    socket_rel = os.path.relpath(token_socket, "/run/shiori")
    if socket_rel == "." or socket_rel.startswith("..") or os.path.isabs(socket_rel) or ".." in Path(socket_rel).parts:
        log("Socket relative path traversal detected; skipping.")
        return 0

    # Volumes check
    volumes = app_service.get("volumes", [])
    compose_volume = next(
        (
            v for v in volumes
            if isinstance(v, dict) and (v.get("target") == "/run/shiori" or v.get("destination") == "/run/shiori")
        ),
        None,
    )
    if not compose_volume:
        log("No /run/shiori volume found in compose config; skipping.")
        return 0
    compose_source_str = compose_volume.get("source")
    if not compose_source_str:
        log("Volume /run/shiori has no source in compose config; skipping.")
        return 0
    compose_source = Path(compose_source_str).resolve()

    # Compare inspect bind source and compose source
    if not is_acceptable_mount_source(inspect_source, compose_source):
        log(
            f"Inspect mount source ({inspect_source}) does not match compose source ({compose_source}); "
            "failing closed (opaque VM mappings unverified)."
        )
        return 0

    # inspect.Config.Env check when present
    inspect_env = inspect_data.get("Config", {}).get("Env")
    if isinstance(inspect_env, list):
        for item in inspect_env:
            if isinstance(item, str) and item.startswith("GITHUB_TOKEN_SOCKET="):
                val = item.split("=", 1)[1]
                if val != token_socket:
                    log("Container Config.Env GITHUB_TOKEN_SOCKET does not agree with compose config; failing closed.")
                    return 0

    # 5. Host source and socket readiness and ownership
    expected_uid = os.geteuid()
    if not compose_source.is_dir():
        log(f"Mount source {compose_source} is not a directory or does not exist; waiting.")
        return 0
    try:
        source_stat = os.stat(compose_source)
    except OSError as exc:
        log(f"Failed to stat mount source {compose_source}: {type(exc).__name__}; waiting.")
        return 0

    if source_stat.st_uid != expected_uid:
        log(f"Mount source {compose_source} owned by uid {source_stat.st_uid}, expected {expected_uid}; waiting.")
        return 0

    host_socket = (compose_source / socket_rel).resolve()
    try:
        host_socket.relative_to(compose_source)
    except ValueError:
        log("Socket path escapes mount source; skipping.")
        return 0

    if not host_socket.exists():
        log(f"Socket {host_socket} does not exist; waiting.")
        return 0
    try:
        socket_stat = os.stat(host_socket)
    except OSError as exc:
        log(f"Failed to stat socket {host_socket}: {type(exc).__name__}; waiting.")
        return 0

    if not stat.S_ISSOCK(socket_stat.st_mode):
        log(f"Path {host_socket} is not a Unix socket; waiting.")
        return 0

    if socket_stat.st_uid != expected_uid:
        log(f"Socket {host_socket} owned by uid {socket_stat.st_uid}, expected {expected_uid}; waiting.")
        return 0

    # 6. State comparison
    state = load_state(state_path)
    current_gen = {
        "source_path": str(compose_source),
        "dev": source_stat.st_dev,
        "ino": source_stat.st_ino,
    }

    success = state.get("success")
    if (
        isinstance(success, dict)
        and success.get("source_path") == current_gen["source_path"]
        and success.get("dev") == current_gen["dev"]
        and success.get("ino") == current_gen["ino"]
    ):
        if success.get("container_id") == container_id:
            # Case 1: same generation + same container ID -> NOOP (even if socket inode changed)
            return 0

        # Case 2: same generation + new container ID -> adopt running container
        # (e.g. after update.sh runs docker compose up -d app without host runtime changes)
        prev_container_id = success.get("container_id")
        if dry_run:
            log(
                f"Dry-run: would adopt new container ID {container_id} for generation {current_gen} "
                f"(previous container: {prev_container_id})."
            )
            return 0

        state["success"]["container_id"] = container_id
        state.pop("last_attempt", None)
        save_state(state_path, state)
        log(
            f"Adopted existing running container {container_id} for unchanged mount generation "
            f"(previous container: {prev_container_id})."
        )
        return 0

    # Case 3: Changed generation or state missing/unknown/corrupt -> recreate
    # Check cooldown from last_attempt keyed on filesystem generation independent of container ID
    last_attempt = state.get("last_attempt")
    if isinstance(last_attempt, dict):
        if (
            last_attempt.get("source_path") == current_gen["source_path"]
            and last_attempt.get("dev") == current_gen["dev"]
            and last_attempt.get("ino") == current_gen["ino"]
        ):
            attempt_time = float(last_attempt.get("timestamp", 0))
            now = time.time()
            elapsed = now - attempt_time
            if 0 <= elapsed < RETRY_COOLDOWN_SECONDS:
                log(f"Recreation cooldown active ({elapsed:.1f}s < {RETRY_COOLDOWN_SECONDS}s); skipping retry.")
                return 0

    if dry_run:
        log(f"Dry-run: would recreate app container for generation {current_gen} (container: {container_id}).")
        return 0

    # 7. Pre-mutation re-verification
    if not is_systemd_active():
        log("shiori.service became inactive immediately before recreation; skipping.")
        return 0

    current_ids = get_running_container_ids()
    if current_ids != [container_id]:
        log("app container changed or stopped immediately before recreation; skipping.")
        return 0

    if not compose_source.is_dir():
        log("Mount source directory disappeared immediately before recreation; skipping.")
        return 0
    try:
        cur_source_stat = os.stat(compose_source)
        if cur_source_stat.st_dev != current_gen["dev"] or cur_source_stat.st_ino != current_gen["ino"]:
            log("Mount source generation changed immediately before recreation; skipping.")
            return 0
        cur_socket_stat = os.stat(host_socket)
        if not stat.S_ISSOCK(cur_socket_stat.st_mode):
            log("Socket became invalid immediately before recreation; skipping.")
            return 0
    except OSError as exc:
        log(f"Pre-mutation stat failed: {type(exc).__name__}; skipping.")
        return 0

    # 8. Record attempt before mutation
    state["last_attempt"] = {
        "source_path": current_gen["source_path"],
        "dev": current_gen["dev"],
        "ino": current_gen["ino"],
        "container_id": container_id,
        "timestamp": time.time(),
    }
    save_state(state_path, state)

    # 9. Execute recreation
    cmd = ["docker", "compose", "up", "-d", "--force-recreate", "--no-deps", "app"]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            timeout=SUBPROCESS_TIMEOUT_LONG,
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f"docker compose up execution failed: {type(exc).__name__}")
        return 1

    if proc.returncode != 0:
        log(f"docker compose up exited with code {proc.returncode}")
        return 1

    # 10. Postcheck
    post_ids = get_running_container_ids()
    if len(post_ids) != 1:
        log(f"Postcheck failed: expected exactly 1 running container, got {len(post_ids)}")
        return 1
    new_container_id = post_ids[0]

    inspect_post = inspect_container(new_container_id)
    if not inspect_post or not inspect_post.get("State", {}).get("Running"):
        log("Postcheck failed: new container is not running")
        return 1

    if not compose_source.is_dir():
        log("Postcheck failed: mount source is no longer a directory")
        return 1
    try:
        post_source_stat = os.stat(compose_source)
        if post_source_stat.st_dev != current_gen["dev"] or post_source_stat.st_ino != current_gen["ino"]:
            log("Postcheck failed: mount source generation changed mid-command")
            return 1
        post_socket_stat = os.stat(host_socket)
        if not stat.S_ISSOCK(post_socket_stat.st_mode):
            log("Postcheck failed: socket invalid after recreation")
            return 1
    except OSError as exc:
        log(f"Postcheck stat failed: {type(exc).__name__}")
        return 1

    # 11. Atomic commit of success
    state["success"] = {
        "source_path": current_gen["source_path"],
        "dev": current_gen["dev"],
        "ino": current_gen["ino"],
        "container_id": new_container_id,
        "timestamp": time.time(),
    }
    state.pop("last_attempt", None)
    save_state(state_path, state)
    log(f"Reconciled mint mount: app recreated successfully (container: {new_container_id}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
