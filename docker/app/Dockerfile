FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# モデルキャッシュは named volume（再ビルドで再ダウンロードしない）
ENV HF_HOME=/models

CMD ["python", "-m", "shiori", "serve", "--transport", "http"]
