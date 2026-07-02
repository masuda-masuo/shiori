"""Unit tests for the search module (issue #41, #69)."""

from __future__ import annotations

from shiori.search import _rank_candidates

# _RESULT_COLS indices (must match constants in search.py)
_COL_SOURCE_TYPE = 1
_COL_STATE = 9
_COL_UPDATED_AT = 13


def _make_row(source_type, state=None, updated_at=None, created_at=None):
    """Build a mock row tuple matching _RESULT_COLS."""
    from datetime import datetime, timezone
    return (
        1, source_type, "test/repo", "dummy/path", None, None,
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
