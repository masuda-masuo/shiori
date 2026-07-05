#!/usr/bin/env bash
# 短命 GitHub トークンを runtime/github-token に発行する（mcp-token モデル）。
# app コンテナは GITHUB_TOKEN_COMMAND=cat /run/shiori/github-token で読む。
# TokenCommandProvider は読んだトークンを最長 50 分使い回すため、失効（60 分）
# 以内に収めるにはファイルは常に 10 分未満の鮮度が必要。--loop の間隔は
# 300 秒（5 分）から増やさないこと。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# WSL からはホストの mcp-token.exe をそのまま実行できる
MCP_TOKEN_EXE=${MCP_TOKEN_EXE:-/mnt/c/work/mcp/mcp-token.exe}
mkdir -p runtime

refresh() {
  "$MCP_TOKEN_EXE" github > runtime/github-token.tmp
  mv -f runtime/github-token.tmp runtime/github-token
}

if [ "${1:-}" = "--loop" ]; then
  while true; do
    refresh || echo "refresh-token: mint failed; keeping previous token" >&2
    sleep 300
  done
else
  refresh
fi
