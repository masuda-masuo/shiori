"""検索（詳細設計/05）。

決定事項:
- `semantic_search` は内部でハイブリッド: pgvector 類似度と pgroonga キーワードの
  双方で候補を取り、RRF (k=60) で融合して top-k を返す。エージェントの入口ツール。
- `keyword_search` は pgroonga (`&@~`) による厳密寄りの検索専用として分離して残す。
  code チャンクに対しては content に加えて symbols カラムも OR 検索する（issue #33）。
- リランクモデルは v1 では不採用（RRF のみ）。
- 返すのは常にポインタ＋スニペット（既定 400 字）。state / updated_at を結果に
  含め、鮮度の判断はエージェント側に委ねる。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import psycopg

from .config import Settings
from .db import vec_literal
from .embedding import Embedder

RRF_K = 60

_RESULT_COLS = (
    "id, source_type, repo, path, issue_no, comment_id, language, "
    "heading_path, content, state, author, line, created_at, updated_at, url"
)


@dataclass
class SearchHit:
    source_type: str
    repo: str
    path: str | None
    issue_no: int | None
    heading_path: str | None
    snippet: str
    language: str | None
    state: str | None
    author: str | None
    line: int | None
    created_at: str | None
    updated_at: str | None
    url: str | None
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def _filter_sql(filters: dict | None) -> tuple[str, list]:
    clauses, params = [], []
    f = filters or {}
    for col in ("source_type", "language", "state", "repo", "prog_lang"):
        if f.get(col):
            clauses.append(f"{col} = %s")
            params.append(f[col])
    if f.get("path_prefix"):
        clauses.append("path LIKE %s")
        params.append(f["path_prefix"].rstrip("%") + "%")
    if f.get("updated_after"):
        clauses.append("updated_at >= %s")
        params.append(f["updated_after"])
    sql = (" AND " + " AND ".join(clauses)) if clauses else ""
    return sql, params


def _row_to_hit(row, snippet_chars: int, score: float) -> SearchHit:
    (
        _id, source_type, repo, path, issue_no, _comment_id, language,
        heading_path, content, state, author, line, created_at, updated_at, url,
    ) = row
    snippet = content if len(content) <= snippet_chars else content[:snippet_chars] + "…"
    return SearchHit(
        source_type=source_type, repo=repo, path=path, issue_no=issue_no,
        heading_path=heading_path, snippet=snippet, language=language,
        state=state, author=author, line=line,
        created_at=created_at.isoformat() if created_at else None,
        updated_at=updated_at.isoformat() if updated_at else None,
        url=url, score=round(score, 4),
    )


def _vector_candidates(
    conn: psycopg.Connection, qvec, filters: dict | None, limit: int
) -> list[tuple]:
    fsql, fparams = _filter_sql(filters)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_RESULT_COLS}, 1 - (embedding <=> %s::vector) AS score
            FROM chunks
            WHERE embedding IS NOT NULL{fsql}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            [vec_literal(qvec), *fparams, vec_literal(qvec), limit],
        )
        return cur.fetchall()


def _keyword_candidates(
    conn: psycopg.Connection, query: str, filters: dict | None, limit: int
) -> list[tuple]:
    fsql, fparams = _filter_sql(filters)
    # code チャンクは content（シグネチャ＋docstring）と symbols（識別子分割文字列）の
    # 両方を pgroonga 検索する。OR 検索で片方にヒットすれば候補になる。
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_RESULT_COLS}, pgroonga_score(tableoid, ctid) AS score
            FROM chunks
            WHERE (content &@~ %s OR symbols &@~ %s){fsql}
            ORDER BY score DESC
            LIMIT %s
            """,
            [query, query, *fparams, limit],
        )
        return cur.fetchall()


def keyword_search(
    settings: Settings,
    conn: psycopg.Connection,
    query: str,
    filters: dict | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """キーワード検索（日本語対応トークナイズ）。関数名・API 名・エラーコード・設定キーなど
    固有の文字列の一致に強い。semantic_search で取りこぼした厳密な語に使う。

    code チャンクに対しては content（シグネチャ＋docstring）に加えて symbols
    （識別子分割済み文字列）も OR 検索するため、camelCase や snake_case の部分一致でも
    発見できる（詳細設計/10 決定 3）。
    """
    k = top_k or settings.default_top_k
    rows = _keyword_candidates(conn, query, filters, k)
    return [
        _row_to_hit(r[:-1], settings.snippet_chars, float(r[-1])).to_dict()
        for r in rows
    ]


def semantic_search(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    query: str,
    filters: dict | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """ハイブリッド検索。ベクトルとキーワードの順位を RRF で融合する。

    source_type='code' のチャンクも検索対象に含まれる。
    キーワード側は symbols カラムも OR 検索するため、関数名やクラス名の部分一致でも
    発見できる（詳細設計/10 決定 3）。
    """
    k = top_k or settings.default_top_k
    pool = max(k * 4, 20)
    qvec = embedder.embed_query(query)
    vec_rows = _vector_candidates(conn, qvec, filters, pool)
    try:
        kw_rows = _keyword_candidates(conn, query, filters, pool)
    except psycopg.Error:
        conn.rollback()
        kw_rows = []  # pgroonga クエリ構文エラー等は無視して意味検索のみで返す

    scores: dict[int, float] = {}
    rows_by_id: dict[int, tuple] = {}
    for rank, row in enumerate(vec_rows):
        rid = row[0]
        rows_by_id[rid] = row[:-1]
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, row in enumerate(kw_rows):
        rid = row[0]
        rows_by_id.setdefault(rid, row[:-1])
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:k]
    return [
        _row_to_hit(rows_by_id[rid], settings.snippet_chars, score).to_dict()
        for rid, score in ranked
    ]
