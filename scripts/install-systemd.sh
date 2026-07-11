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
sed "s|@SHIORI_DIR@|$SHIORI_DIR|g" "$SCRIPT_DIR/shiori-refresh.service" > "$USER_UNIT_DIR/shiori-refresh.service"
sed "s|@SHIORI_DIR@|$SHIORI_DIR|g" "$SCRIPT_DIR/shiori-refresh.timer" > "$USER_UNIT_DIR/shiori-refresh.timer"

systemctl --user daemon-reload

systemctl --user enable --now shiori.service shiori-refresh.timer

echo ""
echo "==> Done.  Useful commands:"
echo "    systemctl --user status shiori"
echo "    systemctl --user stop shiori"
echo "    journalctl --user -u shiori -f"
echo "    systemctl --user list-timers shiori-refresh.timer"
echo "    journalctl --user -u shiori-refresh -f"
echo ""
echo "NOTE: shiori-refresh.timer keeps runtime/github-token fresh (GitHub App"
echo "      installation tokens expire in 1h; TokenCommandProvider caches for"
echo "      up to 50 min, so the token file must stay under 10 min old -- do"
echo "      not disable or lengthen this timer)."
echo ""

# Ensure user services survive logout (optional, requires root or polkit).
if ! loginctl show-user "$USER" --property=Linger | grep -q '=yes'; then
  echo "NOTE: user lingering is off.  Run this once as root to keep services"
  echo "      running after logout:"
  echo "      sudo loginctl enable-linger $USER"
fi
