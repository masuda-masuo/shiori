#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$DIR/.env" ]; then
  set -a
  source "$DIR/.env"
  set +a
fi

# GITHUB_TOKEN_COMMAND 経由で TokenCommandProvider が定期実行・自動更新する
export GITHUB_TOKEN_COMMAND=${GITHUB_TOKEN_COMMAND:-}
export SHIORI_REPOS=${SHIORI_REPOS:-masuda-masuo/shiori}
export SHIORI_INDEX_CODE=${SHIORI_INDEX_CODE:-}
export SHIORI_ALLOW_REBUILD=${SHIORI_ALLOW_REBUILD:-false}
export DATABASE_URL=${DATABASE_URL:-postgresql://shiori:shiori@127.0.0.1:5432/shiori}
export SHIORI_INDEX_BOT_LOGINS=${SHIORI_INDEX_BOT_LOGINS:-}
export SHIORI_SYNC_INTERVAL_SECONDS=${SHIORI_SYNC_INTERVAL_SECONDS:-10}
