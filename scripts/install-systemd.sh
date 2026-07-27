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

systemctl --user daemon-reload

systemctl --user enable --now shiori.service
systemctl --user enable --now shiori-ingest-dev.timer shiori-ingest-ref.timer

echo ""
echo "==> Done.  Useful commands:"
echo "    systemctl --user status shiori"
echo "    systemctl --user stop shiori"
echo "    journalctl --user -u shiori -f"
echo "    systemctl --user list-timers 'shiori-ingest-*'"
echo "    journalctl --user -u shiori-ingest-dev -f"
echo ""
echo "NOTE: this only installs shiori.service.  Shiori does not own a mint-"
echo "      socket unit -- if you need GITHUB_TOKEN_SOCKET for private repos,"
echo "      install the on-demand mint socket separately via mcp-launcher's"
echo "      own scripts/install-mint-socket.sh (issue #204 / mcp-launcher#42)."
echo "      See detailed design/15 for the full rationale and the socket"
echo "      contract."
echo ""

# Ensure user services survive logout (optional, requires root or polkit).
if ! loginctl show-user "$USER" --property=Linger | grep -q '=yes'; then
  echo "NOTE: user lingering is off.  Run this once as root to keep services"
  echo "      running after logout:"
  echo "      sudo loginctl enable-linger $USER"
fi
