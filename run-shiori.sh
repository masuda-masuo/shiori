#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/env.sh"

# stop existing instance first
if "$DIR/stop-shiori.sh" 2>/dev/null; then
  sleep 1
fi

exec "$DIR/.venv/bin/python" -m shiori serve "$@"
