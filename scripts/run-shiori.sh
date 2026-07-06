#!/usr/bin/env bash
# compose の app コンテナ（ripgrep 同梱）で shiori を serve する。
# 認証は mcp-token モデル: GitHub App の鍵は OS keystore から出さず、
# ホスト側で発行した短命トークン（runtime/github-token）だけがコンテナに届く。
#
# systemd が使える環境では scripts/install-systemd.sh で systemd 管理に移行することを推奨。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# systemd 管理下なら systemctl で制御
if systemctl --user is-active --quiet shiori.service 2>/dev/null; then
  echo "shiori is managed by systemd. Use: systemctl --user restart shiori"
  exit 1
fi

scripts/stop-shiori.sh
scripts/refresh-token.sh
nohup scripts/refresh-token.sh --loop >/dev/null 2>&1 &
echo $! > runtime/refresher.pid
docker compose up -d --build app
echo "shiori MCP: http://localhost:8765/mcp"
