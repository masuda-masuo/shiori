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

RUN useradd -m runner

# バイナリ配置先（volume に隠されない場所）
WORKDIR /opt/actions-runner
# latest 指定時は GitHub API から最新リリースタグを取得して動的解決
RUN if [ "$RUNNER_VERSION" = "latest" ]; then \
      RUNNER_VERSION=$(curl -s https://api.github.com/repos/actions/runner/releases/latest \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))"); \
      echo "Resolved latest runner version: v${RUNNER_VERSION}"; \
    fi \
    && echo "Downloading actions/runner v${RUNNER_VERSION}..." \
    && curl -fsSL -o runner.tar.gz \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    && tar xzf runner.tar.gz && rm runner.tar.gz \
    && ./bin/installdependencies.sh \
    && chown -R runner:runner /opt/actions-runner \
    # イメージのバージョン情報を保存（entrypoint.sh が volume との比較に使用）
    && echo -n "${RUNNER_VERSION}" > /opt/actions-runner/.image-runner-version

# installdependencies.sh がベースイメージの Debian リリースで libicu を解決できないため
# .NET ランタイムの依存を明示的にインストールする（issue #20）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libicu-dev liblttng-ust1 libkrb5-3 zlib1g \
    && rm -rf /var/lib/apt/lists/*

# Python（base イメージのグローバル環境）を runner ユーザーでも使えるようにする
RUN chown -R runner:runner /usr/local/lib/python3.12 /usr/local/bin || true

# /actions-runner を初期化（volume マウントポイント。初回は空）
RUN mkdir -p /actions-runner && chown runner:runner /actions-runner

USER runner

COPY --chown=runner:runner docker/runner/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]