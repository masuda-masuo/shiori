#!/usr/bin/env bash
# self-hosted runner エントリーポイント（issue #6）。
# 初回は .runner ファイルが存在しないため、登録手順を案内して終了する。
# 登録後は docker compose up -d runner で永続起動する。
set -eu

cd /actions-runner

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
