#!/usr/bin/env bash
# compose の ingest サービス（app と同一イメージ）でワンショット同期する。
# トークン供給は pull 型 (on-demand mint socket) なので事前 refresh は不要。
#
# ==== 有界 backfill (issue #315) ====
# 巨大な ref repo の初回 backfill は既定では全履歴を取りに行く。
# --backfill-since でカーソルを seed し、開始日時を区切れる:
#   ./scripts/ingest.sh fetch --backfill-since 2024-01-01 --repo owner/repo
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
#
# ==== 実行ログ (issue #372) ====
# 起動方法（systemd timer / 手動）によらず、実行ごとにログファイルを残す。
# stdout+stderr は従来どおり journal（呼び出し元の stdout）にも流れる
# （tee による複製で、移動ではなく複製）。保存先は環境変数 SHIORI_LOG_DIR
# で上書きでき、既定はリポジトリ直下の logs/ingest/。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# compose file list (empty = docker compose default: docker-compose.yml only)
COMPOSE_FILES=()
if [ "${SHIORI_GPU:-}" = "1" ]; then
    COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.gpu.yml)
fi

# ---- 実行ログ (issue #372) ----
# レーン（起動引数）ごとにディレクトリを分ける。dev レーンは約96回/日、
# ref レーンは1回/日なので、グローバルな「新しい順に N 個」方式では騒がしい
# レーンが静かなレーンのログを追い出してしまう。掃除はレーン内で完結させる。
# ファイル名は mktemp で一意化する: 同一秒に複数起動しても衝突しない。
# このホストの時計は秒単位で狂い・逆進するため、ファイル名の時系列は当てに
# せず、掃除はカーネルが付ける mtime で行う（日単位の保持期間は秒単位の
# 時計ステップに対して頑健）。
LOG_DIR="${SHIORI_LOG_DIR:-$PWD/logs/ingest}"
LANE_NAME="$(printf '%s' "$*" | LC_ALL=C tr -cs 'A-Za-z0-9' '-')"
LANE_NAME="${LANE_NAME:-default}"
RUN_LOG_DIR="$LOG_DIR/$LANE_NAME"
mkdir -p "$RUN_LOG_DIR"

# このレーン内の古いログを削除（既定: 30日より古いもの）。
find "$RUN_LOG_DIR" -maxdepth 1 -type f -name 'ingest-*.log' -mtime +30 -delete 2>/dev/null || true

LOG_FILE="$(mktemp "$RUN_LOG_DIR/ingest-XXXXXX.log")"

# pipefail により docker compose が失敗すればその終了ステータスがそのまま
# スクリプトの終了ステータスになる（set -e と合わせて、失敗した実行は必ず
# 非ゼロで終了し systemd ユニットも失敗する）。tee 側の失敗（ディスク満杯
# など）も実行の失敗として表面化する。
if [ "${SHIORI_BUILD:-}" = "1" ]; then
    docker compose "${COMPOSE_FILES[@]}" run --build --rm ingest python -m shiori ingest "$@" 2>&1 | tee "$LOG_FILE"
else
    docker compose "${COMPOSE_FILES[@]}" run --rm ingest python -m shiori ingest "$@" 2>&1 | tee "$LOG_FILE"
fi
