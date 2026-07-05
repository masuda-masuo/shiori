#!/usr/bin/env bash
# compose の app コンテナ（ripgrep 同梱）で shiori を serve する。
# 認証は mcp-token モデル: GitHub App の鍵は OS keystore から出さず、
# ホスト側で発行した短命トークン（runtime/github-token）だけがコンテナに届く。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

scripts/stop-shiori.sh
scripts/refresh-token.sh
nohup scripts/refresh-token.sh --loop >/dev/null 2>&1 &
echo $! > runtime/refresher.pid
docker compose up -d --build app
echo "shiori MCP: http://localhost:8765/mcp"
