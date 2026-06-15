"""検索（詳細設計/05）。

決定事項:
- `semantic_search` は内部でハイブリッド: pgvector 類似度と pgroonga キーワードの
  双方で候補を取り、RRF (k=60) で融合して top-k を返す。MCP ツール `shiori_search`
  の実体であり、エージェントの入口ツール。
- `keyword_search` は pgroonga (`&@~`) による厳密寄りの検索専用として分離して残す。
  code チャンクに対しては content に加えて symbols カラムも OR 検索する（issue #33）。
- リランクモデルは v1 では不採用（RRF のみ）。
- 返すのは常にポインタ＋スニペット（既定 400 字）。state / updated_at を結果に
  含め、鮮度の判断はエージェント側に委ねる。
- ランキング方針（issue #69, docs/design/05）:
  - 一次ソース（doc / code）: 関連度（RRF / pgroonga スコア）のみ。日付ソートは無効。
  - 二次ソース（issue / pr_review）: 関連度主＋state / updated_at の tie-break。
  - 純粋な日付置換ソート（sort_by=updated_at/created_at で関連度を丸ごと捨てる挙動）は撤去。
  - sort_by は既定 "score" で後方互換維持。非 "score" 指定も二次ソースの tie-break に限定。
  - tie-break は pool 段（top-k 切り詰め前）で適用する。
  - 同スコアでは一次ソース（doc/code）が sentinel により二次より前に来る。
  - sort_order="asc" 時は複合キー全体が反転する（closed→open・古い→新しい）。
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
    """候補プールに source-aware な複合ランキングを適用する（issue #69）。

    ランキング方針（docs/design/05）:
    - 一次ソース（doc / code）: 関連度スコアのみ。日付系 sort_by は無効（no-op）。
      同スコアでは sentinel により二次ソースより前に来る。
    - 二次ソース（issue / pr_review）: 関連度主＋state / updated_at の tie-break。
      同スコア帯では open → 新着順に並ぶ。
    - sort_by は既定 "score" で後方互換を維持。非 "score" 指定も挙動は不変で、
      純粋な日付置換ソート（関連度を丸ごと捨てる挙動）は行わない。
      tie-break は常に updated_at で行われ、created_at は updated_at に集約される。
    - sort_order="asc" 時は複合キー全体が反転する（closed→open・古い→新しい）。

    Args:
        ranked: [(row_id, score), ...] — RRF または pgroonga スコア付き候補。
        rows_by_id: row_id → row tuple（スコア抜き）。
        sort_by: "score" / "updated_at" / "created_at"。挙動はすべて同一。
        sort_order: "desc"（既定）/ "asc"。

    Returns:
        (ranked_list, ranking_method_string) — 常に "rrf"。
    """
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
    """結果リストを指定されたキーと順序でソートする（後方互換ラッパー）。

    deprecated: 新規コードでは _rank_candidates を使用すること。
    sort_by="score" 以外は関連度を丸ごと破棄する旧挙動を維持。
    """
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
    """キーワード検索（日本語対応トークナイズ）。関数名・API 名・エラーコード・設定キーなど
    固有の文字列の一致に強い。通常は shiori_search を使い、厳密一致が必要なときに
    shiori_keyword_search を使うこと。

    code チャンクに対しては content（シグネチャ＋docstring）に加えて symbols
    （識別子分割済み文字列）も OR 検索するため、camelCase や snake_case の部分一致でも
    発見できる（詳細設計/10 決定 3）。

    ランキングは関連度主に固定（issue #69）。一次ソース（doc/code）はスコア順、
    二次ソース（issue/pr_review）はスコア＋state/updated_at tie-break。
    sort_by: "score"（既定）/ "updated_at" / "created_at"。
      純粋な日付置換ソートは行わず、二次ソースの tie-break 指定に限定される。
      created_at は updated_at に集約される。
    sort_order: "desc"（既定）/ "asc"（asc 時は複合キー全体が反転）。
    """
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
    """ハイブリッド検索。ベクトルとキーワードの順位を RRF で融合する。
    MCP ツール `shiori_search` の実体。エージェントの入口ツール。

    source_type='code' のチャンクも検索対象に含まれる。
    キーワード側は symbols カラムも OR 検索するため、関数名やクラス名の部分一致でも
    発見できる（詳細設計/10 決定 3）。

    ランキングは関連度主に固定（issue #69）。一次ソース（doc/code）は RRF スコア順、
    二次ソース（issue/pr_review）は RRF スコア＋state/updated_at tie-break。
    同スコアでは一次ソースが二次より前に来る。
    tie-break は pool 段（top-k 切り詰め前）で適用する。
    sort_by: "score"（既定）/ "updated_at" / "created_at"。
      純粋な日付置換ソートは行わず、二次ソースの tie-break 指定に限定される。
      created_at は updated_at に集約される。
    sort_order: "desc"（既定）/ "asc"（asc 時は複合キー全体が反転）。
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
