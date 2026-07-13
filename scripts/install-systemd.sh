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
sed "s|@SHIORI_DIR@|$SHIORI_DIR|g" "$SCRIPT_DIR/shiori-mint.socket" > "$USER_UNIT_DIR/shiori-mint.socket"
sed "s|@SHIORI_DIR@|$SHIORI_DIR|g" "$SCRIPT_DIR/shiori-mint@.service" > "$USER_UNIT_DIR/shiori-mint@.service"

systemctl --user daemon-reload

systemctl --user enable --now shiori.service shiori-mint.socket

echo ""
echo "==> Done.  Useful commands:"
echo "    systemctl --user status shiori"
echo "    systemctl --user status shiori-mint.socket"
echo "    systemctl --user stop shiori"
echo "    journalctl --user -u shiori -f"
echo "    journalctl --user -u shiori-mint -f"
echo ""
echo "NOTE: TokenSocketProvider connects to runtime/mint.sock on demand."
echo "      No periodic refresh timer is needed -- the socket is pull-based."
echo "      See detailed design/15 for the full rationale."
echo ""

# Ensure user services survive logout (optional, requires root or polkit).
if ! loginctl show-user "$USER" --property=Linger | grep -q '=yes'; then
  echo "NOTE: user lingering is off.  Run this once as root to keep services"
  echo "      running after logout:"
  echo "      sudo loginctl enable-linger $USER"
fi
