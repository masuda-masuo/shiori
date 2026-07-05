#!/usr/bin/env bash
set -euo pipefail
pids=$(pgrep -f "shiori serve" 2>/dev/null || true)
if [ -n "$pids" ]; then
  echo "Stopping shiori serve (PID: $pids)..."
  kill $pids 2>/dev/null || true
fi
