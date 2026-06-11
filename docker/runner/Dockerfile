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
ARG RUNNER_VERSION=2.325.0

RUN useradd -m runner

WORKDIR /actions-runner
RUN curl -fsSL -o runner.tar.gz \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    && tar xzf runner.tar.gz && rm runner.tar.gz \
    && ./bin/installdependencies.sh \
    && chown -R runner:runner /actions-runner

# Python（base イメージのグローバル環境）を runner ユーザーでも使えるようにする
RUN chown -R runner:runner /usr/local/lib/python3.12 /usr/local/bin || true

USER runner

COPY --chown=runner:runner docker/runner/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
