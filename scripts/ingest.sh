#!/usr/bin/env bash
# compose の ingest サービス（app と同一イメージ）でワンショット同期する。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
scripts/refresh-token-gh.sh
exec docker compose run --build --rm ingest python -m shiori ingest "$@"
