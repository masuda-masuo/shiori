#!/usr/bin/env bash
# compose の ingest サービス（app と同一イメージ）でワンショット同期する。
# トークン供給は pull 型 (on-demand mint socket) なので事前 refresh は不要。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec docker compose run --build --rm ingest python -m shiori ingest "$@"
