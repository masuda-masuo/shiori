#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# トークン更新ループを止める
if [ -f runtime/refresher.pid ]; then
  kill "$(cat runtime/refresher.pid)" 2>/dev/null || true
  rm -f runtime/refresher.pid
fi
# 旧運用（ホスト venv serve）が残っていれば止める
pids=$(pgrep -f "shiori serve" 2>/dev/null || true)
if [ -n "$pids" ]; then
  kill $pids 2>/dev/null || true
fi
docker compose stop app 2>/dev/null || true
