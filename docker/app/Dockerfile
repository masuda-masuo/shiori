FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates ripgrep wget universal-ctags \
    && rm -rf /var/lib/apt/lists/* \
    && wget -qO- "https://github.com/XAMPPRocky/tokei/releases/download/v12.1.2/tokei-x86_64-unknown-linux-gnu.tar.gz" \
    | tar xz -C /usr/local/bin tokei

WORKDIR /app

COPY pyproject.toml README.md ./
RUN mkdir -p src/shiori && touch src/shiori/__init__.py \
    && pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir .

COPY src ./src
RUN pip install --no-cache-dir --no-deps --force-reinstall .

ENV HF_HOME=/models

CMD ["python", "-m", "shiori", "serve", "--transport", "http"]
