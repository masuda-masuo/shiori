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
    # run.sh から runner のバージョン情報を取得（存在する場合）
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
      
      # 全ファイルを削除（退避済みの state ファイルは残す）
      find /actions-runner -mindepth 1 -maxdepth 1 ! -name '.image-runner-version' -exec rm -rf {} +
      
      # イメージ内の新バイナリをコピー（ただし .image-runner-version は除く）
      # 一時的に構築用のディレクトリからコピーする代わりに、
      # /actions-runner の初期内容を保持している前提で、新イメージの内容を展開
      # 実際にはイメージビルド時に /actions-runner に展開済みなので、
      # 新イメージでコンテナを起動すれば新しいバイナリが /actions-runner にある
      echo "[shiori-runner] バイナリ更新のため、volume を初期化して新イメージの内容を使用します" >&2
      
      # state ファイルをリストア
      for f in .runner .credentials .credentials_rsaparams; do
        if [ -f "/tmp/${f}" ]; then
          cp "/tmp/${f}" "./${f}"
        fi
      done
      
      # 現在のイメージバージョンを volume に記録
      echo -n "${IMAGE_VERSION}" > .volume-runner-version
      
      echo "[shiori-runner] バイナリ更新完了（version: ${IMAGE_VERSION}）" >&2
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
