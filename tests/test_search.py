"""Unit tests for the search module (issue #41, #69)."""

from __future__ import annotations

import contextlib
import json
from typing import cast

import psycopg

from shiori.config import Settings
from shiori.embedding import Embedder
from shiori.search import (
    _filter_sql,
    _rank_candidates,
    _to_or_query,
    keyword_search,
    prune_search_log,
    semantic_search,
)

# _RESULT_COLS indices (must match constants in search.py)
_COL_SOURCE_TYPE = 1
_COL_KIND = 6
_COL_STATE = 10
_COL_UPDATED_AT = 14


def _make_row(source_type, kind=None, state=None, updated_at=None, created_at=None):
    """Build a mock row tuple matching _RESULT_COLS (issue #98)."""
    return (
        1, source_type, "test/repo", "dummy/path", None, None, kind,
        "ja", "heading", "content text", state, "author", None,
        created_at, updated_at, "https://example.com",
    )


# ── _rank_candidates tests (issue #69) ──

class TestRankCandidates:
    """Source-aware multi-pool ranking behavior."""

    # ── Primary sources (doc / code): score only ──

    def test_primary_sources_score_only(self):
        from datetime import datetime, timezone
        ts1 = datetime(2026, 6, 10, tzinfo=timezone.utc)
        ts2 = datetime(2026, 6, 14, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("doc", state=None, updated_at=ts1), 2: _make_row("code", state=None, updated_at=ts2)}
        ranked = [(1, 0.3), (2, 0.8)]
        result, method = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [2, 1]
        assert method == "rrf"

    def test_primary_sources_ignore_date_sort_by(self):
        """Primary sources: score order is preserved even with sort_by=updated_at."""
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("doc", state=None, updated_at=ts_old), 2: _make_row("doc", state=None, updated_at=ts_new)}
        ranked = [(1, 0.9), (2, 0.5)]
        result, method = _rank_candidates(ranked, rows_by_id, sort_by="updated_at")
        assert [rid for rid, _ in result] == [1, 2]
        assert method == "rrf"

    # ── Secondary sources (issue / pr_review): compound tie-break ──

    def test_secondary_sources_score_primary(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("issue", state="open", updated_at=ts), 2: _make_row("pr_review", state="open", updated_at=ts)}
        ranked = [(1, 0.3), (2, 0.8)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [2, 1]

    def test_secondary_tie_break_state_priority(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("issue", state="closed", updated_at=ts), 2: _make_row("issue", state="open", updated_at=ts)}
        ranked = [(1, 0.5), (2, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [2, 1]

    def test_secondary_tie_break_updated_at(self):
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("issue", state="open", updated_at=ts_old), 2: _make_row("pr_review", state="open", updated_at=ts_new)}
        ranked = [(1, 0.5), (2, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [2, 1]

    def test_secondary_state_beats_updated_at(self):
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("issue", state="open", updated_at=ts_old), 2: _make_row("issue", state="closed", updated_at=ts_new)}
        ranked = [(1, 0.5), (2, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [1, 2]

    # ── created_at converges to updated_at (review #1) ──

    def test_sort_by_created_at_same_as_updated_at(self):
        """sort_by=created_at produces the same tie-break as updated_at."""
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("issue", state="open", updated_at=ts_old), 2: _make_row("issue", state="open", updated_at=ts_new)}
        ranked = [(1, 0.5), (2, 0.5)]
        result_ua, method_ua = _rank_candidates(ranked, rows_by_id, sort_by="updated_at")
        result_ca, method_ca = _rank_candidates(ranked, rows_by_id, sort_by="created_at")
        assert result_ua == result_ca
        assert method_ua == "rrf"
        assert method_ca == "rrf"

    # ── Mixed sources ──

    def test_mixed_sources_score_primary(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("doc", state=None, updated_at=ts), 2: _make_row("issue", state="open", updated_at=ts), 3: _make_row("code", state=None, updated_at=ts)}
        ranked = [(1, 0.3), (2, 0.9), (3, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [2, 3, 1]

    def test_mixed_sources_primary_before_secondary_at_equal_score(self):
        """At equal score, primary sources come before secondary via sentinel."""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("doc", state=None, updated_at=ts), 2: _make_row("issue", state="open", updated_at=ts)}
        ranked = [(1, 0.5), (2, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [1, 2]

    # ── sort_order ──

    def test_sort_order_asc(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("doc", state=None, updated_at=ts), 2: _make_row("doc", state=None, updated_at=ts)}
        ranked = [(1, 0.3), (2, 0.8)]
        result, _ = _rank_candidates(ranked, rows_by_id, sort_order="asc")
        assert [rid for rid, _ in result] == [1, 2]

    def test_sort_order_asc_secondary_reverses_composite(self):
        """sort_order=asc reverses the entire composite key: closed→open, old→new."""
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("issue", state="open", updated_at=ts_new), 2: _make_row("issue", state="closed", updated_at=ts_old)}
        ranked = [(1, 0.5), (2, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id, sort_order="asc")
        # asc: closed→open, old→new
        assert [rid for rid, _ in result] == [2, 1]

    # ── Edge / boundary cases ──

    def test_empty_candidates(self):
        result, method = _rank_candidates([], {})
        assert result == []
        assert method == "rrf"

    def test_single_candidate(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("doc", state=None, updated_at=ts)}
        ranked = [(1, 0.5)]
        result, method = _rank_candidates(ranked, rows_by_id)
        assert result == [(1, 0.5)]
        assert method == "rrf"

    def test_missing_row_goes_last(self):
        """IDs missing from rows_by_id sink to the bottom (defensive fallback)."""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("issue", state="open", updated_at=ts)}
        ranked = [(999, 0.5), (1, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [1, 999]

    def test_state_none_secondary(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("issue", state=None, updated_at=ts), 2: _make_row("issue", state="closed", updated_at=ts), 3: _make_row("issue", state="open", updated_at=ts)}
        ranked = [(1, 0.5), (2, 0.5), (3, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [3, 2, 1]

    def test_updated_at_none_secondary(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("issue", state="open", updated_at=None), 2: _make_row("issue", state="open", updated_at=ts)}
        ranked = [(1, 0.5), (2, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [2, 1]

    # ── ranking_method ──

    def test_ranking_method_always_rrf(self):
        """method is always "rrf" regardless of sort_by."""
        for sb in ("score", "updated_at", "created_at"):
            _, method = _rank_candidates([], {}, sort_by=sb)
            assert method == "rrf", f"sort_by={sb} should return 'rrf'"

    # ── Pool stage verification ──

    def test_pool_stage_allows_newer_closed_to_enter_top_k(self):
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_mid = datetime(2026, 3, 15, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("issue", state="closed", updated_at=ts_old), 2: _make_row("issue", state="closed", updated_at=ts_mid), 3: _make_row("issue", state="open", updated_at=ts_old), 4: _make_row("issue", state="open", updated_at=ts_new)}
        ranked = [(1, 0.5), (2, 0.5), (3, 0.5), (4, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [4, 3, 2, 1]


# ── _filter_sql tests (issue #98) ──

class TestFilterSql:
    """kind filter SQL generation."""

    def test_no_filters_returns_empty_clause(self):
        sql, params = _filter_sql(None)
        assert sql == ""
        assert params == []

    def test_empty_filters_returns_empty_clause(self):
        sql, params = _filter_sql({})
        assert sql == ""
        assert params == []

    def test_kind_issue_filter(self):
        sql, params = _filter_sql({"kind": "issue"})
        assert "kind = %s" in sql
        assert params == ["issue"]

    def test_kind_pr_filter(self):
        sql, params = _filter_sql({"kind": "pr"})
        assert "kind = %s" in sql
        assert params == ["pr"]

    def test_kind_combined_with_source_type(self):
        sql, params = _filter_sql({"source_type": "issue", "kind": "pr"})
        assert "source_type = %s" in sql
        assert "kind = %s" in sql
        assert params == ["issue", "pr"]

    def test_kind_combined_with_state(self):
        sql, params = _filter_sql({"kind": "issue", "state": "open"})
        assert "kind = %s" in sql
        assert "state = %s" in sql
        assert "open" in params
        assert "issue" in params

    def test_kind_none_excluded(self):
        sql, params = _filter_sql({"kind": None, "state": "open"})
        assert "kind" not in sql
        assert "state = %s" in sql
        assert params == ["open"]

    def test_labels_filter_adds_exists_subquery(self):
        sql, params = _filter_sql({"labels": ["bug", "enhancement"]})
        assert "EXISTS" in sql
        assert "issue_items" in sql
        assert "&&" in sql
        assert params == [["bug", "enhancement"]]

    def test_labels_filter_combined_with_other_filters(self):
        sql, params = _filter_sql({
            "labels": ["bug"],
            "state": "open",
            "source_type": "issue",
        })
        assert "EXISTS" in sql
        assert "issue_items" in sql
        assert "state = %s" in sql
        assert "source_type = %s" in sql
        assert params == ["issue", "open", ["bug"]]

    def test_labels_filter_none_excluded(self):
        sql, params = _filter_sql({"labels": None})
        assert sql == ""
        assert params == []

    def test_labels_filter_empty_list_excluded(self):
        sql, params = _filter_sql({"labels": []})
        assert sql == ""
        assert params == []


# ── _to_or_query tests (issue #99) ──

class TestToOrQuery:
    def test_single_token_passthrough(self):
        assert _to_or_query("clone_dest") == "clone_dest"

    def test_no_space_passthrough(self):
        assert _to_or_query("日本語") == "日本語"

    def test_two_tokens_or_joined(self):
        assert _to_or_query("clone_dest repo_name") == "clone_dest OR repo_name"

    def test_three_tokens_or_joined(self):
        result = _to_or_query("clone_dest repo_name _try_clone_into_container")
        assert result == "clone_dest OR repo_name OR _try_clone_into_container"

    def test_leading_trailing_whitespace(self):
        result = _to_or_query("  clone_dest repo_name  ")
        assert result == "clone_dest OR repo_name"

    def test_multiple_spaces_between_tokens(self):
        result = _to_or_query("clone_dest    repo_name")
        assert result == "clone_dest OR repo_name"

    def test_empty_string_returns_empty(self):
        assert _to_or_query("") == ""

    def test_single_character_tokens(self):
        result = _to_or_query("a b c")
        assert result == "a OR b OR c"


# ── Search Logging tests (issue #445) ──


class _FakeSearchLogCursor:
    def __init__(self, chunk_rows=None, vec_rows=None, kw_rows=None, raise_on_insert=False):
        self.chunk_rows = chunk_rows or []
        self.vec_rows = vec_rows
        self.kw_rows = kw_rows
        self.raise_on_insert = raise_on_insert
        self.executed: list[tuple[str, list | None]] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        sql_str = str(sql)
        if self.raise_on_insert and "INSERT INTO search_log" in sql_str:
            raise psycopg.Error("Simulated DB log write error")
        self.executed.append((sql_str, params))
        if "DELETE FROM search_log" in sql_str:
            self.rowcount = 5

    def fetchall(self):
        if not self.executed:
            return list(self.chunk_rows)
        last_sql = self.executed[-1][0]
        if "unnest" in last_sql and "issue_items" in last_sql:
            return []
        if "embedding <=" in last_sql:
            return list(self.vec_rows if self.vec_rows is not None else self.chunk_rows)
        if "pgroonga_score" in last_sql:
            return list(self.kw_rows if self.kw_rows is not None else self.chunk_rows)
        return list(self.chunk_rows)


class _FakeSearchLogConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass


class _FakeTxCtx:
    def __init__(self, conn):
        self.conn = conn
        self.entered = False
        self.exited = False
        self.exception_handled = False

    def __enter__(self):
        self.entered = True
        self.conn.tx_events.append("enter_tx")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.exited = True
        if exc_type is not None:
            self.exception_handled = True
            self.conn.tx_events.append(f"rollback_tx:{exc_type.__name__}")
            return False
        self.conn.tx_events.append("commit_tx")
        return False


class _FakeTxConn(_FakeSearchLogConn):
    def __init__(self, cursor):
        super().__init__(cursor)
        self.tx_events: list[str] = []
        self.last_tx: _FakeTxCtx | None = None

    def transaction(self):
        self.last_tx = _FakeTxCtx(self)
        return self.last_tx


class _FakeEmbedder:
    def embed_query(self, query):
        return [0.1, 0.2, 0.3]


def _log_chunk_row(rid, content="test content", score=0.85):
    return (
        rid, "doc", "owner/repo", "docs/spec.md", None, None, None,
        "ja", "heading", content, None, "author", None,
        None, None, "https://example.com", score,
    )


class TestSearchLogging:
    """Acceptance criteria tests for search execution logging (issue #445)."""

    def test_semantic_search_writes_log_row(self):
        """Criterion 1: Running semantic_search writes query, chunk ids with scores, and repo filter."""
        # Both vec_rows and kw_rows contain the same chunk (dual-retriever hit)
        chunk = _log_chunk_row(42, "semantic content", 0.95)
        cursor = _FakeSearchLogCursor(vec_rows=[chunk], kw_rows=[chunk])
        conn = cast(psycopg.Connection, _FakeSearchLogConn(cursor))
        embedder = cast(Embedder, _FakeEmbedder())
        settings = Settings()

        hits = semantic_search(
            settings, conn, embedder, "vector query", filters={"repo": "owner/repo"}, top_k=5
        )
        assert len(hits) == 1

        insert_calls = [p for s, p in cursor.executed if "INSERT INTO search_log" in s]
        assert len(insert_calls) == 1
        query, search_type, caller, top_k, filters_json, results_json = insert_calls[0]
        assert query == "vector query"
        assert search_type == "semantic"
        assert top_k == 5
        assert json.loads(filters_json) == {"repo": "owner/repo"}
        res = json.loads(results_json)
        assert len(res) == 1
        assert res[0]["id"] == 42
        # Dual-retriever hit: all four sub-fields non-null
        assert res[0]["vec_score"] is not None
        assert res[0]["vec_rank"] == 0
        assert res[0]["kw_score"] is not None
        assert res[0]["kw_rank"] == 0
        # Fused score is still the RRF value
        assert isinstance(res[0]["score"], float)

    def test_semantic_search_vector_only_hit(self):
        """Chunk found by vector retriever only: kw_score/kw_rank are null."""
        vec_chunk = _log_chunk_row(10, "vector only", 0.90)
        # kw_rows is empty — the chunk is absent from keyword candidates
        cursor = _FakeSearchLogCursor(vec_rows=[vec_chunk], kw_rows=[])
        conn = cast(psycopg.Connection, _FakeSearchLogConn(cursor))
        embedder = cast(Embedder, _FakeEmbedder())
        settings = Settings()

        hits = semantic_search(settings, conn, embedder, "vector query", top_k=5)
        assert len(hits) == 1

        insert_calls = [p for s, p in cursor.executed if "INSERT INTO search_log" in s]
        assert len(insert_calls) == 1
        res = json.loads(insert_calls[0][5])
        assert len(res) == 1
        assert res[0]["id"] == 10
        assert res[0]["vec_score"] is not None
        assert res[0]["vec_rank"] == 0
        assert res[0]["kw_score"] is None
        assert res[0]["kw_rank"] is None

    def test_semantic_search_keyword_only_hit(self):
        """Chunk found by keyword retriever only: vec_score/vec_rank are null."""
        kw_chunk = _log_chunk_row(20, "keyword only", 0.80)
        # vec_rows is empty — the chunk is absent from vector candidates
        cursor = _FakeSearchLogCursor(vec_rows=[], kw_rows=[kw_chunk])
        conn = cast(psycopg.Connection, _FakeSearchLogConn(cursor))
        embedder = cast(Embedder, _FakeEmbedder())
        settings = Settings()

        hits = semantic_search(settings, conn, embedder, "keyword query", top_k=5)
        assert len(hits) == 1

        insert_calls = [p for s, p in cursor.executed if "INSERT INTO search_log" in s]
        assert len(insert_calls) == 1
        res = json.loads(insert_calls[0][5])
        assert len(res) == 1
        assert res[0]["id"] == 20
        assert res[0]["vec_score"] is None
        assert res[0]["vec_rank"] is None
        assert res[0]["kw_score"] is not None
        assert res[0]["kw_rank"] == 0

    def test_semantic_search_both_retrievers_hit(self):
        """Chunk found by both retrievers: all four sub-fields non-null, ranks reflect retriever positions."""
        # Two chunks found by both retrievers, in different orders per retriever
        vec_rows = [_log_chunk_row(1, "vec first", 0.95), _log_chunk_row(2, "vec second", 0.85)]
        kw_rows = [_log_chunk_row(2, "kw first", 0.90), _log_chunk_row(1, "kw second", 0.80)]
        cursor = _FakeSearchLogCursor(vec_rows=vec_rows, kw_rows=kw_rows)
        conn = cast(psycopg.Connection, _FakeSearchLogConn(cursor))
        embedder = cast(Embedder, _FakeEmbedder())
        settings = Settings()

        hits = semantic_search(settings, conn, embedder, "query", top_k=10)
        assert len(hits) == 2

        insert_calls = [p for s, p in cursor.executed if "INSERT INTO search_log" in s]
        assert len(insert_calls) == 1
        res = json.loads(insert_calls[0][5])
        assert len(res) == 2

        res_by_id = {r["id"]: r for r in res}
        # Chunk 1: vec_rank=0, kw_rank=1 (second in kw_rows)
        assert res_by_id[1]["vec_rank"] == 0
        assert res_by_id[1]["kw_rank"] == 1
        # Chunk 2: vec_rank=1, kw_rank=0
        assert res_by_id[2]["vec_rank"] == 1
        assert res_by_id[2]["kw_rank"] == 0
        # All non-null
        for r in res:
            assert r["vec_score"] is not None
            assert r["kw_score"] is not None

    def test_semantic_search_rank_reflects_candidate_order_not_final_order(self):
        """Ranks recorded are retriever candidate-list positions, not post-tiebreak display order."""
        # vec candidate order: chunk A at rank 0, chunk B at rank 1
        # kw candidate order: chunk B at rank 0, chunk A at rank 1
        # RRF fusion gives equal scores; tiebreak may reorder, but recorded ranks stay as above.
        vec_rows = [_log_chunk_row(100, "A", 0.90), _log_chunk_row(200, "B", 0.85)]
        kw_rows = [_log_chunk_row(200, "B", 0.90), _log_chunk_row(100, "A", 0.80)]
        cursor = _FakeSearchLogCursor(vec_rows=vec_rows, kw_rows=kw_rows)
        conn = cast(psycopg.Connection, _FakeSearchLogConn(cursor))
        embedder = cast(Embedder, _FakeEmbedder())
        settings = Settings()

        semantic_search(settings, conn, embedder, "query", top_k=10)

        insert_calls = [p for s, p in cursor.executed if "INSERT INTO search_log" in s]
        res = json.loads(insert_calls[0][5])
        res_by_id = {r["id"]: r for r in res}

        # Regardless of final display order, ranks reflect retriever candidate positions
        assert res_by_id[100]["vec_rank"] == 0
        assert res_by_id[100]["kw_rank"] == 1
        assert res_by_id[200]["vec_rank"] == 1
        assert res_by_id[200]["kw_rank"] == 0

    def test_keyword_search_writes_log_row(self):
        """Criterion 2: Running keyword_search writes query, chunk ids with scores, and repo filter."""
        cursor = _FakeSearchLogCursor(chunk_rows=[_log_chunk_row(101, "keyword content", 0.88)])
        conn = cast(psycopg.Connection, _FakeSearchLogConn(cursor))
        settings = Settings()

        hits = keyword_search(
            settings, conn, "exact phrase", filters={"repo": "owner/repo"}, top_k=3
        )
        assert len(hits) == 1

        insert_calls = [p for s, p in cursor.executed if "INSERT INTO search_log" in s]
        assert len(insert_calls) == 1
        query, search_type, caller, top_k, filters_json, results_json = insert_calls[0]
        assert query == "exact phrase"
        assert search_type == "keyword"
        assert top_k == 3
        assert json.loads(filters_json) == {"repo": "owner/repo"}
        res = json.loads(results_json)
        assert res == [{
            "id": 101, "score": 0.88,
            "vec_score": None, "vec_rank": None,
            "kw_score": 0.88, "kw_rank": 0,
        }]

    def test_log_write_failure_isolated_and_observable(self, caplog):
        """Criterion 3: Search returns full hits when log write fails, and failure is logged."""
        cursor = _FakeSearchLogCursor(
            chunk_rows=[_log_chunk_row(10, "content", 0.75)],
            raise_on_insert=True,
        )
        conn = cast(psycopg.Connection, _FakeSearchLogConn(cursor))
        settings = Settings()

        with caplog.at_level("WARNING", logger="shiori.search"):
            hits = keyword_search(settings, conn, "failed log query", top_k=2)

        # Half 1: Full result set still returned
        assert len(hits) == 1
        assert hits[0]["snippet"] == "content"

        # Half 2: Failure is observable in logs
        assert "Failed to write search log" in caplog.text
        assert "Simulated DB log write error" in caplog.text

    def test_logging_disabled_no_row_written_and_identical_hits(self):
        """Criterion 4: Disabled logging writes no row and return hits are byte-identical."""
        chunk = _log_chunk_row(7, "same content", 0.9)
        cursor_enabled = _FakeSearchLogCursor(chunk_rows=[chunk])
        cursor_disabled = _FakeSearchLogCursor(chunk_rows=[chunk])

        settings_enabled = Settings()
        settings_disabled = Settings()
        settings_disabled.search_logging_enabled = False

        hits_enabled = keyword_search(settings_enabled, cast(psycopg.Connection, _FakeSearchLogConn(cursor_enabled)), "query")
        hits_disabled = keyword_search(settings_disabled, cast(psycopg.Connection, _FakeSearchLogConn(cursor_disabled)), "query")

        # Hits are byte-identical
        assert hits_enabled == hits_disabled

        # Enabled wrote a row, disabled wrote no row
        assert any("INSERT INTO search_log" in s for s, _ in cursor_enabled.executed)
        assert not any("INSERT INTO search_log" in s for s, _ in cursor_disabled.executed)

    def test_caller_attribution_distinguishes_callers(self):
        """Criterion 5: Two searches attributed to different callers produce distinct rows."""
        cursor1 = _FakeSearchLogCursor(chunk_rows=[_log_chunk_row(1)])
        cursor2 = _FakeSearchLogCursor(chunk_rows=[_log_chunk_row(2)])

        settings = Settings()

        keyword_search(settings, cast(psycopg.Connection, _FakeSearchLogConn(cursor1)), "query", caller="agent_alpha")
        keyword_search(settings, cast(psycopg.Connection, _FakeSearchLogConn(cursor2)), "query", caller="human_beta")

        insert1 = [p for s, p in cursor1.executed if "INSERT INTO search_log" in s][0]
        insert2 = [p for s, p in cursor2.executed if "INSERT INTO search_log" in s][0]

        caller1 = insert1[2]
        caller2 = insert2[2]

        assert caller1 == "agent_alpha"
        assert caller2 == "human_beta"
        assert caller1 != caller2

    def test_search_query_does_not_execute_delete(self):
        """Interactive search path performs only INSERT, not DELETE, avoiding lock contention."""
        cursor = _FakeSearchLogCursor(chunk_rows=[_log_chunk_row(1)])
        settings = Settings()
        settings.search_log_retention_days = 14

        keyword_search(settings, cast(psycopg.Connection, _FakeSearchLogConn(cursor)), "query")

        delete_calls = [s for s, _ in cursor.executed if "DELETE FROM search_log" in s]
        assert len(delete_calls) == 0

    def test_retention_pruning_executed_when_configured(self):
        """Spec 5 & Design Finding: Periodic maintenance prune_search_log executes retention DELETE."""
        cursor = _FakeSearchLogCursor()
        conn = cast(psycopg.Connection, _FakeSearchLogConn(cursor))

        deleted = prune_search_log(conn, retention_days=14)

        assert deleted == 5
        delete_calls = [s for s, _ in cursor.executed if "DELETE FROM search_log" in s]
        assert len(delete_calls) == 1
        assert "created_at < NOW() -" in delete_calls[0]

    def test_production_transaction_context_manager_branch_success(self):
        """Cover conn.transaction() context manager success branch in _log_search."""
        cursor = _FakeSearchLogCursor(chunk_rows=[_log_chunk_row(55, "tx test content", 0.92)])
        conn = _FakeTxConn(cursor)
        settings = Settings()

        hits = keyword_search(settings, cast(psycopg.Connection, conn), "tx search query", top_k=2)

        assert len(hits) == 1
        assert conn.tx_events == ["enter_tx", "commit_tx"]
        assert conn.last_tx is not None and conn.last_tx.entered and conn.last_tx.exited
        insert_calls = [p for s, p in cursor.executed if "INSERT INTO search_log" in s]
        assert len(insert_calls) == 1
        assert insert_calls[0][0] == "tx search query"

    def test_production_transaction_context_manager_branch_error_rollback(self, caplog):
        """Cover conn.transaction() context manager failure/rollback branch in _log_search."""
        cursor = _FakeSearchLogCursor(
            chunk_rows=[_log_chunk_row(55, "tx test content", 0.92)],
            raise_on_insert=True,
        )
        conn = _FakeTxConn(cursor)
        settings = Settings()

        with caplog.at_level("WARNING", logger="shiori.search"):
            hits = keyword_search(settings, cast(psycopg.Connection, conn), "tx failed query", top_k=2)

        assert len(hits) == 1
        assert conn.tx_events == ["enter_tx", "rollback_tx:Error"]
        assert conn.last_tx is not None and conn.last_tx.exception_handled
        assert "Failed to write search log" in caplog.text

    def test_mcp_tools_pass_caller_mcp(self, monkeypatch):
        """MCP tool handlers pass caller='mcp' to semantic_search and keyword_search."""
        from shiori.tools import search as tools_search

        executed_callers = []

        def mock_keyword_search(*args, **kwargs):
            executed_callers.append(kwargs.get("caller"))
            return []

        def mock_semantic_search(*args, **kwargs):
            executed_callers.append(kwargs.get("caller"))
            return []

        @contextlib.contextmanager
        def mock_conn():
            yield _FakeSearchLogConn(_FakeSearchLogCursor())

        monkeypatch.setattr(tools_search.search, "keyword_search", mock_keyword_search)
        monkeypatch.setattr(tools_search.search, "semantic_search", mock_semantic_search)
        monkeypatch.setattr(tools_search, "_conn", mock_conn)
        monkeypatch.setattr(tools_search, "_get_embedder", lambda: _FakeEmbedder())

        tools_search.semantic_search(query="mcp vector")
        tools_search.keyword_search(query="mcp kw")

        assert executed_callers == ["mcp", "mcp"]
