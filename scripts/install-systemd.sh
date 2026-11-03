#!/usr/bin/env bash
# Install shiori systemd user units and enable auto-start.
# Run once after `git clone`.  Requires systemd (Linux, WSL2 with systemd enabled).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHIORI_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# Preflight (shiori #467) ---------------------------------------------------
# Refuse before copying any unit, running daemon-reload, or enabling/starting
# anything.  These checks only read; unmanaged drop-ins are never deleted or
# rewritten by this installer.

OLD_WAIT_DOCKER_DROPIN="$USER_UNIT_DIR/shiori.service.d/wait-docker.conf"
START_SHIORI_WRAPPER="$SCRIPT_DIR/start_shiori.py"
WAIT_DOCKER_HELPER="$SCRIPT_DIR/wait_for_docker.py"

if [ -e "$OLD_WAIT_DOCKER_DROPIN" ] || [ -L "$OLD_WAIT_DOCKER_DROPIN" ]; then
  cat >&2 <<EOF
ERROR: old host-only Docker-wait drop-in found:
       $OLD_WAIT_DOCKER_DROPIN

shiori.service now owns the Docker readiness wait, so leaving this drop-in in
place would run two Docker waits.  Nothing has been installed or started.

Migrate manually:
  1. Back up the current unit and this drop-in.
  2. Make sure no shiori startup is in progress, then stop the service:
         systemctl --user stop shiori.service
  3. Remove ONLY the old wait-docker.conf file above.  Do not delete other
     drop-ins or the shiori.service.d directory.
  4. Run this installer again from the updated checkout.

This installer never removes or rewrites unmanaged drop-ins for you.
EOF
  exit 1
fi

require_python() {
  local python_path="$1"
  local description="$2"
  if [ ! -e "$python_path" ]; then
    echo "ERROR: $description interpreter not found: $python_path" >&2
    return 1
  fi
  if [ ! -f "$python_path" ] || [ ! -x "$python_path" ]; then
    echo "ERROR: $description interpreter is not an executable file: $python_path" >&2
    return 1
  fi
  return 0
}

# The unit always runs the literal /usr/bin/python3, so that path must work.
if ! require_python "/usr/bin/python3" "ExecStart (/usr/bin/python3)"; then
  echo "       The installed unit uses /usr/bin/python3 for start_shiori.py." >&2
  exit 1
fi

# SHIORI_PYTHON3 is an additional test/override check only; it does not change
# the interpreter path baked into the unit.
if [ -n "${SHIORI_PYTHON3:-}" ]; then
  if ! require_python "$SHIORI_PYTHON3" "SHIORI_PYTHON3"; then
    exit 1
  fi
fi

if [ ! -f "$START_SHIORI_WRAPPER" ]; then
  echo "ERROR: checkout startup wrapper not found: $START_SHIORI_WRAPPER" >&2
  echo "       shiori.service ExecStart requires scripts/start_shiori.py." >&2
  exit 1
fi

if [ ! -f "$WAIT_DOCKER_HELPER" ]; then
  echo "ERROR: checkout helper not found: $WAIT_DOCKER_HELPER" >&2
  echo "       scripts/start_shiori.py requires scripts/wait_for_docker.py." >&2
  exit 1
fi

echo "==> Installing shiori systemd user units"
echo "    shiori dir : $SHIORI_DIR"
echo "    unit dir   : $USER_UNIT_DIR"
echo ""

mkdir -p "$USER_UNIT_DIR"

sed "s|@SHIORI_DIR@|$SHIORI_DIR|g" "$SCRIPT_DIR/shiori.service" > "$USER_UNIT_DIR/shiori.service"

# Steady-sync timer units (issue #347): dev repos ~15min, ref repos daily.
for unit in shiori-ingest-dev shiori-ingest-ref; do
  sed "s|@SHIORI_DIR@|$SHIORI_DIR|g" "$SCRIPT_DIR/systemd/$unit.service" > "$USER_UNIT_DIR/$unit.service"
  cp "$SCRIPT_DIR/systemd/$unit.timer" "$USER_UNIT_DIR/$unit.timer"
done

# Mint mount reconciliation unit (issue #461): periodic check for WSL/runtime tmpfs socket mount recreation.
sed "s|@SHIORI_DIR@|$SHIORI_DIR|g" "$SCRIPT_DIR/systemd/shiori-mint-mount.service" > "$USER_UNIT_DIR/shiori-mint-mount.service"
cp "$SCRIPT_DIR/systemd/shiori-mint-mount.timer" "$USER_UNIT_DIR/shiori-mint-mount.timer"

systemctl --user daemon-reload

systemctl --user enable --now shiori.service
systemctl --user enable --now shiori-ingest-dev.timer shiori-ingest-ref.timer
systemctl --user enable --now shiori-mint-mount.timer

echo ""
echo "==> Done.  Useful commands:"
echo "    systemctl --user status shiori"
echo "    systemctl --user stop shiori"
echo "    journalctl --user -u shiori -f"
echo "    systemctl --user list-timers 'shiori-*'"
echo "    journalctl --user -u shiori-ingest-dev -f"
echo "    journalctl --user -u shiori-mint-mount -f"
echo ""
echo "PREREQUISITE: /usr/bin/python3 must be available on the host for"
echo "              shiori.service's startup wrapper and Docker readiness wait, and for"
echo "              shiori-mint-mount.service."
echo ""
echo "NOTE: this only installs shiori.service and companion timers.  Shiori does"
echo "      not own a mint-socket unit -- if you need GITHUB_TOKEN_SOCKET for"
echo "      private repos, install the on-demand mint socket separately via"
echo "      mcp-launcher's own scripts/install-mint-socket.sh (issue #204 /"
echo "      mcp-launcher#42). See detailed design/15 for the full rationale"
echo "      and the socket contract."
echo ""

# Ensure user services survive logout (optional, requires root or polkit).
if ! loginctl show-user "$USER" --property=Linger | grep -q '=yes'; then
  echo "NOTE: user lingering is off.  Run this once as root to keep services"
  echo "      running after logout:"
  echo "      sudo loginctl enable-linger $USER"
fi
