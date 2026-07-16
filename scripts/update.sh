#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

git pull
docker compose build app

if systemctl --user is-active --quiet shiori.service 2>/dev/null; then
  systemctl --user restart shiori.service
else
  docker compose up -d app
fi

echo ""
echo "Update done. app container restarted with the new image."
