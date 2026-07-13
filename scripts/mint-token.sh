#!/usr/bin/env bash
# Mint a short-lived GitHub token via mcp-token and write to stdout.
# Run by systemd (shiori-mint@.service) on each socket connection.
# Standard input/output are the connected socket.
set -euo pipefail

# Prefer explicit override, then PATH.
if [ -n "${MCP_TOKEN_EXE:-}" ] && [ -x "${MCP_TOKEN_EXE}" ]; then
    exec "${MCP_TOKEN_EXE}" github
fi

exec mcp-token github
