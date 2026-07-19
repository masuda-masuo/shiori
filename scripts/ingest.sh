#!/usr/bin/env bash
# compose の ingest サービス（app と同一イメージ）でワンショット同期する。
# トークン供給は pull 型 (on-demand mint socket) なので事前 refresh は不要。
#
# ==== 有界 backfill (issue #315) ====
# 巨大な ref repo の初回 backfill は既定では全履歴を取りに行く。
# --backfill-since でカーソルを seed し、開始日時を区切れる:
#   ./scripts/ingest.sh fetch --backfill-since 2024-01-01
# 環境変数 SHIORI_REF_BACKFILL_SINCE でも指定できる（ref repo のみ適用、
# dev repo は常に全量 backfill）。seed された repo には state=open の
# 一回限りパスも走り、seed 日以前から open のままの issue も本文が索引に入る。
#
# ==== 並行実行パターン ====
# per-repo PG advisory lock (issue #307) により、同一リポジトリの同時実行は
# 排他されるが、異なるリポジトリは並行して実行できる。
# 例: 大きい ref backfill が動いている横で、
#   ./scripts/ingest.sh fetch --repo owner/dev-repo
# を実行できる（kill 運用の代替）。
# fetch/index/run の対象順序は dev repo 優先（SHIORI_DEV_REPOS が先）。
#
# ==== ビルド / GPU ====
# デフォルトでは --build なし。SHIORI_BUILD=1 でビルドを強制する。
# デフォルトでは CPU 構成。SHIORI_GPU=1 で docker-compose.gpu.yml を追加する
# （nvidia-container-toolkit が入ったホストでのみ有効）。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# compose file list (empty = docker compose default: docker-compose.yml only)
COMPOSE_FILES=()
if [ "${SHIORI_GPU:-}" = "1" ]; then
    COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.gpu.yml)
fi

if [ "${SHIORI_BUILD:-}" = "1" ]; then
    exec docker compose "${COMPOSE_FILES[@]}" run --build --rm ingest python -m shiori ingest "$@"
else
    exec docker compose "${COMPOSE_FILES[@]}" run --rm ingest python -m shiori ingest "$@"
fi
