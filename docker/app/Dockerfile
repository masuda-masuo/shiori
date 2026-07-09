FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates ripgrep tokei \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1) 依存だけ先に解決する。
#    pyproject.toml / README.md が変わらない限りこのレイヤーはキャッシュされ、
#    src/ の変更で torch 等の重い依存を再ダウンロードしない。
#    （setuptools の packages.find が src/ を要求するためスタブを置く）
#    torch は CPU 専用 wheel で入れ、CUDA 同梱の GB 級ダウンロードを避ける。
COPY pyproject.toml README.md ./
RUN mkdir -p src/shiori && touch src/shiori/__init__.py \
    && pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir .

# 2) 本物のコードを入れて、自分自身だけ再インストール（依存は再解決しない）
COPY src ./src
RUN pip install --no-cache-dir --no-deps --force-reinstall .

# モデルキャッシュは named volume（再ビルドで再ダウンロードしない）
ENV HF_HOME=/models

CMD ["python", "-m", "shiori", "serve", "--transport", "http"]
