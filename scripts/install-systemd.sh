#!/usr/bin/env bash
# Install shiori systemd user units and enable auto-start.
# Run once after `git clone`.  Requires systemd (Linux, WSL2 with systemd enabled).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHIORI_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

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
