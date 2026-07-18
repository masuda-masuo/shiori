#!/usr/bin/env bash
# compose の ingest サービス（app と同一イメージ）でワンショット同期する。
# トークン供給は pull 型 (on-demand mint socket) なので事前 refresh は不要。
#
# ==== 並行実行パターン ====
# per-repo PG advisory lock (issue #307) により、同一リポジトリの同時実行は
# 排他されるが、異なるリポジトリは並行して実行できる。
# 例: 大きな ref backfill が動いている横で、
#   ./scripts/ingest.sh fetch --repo owner/dev-repo
# を実行できる（kill 運用の代替）。
#
# ビルド: デフォルトでは --build なし。SHIORI_BUILD=1 でビルドを強制する。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [ "${SHIORI_BUILD:-}" = "1" ]; then
    exec docker compose run --build --rm ingest python -m shiori ingest "$@"
else
    exec docker compose run --rm ingest python -m shiori ingest "$@"
fi
