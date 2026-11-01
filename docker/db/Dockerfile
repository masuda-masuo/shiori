# PostgreSQL + pgroonga（日本語全文検索）+ pgvector（埋め込み）
# 詳細設計/04 の決定: pgroonga 採用。TokenMecab 用プラグインも同梱を試みる
# （無くてもアプリ側が TokenBigram にフォールバックする）。
FROM groonga/pgroonga:latest-debian-16

# Installing groonga-tokenizer-mecab upgrades Groonga/Arrow and requires explicit
# opentelemetry-cpp runtime dependency to satisfy dynamic linking for pgroonga.so.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-16-pgvector \
        groonga-tokenizer-mecab \
        opentelemetry-cpp \
    && rm -rf /var/lib/apt/lists/* \
    && test -f /usr/lib/postgresql/16/lib/pgroonga.so \
    && ldd /usr/lib/postgresql/16/lib/pgroonga.so \
    && ! ldd /usr/lib/postgresql/16/lib/pgroonga.so | grep -F "not found"
