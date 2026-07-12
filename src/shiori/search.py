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
    "id, source_type, repo, path, issue_no, comment_id, kind, language, "
    "heading_path, content, state, author, line, created_at, updated_at, url"
)

# _RESULT_COLS column positions (row tuple indices)
_COL_SOURCE_TYPE = 1
_COL_STATE = 10
_COL_UPDATED_AT = 14

# Sentinel for primary source (doc/code) compound key.
# Secondary source -sp max is 0 (open is -0). For desc sort, primary comes first
# needs sentinel > 0 → 1. _PRIMARY_DATE ("9999") is insurance;
# effectively unreachable (inert).
_PRIMARY_SP = 1
_PRIMARY_DATE = "9999"

# Fallback for missing rows. Defensively sink to lowest rank.
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
    kind: str | None
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
    for col in ("source_type", "language", "state", "repo", "prog_lang", "kind"):
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
        _id, source_type, repo, path, issue_no, _comment_id, kind, language,
        heading_path, content, state, author, line, created_at, updated_at, url,
    ) = row
    snippet = (
        content if len(content) <= snippet_chars else content[:snippet_chars] + "…"
    )
    return SearchHit(
        source_type=source_type, repo=repo, path=path, issue_no=issue_no,
        heading_path=heading_path, snippet=snippet, kind=kind,
        language=language,
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
            """,  # type: ignore
            [vec_literal(qvec), *fparams, vec_literal(qvec), limit],
        )
        return cur.fetchall()


def _to_or_query(query: str) -> str:
    terms = query.split()
    if len(terms) <= 1:
        return query
    return " OR ".join(terms)


def _keyword_candidates(
    conn: psycopg.Connection, query: str, filters: dict | None, limit: int,
    match_all: bool = False,
) -> list[tuple]:
    fsql, fparams = _filter_sql(filters)
    pgroonga_query = query if match_all else _to_or_query(query)
    # Code chunks search both content (signature + docstring) and symbols (identifier-split text)
    # via pgroonga. OR search: hit either to become candidate.
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_RESULT_COLS}, pgroonga_score(tableoid, ctid) AS score
            FROM chunks
            WHERE (content &@~ %s OR symbols &@~ %s){fsql}
            ORDER BY score DESC
            LIMIT %s
            """,  # type: ignore
            [pgroonga_query, pgroonga_query, *fparams, limit],
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
            # Defensive fallback: sink missing rows to lowest rank
            return (score, _MISSING_SP, _MISSING_DATE)

        source_type = row[_COL_SOURCE_TYPE]

        # Primary source (doc / code): score only.
        # Non-score tie-break elements neutralized by sentinel,
        # so secondary source state/updated_at does not unfairly demote them.
        if source_type in ("doc", "code"):
            return (score, _PRIMARY_SP, _PRIMARY_DATE)

        # Secondary source (issue / pr_review): compound tie-break
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


def keyword_search(
    settings: Settings,
    conn: psycopg.Connection,
    query: str,
    filters: dict | None = None,
    top_k: int | None = None,
    sort_by: str = "score",
    sort_order: str = "desc",
    match_all: bool = False,
) -> list[dict]:
    """Keyword search (Japanese tokenize). Strong for exact matches: function names, API names, error codes, config keys.
    Multi-token queries use OR matching by default (any token can match); tokens that match more/strongly rank higher.
    Pass match_all=True for AND behavior (all tokens must match the same chunk — very narrow).
    sort_by/sort_order for backward compat; ranking always relevance-based (issue #69).
    Returns list of Hit objects."""
    k = top_k or settings.default_top_k
    pool = max(k * 4, 20)
    rows = _keyword_candidates(conn, query, filters, pool, match_all=match_all)

    # Decompose candidates into (row_id, score)
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
    Same filtering as keyword_search."""
    k = top_k or settings.default_top_k
    pool = max(k * 4, 20)
    qvec = embedder.embed_query(query)
    vec_rows = _vector_candidates(conn, qvec, filters, pool)
    try:
        kw_rows = _keyword_candidates(conn, query, filters, pool)
    except psycopg.Error:
        conn.rollback()
        kw_rows = []  # Ignore pgroonga query syntax errors; return semantic-only results

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

    # Sort candidates by RRF score
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    # Apply source-aware compound tie-break at pool stage (issue #69)
    ranked, method = _rank_candidates(ranked, rows_by_id, sort_by, sort_order)

    # Truncate to top-k after tie-break
    hits = []
    for rid, score in ranked[:k]:
        h = _row_to_hit(rows_by_id[rid], settings.snippet_chars, score)
        d = h.to_dict()
        d["_ranking"] = method
        hits.append(d)
    return hits
