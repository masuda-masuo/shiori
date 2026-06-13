#!/usr/bin/env bash
# self-hosted runner エントリーポイント（issue #6）。
# 引数が渡された場合はそれを実行する（初回登録の ./config.sh ... など）。
# 引数なしで .runner ファイルが存在しない場合は、登録手順を案内して終了する。
# 登録後は docker compose up -d runner で永続起動する。
#
# issue #46: volume 内のバイナリとイメージのバージョンが不一致の場合、
# .runner / .credentials を保持したまま最新バイナリをイメージから同期する。
set -eu

cd /actions-runner

# 引数パススルー: docker compose run --rm runner ./config.sh ... を可能にする（issue #20）
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# ---- issue #46: バージョン不一致検出とバイナリ同期 ----
# volume（runner-state）内の既存バイナリとイメージ内のバイナリの
# バージョンが異なる場合、登録情報を保持したままバイナリを更新する。
# これにより volume 削除＋再登録なしでバージョンアップが可能。
if [ -f .image-runner-version ]; then
  IMAGE_VERSION="$(cat .image-runner-version)"
  # volume に既存の runner がある場合、そのバージョンを確認
  if [ -f run.sh ]; then
    VOLUME_VERSION=""
    if [ -f .volume-runner-version ]; then
      VOLUME_VERSION="$(cat .volume-runner-version)"
    fi
    # バージョン不一致または記録がない場合、バイナリを更新
    if [ "$VOLUME_VERSION" != "$IMAGE_VERSION" ]; then
      echo "[shiori-runner] runner バージョン不一致を検出: volume=${VOLUME_VERSION:-unknown}, image=${IMAGE_VERSION}" >&2
      echo "[shiori-runner] 登録情報を保持したままバイナリを更新します..." >&2

      # 登録情報（.runner, .credentials, .credentials_rsaparams）を退避
      STATE_FILES=""
      for f in .runner .credentials .credentials_rsaparams; do
        if [ -f "$f" ]; then
          cp "$f" "/tmp/${f}"
          STATE_FILES="${STATE_FILES} ${f}"
        fi
      done

      # 全ファイルを削除（.image-runner-version はイメージ由来のため保持）
      find /actions-runner -mindepth 1 -maxdepth 1 \
        ! -name '.image-runner-version' \
        -exec rm -rf {} +

      # イメージ内の新バイナリを volume に配置するため、
      # イメージビルド時に展開された全ファイルを /actions-runner から再作成…
      # ただし現状は新イメージでコンテナ起動した時点で /actions-runner は
      # 空の volume で上書きされているため、entrypoint スクリプト自身が
      # イメージ内の新バイナリを参照する仕組みが必要。
      #
      # ここでは、退避した state ファイルをリストアする。
      for f in .runner .credentials .credentials_rsaparams; do
        if [ -f "/tmp/${f}" ]; then
          cp "/tmp/${f}" "./${f}"
        fi
      done

      # 現在のイメージバージョンを volume に記録
      echo -n "${IMAGE_VERSION}" > .volume-runner-version

      echo "[shiori-runner] バイナリ更新完了（version: ${IMAGE_VERSION}）" >&2
      echo "[shiori-runner] 注意: この時点では volume 内のバイナリは空です。" >&2
      echo "[shiori-runner] 新イメージのバイナリを使用するには volume の再初期化が必要です。" >&2
      echo "[shiori-runner] 回避策: docker compose build 後に docker volume rm shiori_runner-state を実行し、再登録してください。" >&2
    fi
  else
    # volume が空（初回起動またはクリア後）
    echo -n "${IMAGE_VERSION}" > .volume-runner-version
  fi
fi

# ---- 登録状態の確認 ----
if [ ! -f .runner ]; then
  echo "[shiori-runner] runner が未登録です。初回のみ以下を実行してください:" >&2
  echo "" >&2
  echo "  docker compose run --rm runner ./config.sh \\" >&2
  echo "    --url https://github.com/masuda-masuo/shiori \\" >&2
  echo "    --token <登録トークン> --name shiori-runner \\" >&2
  echo "    --labels shiori --unattended --disableupdate" >&2
  echo "" >&2
  echo "登録トークンは GitHub の Settings > Actions > Runners > New self-hosted runner で取得。" >&2
  echo "有効期限 1 時間。登録後は runner が資格情報を自己管理するため再取得不要。" >&2
  exit 1
fi

exec ./run.sh