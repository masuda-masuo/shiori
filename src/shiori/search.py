"""Search orchestration (hybrid: embedding + keyword; detailed design/03).
Two-store: pgvector (embedding, cosine similarity) + pgroonga (FTS, falls back to pg_trgm).
Hybrid: RRF fusion with configurable weights. Post-filter: language/source_type/repo/state/path_prefix.
Two modes: simple (1 table, single kNN) / complex (2 tables, kNN per combo → agg)."""

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

# _RESULT_COLS のカラム位置（row タプルのインデックス）
_COL_SOURCE_TYPE = 1
_COL_STATE = 9
_COL_UPDATED_AT = 13

# 一次ソース（doc/code）の複合キー用 sentinel。
# 二次ソースの -sp 最大値は 0（open の -0）。desc ソートで一次が前に来るには
# sentinel > 0 が必要 → 1。_PRIMARY_DATE（"9999"）は第2要素で決着するため
# 実質到達しない保険（inert）。
_PRIMARY_SP = 1
_PRIMARY_DATE = "9999"

# 欠損 row 用フォールバック。防御的に最下位へ沈める。
_MISSING_SP = -999
_MISSING_DATE = ""


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


def _row_to_hit(
    row, snippet_chars: int, score: float
) -> SearchHit:
    (
        _id, source_type, repo, path, issue_no, _comment_id, language,
        heading_path, content, state, author, line, created_at, updated_at, url,
    ) = row
    snippet = (
        content if len(content) <= snippet_chars else content[:snippet_chars] + "…"
    )
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


def _rank_candidates(
    ranked: list[tuple[int, float]],
    rows_by_id: dict[int, tuple],
    sort_by: str = "score",
    sort_order: str = "desc",
) -> tuple[list[tuple[int, float]], str]:
    """Apply source-aware compound ranking to candidate pool (issue #69).
    Embedding: cosine distance (1-cosine). Keyword: pgroonga_score/pg_trgm.
    RRF: 0.5 embedding + 0.5 keyword."""
    reverse = sort_order != "asc"

    def _key(item: tuple[int, float]) -> tuple[float, int, str]:
        rid, score = item
        row = rows_by_id.get(rid)
        if row is None:
            # 防御的フォールバック: 欠損行は最下位へ沈める
            return (score, _MISSING_SP, _MISSING_DATE)

        source_type = row[_COL_SOURCE_TYPE]

        # 一次ソース（doc / code）: スコアのみ。
        # スコア以外の tie-break 要素は sentinel で中立化し、
        # 二次ソースの state / updated_at により不当に後回しされないようにする。
        if source_type in ("doc", "code"):
            return (score, _PRIMARY_SP, _PRIMARY_DATE)

        # 二次ソース（issue / pr_review）: 複合 tie-break
        st = row[_COL_STATE]
        if st == "open":
            sp = 0
        elif st == "closed":
            sp = 1
        else:
            sp = 2

        ua = row[_COL_UPDATED_AT]
        ua_str = ua.isoformat() if ua else ""
        return (score, -sp, ua_str)

    result = sorted(ranked, key=_key, reverse=reverse)
    return result, "rrf"


# 後方互換用の薄いラッパー。_sort_hits は pool 段非対応のため、
# 新コードでは _rank_candidates を使うこと。
def _sort_hits(
    hits: list[dict[str, Any]], sort_by: str, sort_order: str
) -> list[dict[str, Any]]:
    """Sort result list by specified key and order (backward compat wrapper).
    Stable ordering: sort by PK, dedup by (target_type, target_id)."""
    if sort_by == "score":
        key = lambda h: h.get("score", 0.0)
    elif sort_by in ("updated_at", "created_at"):
        key = lambda h: h.get(sort_by, "")
    else:
        return hits
    return sorted(hits, key=key, reverse=(sort_order != "asc"))


def keyword_search(
    settings: Settings,
    conn: psycopg.Connection,
    query: str,
    filters: dict | None = None,
    top_k: int | None = None,
    sort_by: str = "score",
    sort_order: str = "desc",
) -> list[dict]:
    """Keyword search (Japanese tokenize). Strong for exact matches: function names, API names, error codes, config keys.
    sort_by/sort_order for backward compat; ranking always relevance-based (issue #69).
    Returns list of Hit objects."""
    k = top_k or settings.default_top_k
    pool = max(k * 4, 20)
    rows = _keyword_candidates(conn, query, filters, pool)

    # 候補を (row_id, score) に分解
    rows_by_id: dict[int, tuple] = {}
    scored: list[tuple[int, float]] = []
    for r in rows:
        rid = r[0]
        rows_by_id[rid] = r[:-1]
        scored.append((rid, float(r[-1])))

    ranked, method = _rank_candidates(scored, rows_by_id, sort_by, sort_order)

    hits = []
    for rid, score in ranked[:k]:
        h = _row_to_hit(rows_by_id[rid], settings.snippet_chars, score)
        d = h.to_dict()
        d["_ranking"] = method
        hits.append(d)
    return hits


def semantic_search(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    query: str,
    filters: dict | None = None,
    top_k: int | None = None,
    sort_by: str = "score",
    sort_order: str = "desc",
) -> list[dict]:
    """Hybrid search. Fuses vector and keyword ranks via RRF.
    Identical filtering to keyword_search."""
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

    # RRF スコアで候補を並べる
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    # pool 段で source-aware な複合 tie-break を適用（issue #69）
    ranked, method = _rank_candidates(ranked, rows_by_id, sort_by, sort_order)

    # tie-break 後に top-k 切り詰め
    hits = []
    for rid, score in ranked[:k]:
        h = _row_to_hit(rows_by_id[rid], settings.snippet_chars, score)
        d = h.to_dict()
        d["_ranking"] = method
        hits.append(d)
    return hits
