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
# デフォルトでは自動検知（issue #383）: GPU がホストに見え（nvidia-smi -L が成功）かつ
# nvidia container toolkit が入っていれば docker-compose.gpu.yml を追加し、どちらか
# 不明なら CPU。選択と理由は run ログに device=<gpu|cpu> reason="..." の形で残る。
# 明示指定が検知に優先する: SHIORI_GPU=1 で GPU 強制、SHIORI_GPU=0 で CPU 強制。
#
# ==== 実行ログ (issue #372) ====
# 起動方法（systemd timer / 手動）によらず、実行ごとにログファイルを残す。
# stdout+stderr は従来どおり journal（呼び出し元の stdout）にも流れる
# （tee による複製で、移動ではなく複製）。保存先は環境変数 SHIORI_LOG_DIR
# で上書きでき、既定はリポジトリ直下の logs/ingest/。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# ---- GPU 判定 (issue #383) ----
# docker-compose.gpu.yml を足してよいのは、以下の両方が成り立つときだけ:
#   1. GPU がホストに見えている (nvidia-smi -L が成功する)
#   2. docker が GPU をコンテナに渡せる (runtime: nvidia を処理できる
#      nvidia container toolkit がインストールされている)
# どちらか不明なら CPU のまま実行する。検知の失敗・ハングは実行を止めない
# （失敗時は CPU に倒れる）。
# プローブの制約（対象ホストで実測）: /dev/nvidia* は WSL では存在しないので使わない。
# docker info は約 10 秒かかるので使わない。nvidia-smi はドライバが詰まると
# ハングしうるので timeout で縛る。
# 明示指定が検知に優先する: SHIORI_GPU=1 で GPU 強制（検知しない）、1 以外の
# 非空値（SHIORI_GPU=0 など）で CPU 強制（検知しない）。未設定のときだけ自動検知。
GPU_DEVICE=cpu
GPU_REASON=""
if [ "${SHIORI_GPU:-}" = "1" ]; then
    GPU_DEVICE=gpu
    GPU_REASON="explicit SHIORI_GPU=1"
elif [ -n "${SHIORI_GPU:-}" ]; then
    GPU_DEVICE=cpu
    GPU_REASON="explicit SHIORI_GPU=${SHIORI_GPU}"
else
    # nvidia-smi は PATH 上にある（WSL では /usr/lib/wsl/lib）。systemd ユニットの
    # PATH は /usr/lib/wsl/lib を含まないことがあるため、既知の場所も見る。
    NV_SMI="$(command -v nvidia-smi 2>/dev/null || true)"
    if [ -z "$NV_SMI" ] && [ -x /usr/lib/wsl/lib/nvidia-smi ]; then
        NV_SMI=/usr/lib/wsl/lib/nvidia-smi
    fi
    if [ -n "$NV_SMI" ] && timeout 5 "$NV_SMI" -L >/dev/null 2>&1; then
        if command -v nvidia-container-runtime >/dev/null 2>&1 \
            || command -v nvidia-container-cli >/dev/null 2>&1 \
            || command -v nvidia-ctk >/dev/null 2>&1; then
            GPU_DEVICE=gpu
            GPU_REASON="nvidia-smi -L ok and nvidia container toolkit found"
        else
            GPU_REASON="nvidia-smi ok but nvidia container toolkit not found"
        fi
    else
        GPU_REASON="nvidia-smi -L failed (no GPU visible to host)"
    fi
fi

# compose file list (empty = docker compose default: docker-compose.yml only)
COMPOSE_FILES=()
if [ "$GPU_DEVICE" = "gpu" ]; then
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

# GPU 判定の結果を run ログに残す（journal は約 30 分で消えるため、ファイル側が本命）。
# 固定の greppable な形: device=<gpu|cpu> reason="..."
printf 'device=%s reason="%s"\n' "$GPU_DEVICE" "$GPU_REASON" | tee -a "$LOG_FILE"

# pipefail により docker compose が失敗すればその終了ステータスがそのまま
# スクリプトの終了ステータスになる（set -e と合わせて、失敗した実行は必ず
# 非ゼロで終了し systemd ユニットも失敗する）。tee 側の失敗（ディスク満杯
# など）も実行の失敗として表面化する。
# tee -a: LOG_FILE には既に GPU 判定の1行が入っているので、追記でつなげる
# （mktemp 直後は空なので、出力内容自体は -a の有無で変わらない）。
if [ "${SHIORI_BUILD:-}" = "1" ]; then
    docker compose "${COMPOSE_FILES[@]}" run --build --rm ingest python -m shiori ingest "$@" 2>&1 | tee -a "$LOG_FILE"
else
    docker compose "${COMPOSE_FILES[@]}" run --rm ingest python -m shiori ingest "$@" 2>&1 | tee -a "$LOG_FILE"
fi
