"""Decision-comment ranking boost tests (issue #404).

Unit tests on _rank_candidates (sort-key-only 5% boost) plus integration
tests through the real search functions (keyword_search / semantic_search)
with DB fixtures (issue_items bodies + chunks rows) served by a fake cursor.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import cast

import psycopg
import pytest

from shiori.config import Settings
from shiori.embedding import Embedder
from shiori.search import (
    DECISION_BOOST,
    _rank_candidates,
    keyword_search,
    semantic_search,
)


def _ts(year, month, day):
    """TZ-aware timestamp for updated_at tie-break fixtures."""
    return datetime.fromisoformat(f"{year:04d}-{month:02d}-{day:02d}T00:00:00+00:00")


def _make_row(
    source_type,
    issue_no=None,
    comment_id=None,
    state=None,
    updated_at=None,
    content="content text",
):
    """Build a mock _RESULT_COLS row tuple (16 cols, no score)."""
    return (
        1, source_type, "test/repo", "dummy/path", issue_no, comment_id, None,
        "ja", "heading", content, state, "author", None,
        None, updated_at, "https://example.com",
    )


# \u2500\u2500 _rank_candidates unit tests (issue #404) \u2500\u2500

class TestDecisionBoostRankCandidates:
    """Sort-key-only decision boost applied to issue chunks in score/desc mode."""

    def test_decision_boost_constant(self):
        assert DECISION_BOOST == 1.05

    def test_decision_within_5pct_overtakes_non_decision(self):
        """A qualifying chunk within 5% of a non-qualifying one overtakes it."""
        ts = _ts(2026, 6, 10)
        rows_by_id = {
            1: _make_row("issue", issue_no=1, comment_id=10, state="open", updated_at=ts),  # decision comment
            2: _make_row("issue", issue_no=1, comment_id=0, state="open", updated_at=ts),  # issue body
        }
        ranked = [(1, 0.48), (2, 0.50)]
        # Without the set: no overtake (plain relevance order)
        base, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in base] == [2, 1]
        # With the set: 0.48 * 1.05 = 0.504 > 0.50 -> overtake
        boosted, _ = _rank_candidates(ranked, rows_by_id, decision_ids={1})
        assert [rid for rid, _ in boosted] == [1, 2]

    def test_decision_beyond_5pct_does_not_overtake(self):
        """A qualifying chunk more than 5% behind does NOT overtake."""
        ts = _ts(2026, 6, 10)
        rows_by_id = {
            1: _make_row("issue", issue_no=1, comment_id=10, state="open", updated_at=ts),
            2: _make_row("issue", issue_no=1, comment_id=0, state="open", updated_at=ts),
        }
        ranked = [(1, 0.45), (2, 0.50)]
        # 0.45 * 1.05 = 0.4725 < 0.50 -> no overtake
        result, _ = _rank_candidates(ranked, rows_by_id, decision_ids={1})
        assert [rid for rid, _ in result] == [2, 1]

    def test_decision_set_leaves_doc_code_ordering_identical(self):
        """doc/code ordering is byte-identical with and without a decision set."""
        ts = _ts(2026, 6, 10)
        rows_by_id = {
            1: _make_row("doc", updated_at=ts),
            2: _make_row("code", updated_at=ts),
            3: _make_row("doc", updated_at=ts),
        }
        ranked = [(1, 0.3), (2, 0.8), (3, 0.5)]
        base, method_base = _rank_candidates(ranked, rows_by_id)
        with_set, method_set = _rank_candidates(
            ranked, rows_by_id, decision_ids={1, 2, 3}
        )
        assert with_set == base
        assert method_set == method_base

    def test_decision_no_boost_under_asc(self):
        """No bump under sort_order='asc'."""
        ts = _ts(2026, 6, 10)
        rows_by_id = {
            1: _make_row("issue", issue_no=1, comment_id=10, state="open", updated_at=ts),
            2: _make_row("issue", issue_no=1, comment_id=0, state="open", updated_at=ts),
        }
        ranked = [(1, 0.48), (2, 0.49)]
        result, _ = _rank_candidates(
            ranked, rows_by_id, sort_order="asc", decision_ids={1}
        )
        # asc without boost: 0.48 (comment) then 0.49 (body).
        # A wrongly applied boost (0.504) would flip this order.
        assert [rid for rid, _ in result] == [1, 2]

    def test_decision_no_boost_under_non_score_sort_by(self):
        """No bump under a non-score sort_by."""
        ts = _ts(2026, 6, 10)
        rows_by_id = {
            1: _make_row("issue", issue_no=1, comment_id=10, state="open", updated_at=ts),
            2: _make_row("issue", issue_no=1, comment_id=0, state="open", updated_at=ts),
        }
        ranked = [(1, 0.45), (2, 0.46)]
        result, _ = _rank_candidates(
            ranked, rows_by_id, sort_by="updated_at", decision_ids={1}
        )
        # No boost: 0.46 (body) first. A wrongly applied boost
        # (0.4725 > 0.46) would flip this order.
        assert [rid for rid, _ in result] == [2, 1]

    def test_decision_never_boosts_pr_review_rows(self):
        """Only source_type='issue' chunks can qualify; pr_review is untouched."""
        ts = _ts(2026, 6, 10)
        rows_by_id = {
            1: _make_row("pr_review", issue_no=1, comment_id=10, state="open", updated_at=ts),
            2: _make_row("issue", issue_no=1, comment_id=0, state="open", updated_at=ts),
        }
        ranked = [(1, 0.45), (2, 0.46)]
        result, _ = _rank_candidates(ranked, rows_by_id, decision_ids={1})
        assert [rid for rid, _ in result] == [2, 1]


# \u2500\u2500 Integration through the real search path (DB fixtures) \u2500\u2500

# issue_items bodies (DB fixture; comment_id 0 = thread body)
_ISSUE_ITEMS = {
    (0, "issue body text"),
    (10, "## \u8a2d\u8a08\u5224\u65ad\nSome decision text"),
    (20, "## \u518d\u96c6\u8a08\u306e\u7d50\u679c\u3001\u6c7a\u5b9a\u3092\u4fdd\u7559\n"),
}


class _FakeDecisionCursor:
    """Serves the chunks SELECTs and emulates the issue_items regex filter.

    The issue_items query is emulated by applying the passed regex (Postgres
    `body ~ <pattern>`, string-start anchored) to the fixture bodies -- the
    same semantics as Postgres ARE for these patterns.
    """

    def __init__(self, chunk_rows=None, vec_rows=None, kw_rows=None):
        self.chunk_rows = chunk_rows
        self.vec_rows = vec_rows
        self.kw_rows = kw_rows
        self.executed: list[str] = []
        self._last = ""
        self._params = None
        self.decision_results: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self._last = sql
        self._params = params

    def fetchall(self):
        if "unnest" in self._last and "issue_items" in self._last:
            if self._params is None:
                return []
            # Emulate: which (repo, issue_no, comment_id) triples qualify?
            pattern = self._params[3]
            self.decision_results = [
                ("test/repo", 1, cid)
                for cid, body in _ISSUE_ITEMS
                if re.search(pattern, body)
            ]
            return list(self.decision_results)
        if "embedding <=" in self._last:
            return list(self.vec_rows or [])
        if "pgroonga_score" in self._last:
            return list(self.kw_rows if self.kw_rows is not None else (self.chunk_rows or []))
        return list(self.chunk_rows or [])


class _FakeDecisionConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class _FakeEmbedder:
    def embed_query(self, query):
        return [0.1, 0.2, 0.3]


def _chunk_row(rid, comment_id, content, score):
    """A full _RESULT_COLS + score row (17 cols) as returned by the SELECTs."""
    return (
        rid, "issue", "test/repo", "dummy/path", 1, comment_id, None,
        "ja", "heading", content, "open", "author", None,
        None, None, "https://example.com", score,
    )


def _thread_fixture_rows():
    return [
        _chunk_row(1, 0, "issue body text", 0.5),           # issue body
        _chunk_row(2, 10, "decision comment text", 0.5),    # "## \u8a2d\u8a08\u5224\u65ad"
        _chunk_row(3, 20, "non-qualifying comment text", 0.5),  # "\u6c7a\u5b9a" words only
    ]


class TestDecisionBoostIntegration:
    """Real search path (keyword_search / semantic_search) with DB fixtures."""

    def test_decision_comment_ranks_above_issue_body_keyword(self):
        """Tied decision comment overtakes the issue body; scores unchanged."""
        cursor = _FakeDecisionCursor(chunk_rows=_thread_fixture_rows())
        hits = keyword_search(
            Settings(), cast(psycopg.Connection, _FakeDecisionConn(cursor)), "\u8a2d\u8a08", top_k=10
        )

        assert [h["snippet"] for h in hits] == [
            "decision comment text",
            "issue body text",
            "non-qualifying comment text",
        ]
        # Returned scores are unchanged -- the boost is sort-key only
        assert [h["score"] for h in hits] == [0.5, 0.5, 0.5]
        # ONE qualifying query for the pool
        assert sum("unnest" in sql for sql in cursor.executed) == 1

    def test_decision_comment_ranks_above_issue_body_semantic(self):
        """Same outcome through semantic_search (RRF ties, boost breaks them)."""
        # vec ranks: body 0, decision 1; kw ranks: decision 0, body 1
        # -> identical RRF scores; the boost must break the tie
        vec_rows = [
            _chunk_row(1, 0, "issue body text", 0.9),
            _chunk_row(2, 10, "decision comment text", 0.9),
        ]
        kw_rows = [
            _chunk_row(2, 10, "decision comment text", 0.5),
            _chunk_row(1, 0, "issue body text", 0.5),
        ]
        cursor = _FakeDecisionCursor(vec_rows=vec_rows, kw_rows=kw_rows)

        hits = semantic_search(
            Settings(),
            cast(psycopg.Connection, _FakeDecisionConn(cursor)),
            cast(Embedder, _FakeEmbedder()),
            "\u8a2d\u8a08", top_k=10,
        )
        assert [h["snippet"] for h in hits] == [
            "decision comment text",
            "issue body text",
        ]
        # Scores unchanged (RRF values, not boosted)
        assert hits[0]["score"] == pytest.approx(1 / 61 + 1 / 62, abs=1e-4)
        assert sum("unnest" in sql for sql in cursor.executed) == 1

    def test_decision_qualifying_set_excludes_decided_word_heading(self):
        """"## \u518d\u96c6\u8a08\u306e\u7d50\u679c\u3001\u6c7a\u5b9a\u3092\u4fdd\u7559" does not qualify."""
        cursor = _FakeDecisionCursor(chunk_rows=_thread_fixture_rows())
        keyword_search(Settings(), cast(psycopg.Connection, _FakeDecisionConn(cursor)), "\u8a2d\u8a08", top_k=10)
        # Only the "## \u8a2d\u8a08\u5224\u65ad" comment qualifies: heading-anchored
        # pattern requires \u8a2d\u8a08 immediately before \u5224\u65ad/\u78ba\u5b9a/\u6c7a\u5b9a
        assert cursor.decision_results == [("test/repo", 1, 10)]

    def test_decision_non_default_sort_skips_qualifying_query(self):
        """sort_order=asc: no bump and no qualifying query at all."""
        cursor = _FakeDecisionCursor(chunk_rows=_thread_fixture_rows())
        hits = keyword_search(
            Settings(), cast(psycopg.Connection, _FakeDecisionConn(cursor)), "\u8a2d\u8a08", top_k=10,
            sort_order="asc",
        )
        assert [h["snippet"] for h in hits] == [
            "issue body text",
            "decision comment text",
            "non-qualifying comment text",
        ]
        assert not any("unnest" in sql for sql in cursor.executed)
