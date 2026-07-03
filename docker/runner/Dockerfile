# self-hosted GitHub Actions runner（issue #6）。
# app イメージ（shiori 本体・依存・Python 環境）をベースに Actions runner を追加する。
# ジョブは `python -m shiori ingest` を直接実行するため、shiori は PATH に通っている。
# セキュリティ注意: このイメージは private リポジトリ専用構成。
# public リポジトリで self-hosted runner を使うと fork PR から任意コードが実行される。
FROM python:3.12-slim AS shiori-base

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV HF_HOME=/models

# --- Actions runner レイヤー ---
# issue #46: バージョン固定による deprecated 停止問題への恒久対策
#
# 【設計】
# runner のバイナリは /opt/actions-runner に配置し、volume で隠されないようにする。
# /actions-runner（volume 先）は登録情報（.runner / .credentials）のみ保持する場所で、
# 起動時に entrypoint.sh が /opt/actions-runner から /actions-runner へ
# バイナリを同期する（バージョン不一致時のみ上書き）。
#
# これにより:
#   1. ビルド時に最新バージョンを取得（デフォルトは動的解決）
#   2. entrypoint で volume とイメージのバージョン不一致を検出し自動同期
#   3. volume 削除＋再登録が不要

# デフォルトは latest。ビルド時に GitHub API から最新安定版を動的解決する。
# 固定したい場合は --build-arg RUNNER_VERSION=2.xxx.x で上書き可能。
ARG RUNNER_VERSION=latest

# バイナリ配置先（volume に隠されない場所）
WORKDIR /opt/actions-runner
# latest 指定時は GitHub API から最新リリースタグを取得して動的解決
# 注意: 未認証の GitHub API は 60 req/h のレート制限がある。
# 制限超過やネットワーク障害に備え、フォールバックとして既知の安定版を使用する。
RUN if [ "$RUNNER_VERSION" = "latest" ]; then \
      RUNNER_VERSION=$(curl -sSf https://api.github.com/repos/actions/runner/releases/latest 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))" 2>/dev/null \
      ) || { \
        echo "WARNING: Failed to resolve latest runner version from GitHub API." >&2; \
        echo "WARNING: Possible causes: rate limit exceeded, network unreachable, or API changed." >&2; \
        echo "WARNING: Falling back to known stable version 2.335.1." >&2; \
        echo "WARNING: To pin a specific version, use: docker compose build --build-arg RUNNER_VERSION=2.xxx.x" >&2; \
        RUNNER_VERSION="2.335.1"; \
      }; \
      echo "Resolved runner version: v${RUNNER_VERSION}"; \
    fi \
    && echo "Downloading actions/runner v${RUNNER_VERSION}..." \
    && curl -fsSL -o runner.tar.gz \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    && tar xzf runner.tar.gz && rm runner.tar.gz \
    && ./bin/installdependencies.sh \
    # イメージのバージョン情報を保存（entrypoint.sh が volume との比較に使用）
    && echo -n "${RUNNER_VERSION}" > /opt/actions-runner/.image-runner-version

# installdependencies.sh がベースイメージの Debian リリースで libicu を解決できないため
# .NET ランタイムの依存を明示的にインストールする（issue #20）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libicu-dev liblttng-ust1 libkrb5-3 zlib1g \
    && rm -rf /var/lib/apt/lists/*

# /actions-runner を初期化（volume マウントポイント。初回は空）
RUN mkdir -p /actions-runner

# issue #48: /data/repos を app/ingest（root）と共有するため、runner も root で実行する。
#
# 背景: app/ingest は USER 指定なし＝root 実行で /data/repos にクローンを作成する。
# runner を非root（USER runner）で動かすと、root 所有のクローンに対し git 操作
# （remote set-url / fetch / reset --hard）が ".git/config を書けない" 権限エラーで失敗する。
# PR #49 の safe.directory は「所有者チェック（dubious ownership）」のみを回避するもので、
# 実体のファイル書き込み権限の衝突は別問題として残っていた。
#
# 対策: USER runner を撤去し root で実行することで、/data/repos を触る
# app / ingest / runner の UID を root に一本化する。self-hosted runner は root 実行を
# 拒否するため、RUNNER_ALLOW_RUNASROOT=1 で明示的に許可する。
# 本構成は private リポジトリ専用のため許容する（public 化時は profiles 無効化が前提）。
#
# より安全な代替案: app / ingest / runner を同一の非root UID に統一し、
# /data・/models を chown する（docker/app/Dockerfile への USER 追加が必要・差分大）。
ENV RUNNER_ALLOW_RUNASROOT=1

COPY docker/runner/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
