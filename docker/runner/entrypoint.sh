#!/usr/bin/env bash
# self-hosted runner エントリーポイント（issue #6）。
#
# アーキテクチャ:
#   - バイナリ（run.sh, bin/ 等）はイメージ内 /opt/actions-runner に配置（volume 外）
#   - 登録状態（.runner, .credentials）は /actions-runner に永続化（volume runner-state）
#   - 起動時にバージョン比較し、不一致時のみバイナリを /opt から /actions-runner へ同期
#
# 引数が渡された場合はそれを実行する（初回登録の ./config.sh ... など）。
# 引数なしで .runner ファイルが存在しない場合は、登録手順を案内して終了する。
# 登録後は docker compose up -d runner で永続起動する。
set -eu

BIN_DIR=/opt/actions-runner
STATE_DIR=/actions-runner

# ---- issue #46: バイナリ同期 ----
# イメージ内のバイナリバージョンと volume 内の記録を比較し、
# 不一致時（イメージ更新後など）に最新バイナリを同期する。
# .runner / .credentials は保持する。
if [ -f "${BIN_DIR}/.image-runner-version" ]; then
  IMAGE_VERSION="$(cat "${BIN_DIR}/.image-runner-version")"
  VOLUME_VERSION=""
  if [ -f "${STATE_DIR}/.volume-runner-version" ]; then
    VOLUME_VERSION="$(cat "${STATE_DIR}/.volume-runner-version")"
  fi

  if [ "$VOLUME_VERSION" != "$IMAGE_VERSION" ]; then
    echo "[shiori-runner] runner バージョン不一致: volume=${VOLUME_VERSION:-none}, image=${IMAGE_VERSION}" >&2
    echo "[shiori-runner] 登録情報を保持したままバイナリを同期します..." >&2

    # 登録情報を退避
    for f in .runner .credentials .credentials_rsaparams; do
      if [ -f "${STATE_DIR}/${f}" ]; then
        cp "${STATE_DIR}/${f}" "/tmp/${f}"
      fi
    done

    # volume の内容をクリア（登録情報以外）
    find "${STATE_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true

    # イメージ内の最新バイナリを volume にコピー
    # （隠しファイルを含むすべてのファイルとディレクトリ）
    for item in "${BIN_DIR}"/* "${BIN_DIR}"/.[!.]*; do
      if [ "$item" = "${BIN_DIR}/.image-runner-version" ]; then
        # イメージバージョン情報はコピーしない（volume 用は別途作成）
        continue
      fi
      cp -a "$item" "${STATE_DIR}/" 2>/dev/null || true
    done

    # 退避した登録情報をリストア
    for f in .runner .credentials .credentials_rsaparams; do
      if [ -f "/tmp/${f}" ]; then
        cp "/tmp/${f}" "${STATE_DIR}/${f}"
      fi
    done

    # volume のバージョン情報を更新
    echo -n "${IMAGE_VERSION}" > "${STATE_DIR}/.volume-runner-version"

    echo "[shiori-runner] バイナリ同期完了（version: ${IMAGE_VERSION}）" >&2
  fi
fi

cd "${STATE_DIR}"

# 引数パススルー: docker compose run --rm runner ./config.sh ... を可能にする（issue #20）
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

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