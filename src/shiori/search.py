"""Search orchestration (hybrid: embedding + keyword; detailed design/03).
Two-store: pgvector (embedding, cosine similarity) + pgroonga (FTS, falls back to pg_trgm).
Hybrid: RRF fusion with configurable weights. Post-filter: language/source_type/repo/state/path_prefix.
Two modes: simple (1 table, single kNN) / complex (2 tables, kNN per combo → agg)."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import psycopg

from .config import Settings
from .db import vec_literal
from .embedding import Embedder

log = logging.getLogger(__name__)

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

# Decision-comment ranking boost (issue #404).
# Comments whose FIRST LINE is a markdown heading containing
# 設計判断/設計確定/設計決定 record a design decision (the operator's
# "## 設計判断" / "## 設計確定" convention). Signal chosen by measurement on
# citation-labeled data: lexical/heading AUC 0.873 vs embedding 0.636 (#404).
# Heading-anchored regex only -- no marker-counting lexicon (counting
# markers pulls in measurement-report comments as false positives).
# Sort-key only: key_score = score * DECISION_BOOST. The 5% cap keeps #70's
# cap principle: a chunk more than one relevance notch behind cannot be
# lifted past. Returned scores are unchanged.
DECISION_BOOST = 1.05
_DECISION_HEADING_RE = r"^#{1,6}[^\n]*設計(判断|確定|決定)"

# _RESULT_COLS column positions (row tuple indices) -- additions to the
# header block above (issue #404)
_COL_REPO = 2
_COL_ISSUE_NO = 4
_COL_COMMENT_ID = 5


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
    if f.get("labels"):
        clauses.append(
            "EXISTS (SELECT 1 FROM issue_items ii WHERE ii.repo = chunks.repo "
            "AND ii.issue_no = chunks.issue_no AND ii.comment_id = 0 "
            "AND ii.labels && %s::text[])"
        )
        params.append(f["labels"])
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
    decision_ids: set[int] | None = None,
) -> tuple[list[tuple[int, float]], str]:
    """Apply source-aware compound ranking to candidate pool (issue #69).
    Embedding: cosine distance (1-cosine). Keyword: pgroonga_score/pg_trgm.
    RRF: 0.5 embedding + 0.5 keyword.
    decision_ids: chunk ids of decision-record comments (issue #404).
    Sort-key-only 5% boost (score * DECISION_BOOST) for those chunks, applied
    only in the default sort_by="score"/sort_order="desc" mode; returned
    scores are unchanged."""
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

        # Decision-record comments (issue #404): sort-key-only boost, capped
        # at 5% so a chunk more than one relevance notch behind cannot be
        # lifted past (#70 cap principle). Only source_type='issue' chunks
        # qualify; doc/code/pr_review rows are untouched.
        if (
            decision_ids is not None
            and rid in decision_ids
            and source_type == "issue"
            and sort_by == "score"
            and sort_order == "desc"
        ):
            score = score * DECISION_BOOST

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


def _decision_comment_chunk_ids(
    conn: psycopg.Connection, rows_by_id: dict[int, tuple]
) -> set[int]:
    """Chunk ids of pool candidates that are decision-record comments (issue #404).

    ONE query: asks issue_items which of the candidates' (repo, issue_no,
    comment_id) triples have a body whose first line is a markdown heading
    containing 設計判断/設計確定/設計決定 (string-start anchored, first line
    only). Only source_type='issue' chunks are collected, so doc/code/pr_review
    rows can never qualify.
    """
    candidates = [
        (row[_COL_REPO], row[_COL_ISSUE_NO], row[_COL_COMMENT_ID])
        for row in rows_by_id.values()
        if row[_COL_SOURCE_TYPE] == "issue"
    ]
    if not candidates:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT r.repo, r.issue_no, r.comment_id
            FROM unnest(%s::text[], %s::bigint[], %s::bigint[]) AS r(repo, issue_no, comment_id)
            JOIN issue_items ii
              ON ii.repo = r.repo
             AND ii.issue_no = r.issue_no
             AND ii.comment_id = r.comment_id
            WHERE ii.body ~ %s
            """,
            [
                [c[0] for c in candidates],
                [c[1] for c in candidates],
                [c[2] for c in candidates],
                _DECISION_HEADING_RE,
            ],
        )
        qualifying = set(cur.fetchall())
    return {
        rid
        for rid, row in rows_by_id.items()
        if row[_COL_SOURCE_TYPE] == "issue"
        and (row[_COL_REPO], row[_COL_ISSUE_NO], row[_COL_COMMENT_ID]) in qualifying
    }


def _log_search(
    settings: Settings,
    conn: psycopg.Connection,
    query: str,
    search_type: str,
    filters: dict | None,
    top_k: int,
    results: list[dict],
    caller: str | None = None,
) -> None:
    """Record search execution to search_log table if logging is enabled (issue #445).

    Logging failures are caught, logged as warnings, and rolled back so that a
    failed log write never interrupts search result delivery or breaks the
    database connection context. Retention pruning is intentionally handled out
    of the interactive request path via prune_search_log().
    """
    if not settings.search_logging_enabled:
        return
    caller_val = caller if caller is not None else settings.search_caller
    try:
        import json
        filters_json = json.dumps(filters) if filters else None
        results_json = json.dumps(results)

        if hasattr(conn, "transaction") and callable(getattr(conn, "transaction")):
            ctx = conn.transaction()
        else:
            ctx = None

        if ctx is not None:
            with ctx:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO search_log (query, search_type, caller, top_k, filters, results)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (query, search_type, caller_val, top_k, filters_json, results_json),
                    )
        else:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO search_log (query, search_type, caller, top_k, filters, results)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (query, search_type, caller_val, top_k, filters_json, results_json),
                )
    except Exception as exc:
        log.warning("Failed to write search log: %s", exc, exc_info=True)


def prune_search_log(conn: psycopg.Connection, retention_days: int) -> int:
    """Prune search_log entries older than *retention_days* (issue #445).

    Runs as a periodic maintenance task out of the interactive search path.
    Uses search_log_created_at_idx index to avoid full table scans.
    Returns number of pruned log rows.
    """
    if retention_days <= 0:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM search_log
            WHERE created_at < NOW() - (%s * INTERVAL '1 day')
            """,
            (retention_days,),
        )
        deleted = cur.rowcount
    conn.commit()
    return deleted


def keyword_search(
    settings: Settings,
    conn: psycopg.Connection,
    query: str,
    filters: dict | None = None,
    top_k: int | None = None,
    sort_by: str = "score",
    sort_order: str = "desc",
    match_all: bool = False,
    caller: str | None = None,
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
    kw_info: dict[int, tuple[int, float]] = {}
    for rank, r in enumerate(rows):
        rid = r[0]
        rows_by_id[rid] = r[:-1]
        scored.append((rid, float(r[-1])))
        kw_info[rid] = (rank, round(float(r[-1]), 6))

    # Decision-record comments get a sort-key-only boost (issue #404).
    # The one qualifying query is skipped in sort modes where the boost
    # never applies (non-score sort_by / ascending order).
    decision_ids = (
        _decision_comment_chunk_ids(conn, rows_by_id)
        if sort_by == "score" and sort_order == "desc"
        else None
    )
    ranked, method = _rank_candidates(
        scored, rows_by_id, sort_by, sort_order, decision_ids=decision_ids
    )

    hits = []
    results_to_log = []
    for rid, score in ranked[:k]:
        h = _row_to_hit(rows_by_id[rid], settings.snippet_chars, score)
        d = h.to_dict()
        d["_ranking"] = method
        hits.append(d)
        kr = kw_info[rid]
        results_to_log.append({
            "id": rid,
            "score": round(score, 4),
            "vec_score": None,
            "vec_rank": None,
            "kw_score": kr[1],
            "kw_rank": kr[0],
        })

    _log_search(
        settings=settings,
        conn=conn,
        query=query,
        search_type="keyword",
        filters=filters,
        top_k=k,
        results=results_to_log,
        caller=caller,
    )
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
    caller: str | None = None,
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
    vec_info: dict[int, tuple[int, float]] = {}
    kw_info: dict[int, tuple[int, float]] = {}
    for rank, row in enumerate(vec_rows):
        rid = row[0]
        rows_by_id[rid] = row[:-1]
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)
        vec_info[rid] = (rank, round(float(row[-1]), 6))
    for rank, row in enumerate(kw_rows):
        rid = row[0]
        rows_by_id.setdefault(rid, row[:-1])
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank + 1)
        kw_info[rid] = (rank, round(float(row[-1]), 6))

    # Sort candidates by RRF score
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    # Apply source-aware compound tie-break at pool stage (issue #69)
    # Decision-record comments get a sort-key-only boost (issue #404);
    # the one qualifying query is skipped where the boost never applies.
    decision_ids = (
        _decision_comment_chunk_ids(conn, rows_by_id)
        if sort_by == "score" and sort_order == "desc"
        else None
    )
    ranked, method = _rank_candidates(
        ranked, rows_by_id, sort_by, sort_order, decision_ids=decision_ids
    )

    # Truncate to top-k after tie-break
    hits = []
    results_to_log = []
    for rid, score in ranked[:k]:
        h = _row_to_hit(rows_by_id[rid], settings.snippet_chars, score)
        d = h.to_dict()
        d["_ranking"] = method
        hits.append(d)
        vr = vec_info.get(rid)
        kr = kw_info.get(rid)
        results_to_log.append({
            "id": rid,
            "score": round(score, 4),
            "vec_score": vr[1] if vr is not None else None,
            "vec_rank": vr[0] if vr is not None else None,
            "kw_score": kr[1] if kr is not None else None,
            "kw_rank": kr[0] if kr is not None else None,
        })

    _log_search(
        settings=settings,
        conn=conn,
        query=query,
        search_type="semantic",
        filters=filters,
        top_k=k,
        results=results_to_log,
        caller=caller,
    )
    return hits
