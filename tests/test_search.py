"""Unit tests for the search module (issue #41, #69)."""

from __future__ import annotations

from shiori.search import _rank_candidates, _sort_hits

# _RESULT_COLS のインデックス（search.py の定数と一致）
_COL_SOURCE_TYPE = 1
_COL_STATE = 9
_COL_UPDATED_AT = 13


def _make_row(source_type, state=None, updated_at=None, created_at=None):
    """Create a mock row tuple matching _RESULT_COLS."""
    from datetime import datetime, timezone
    return (
        1, source_type, "test/repo", "dummy/path", None, None,
        "ja", "heading", "content text", state, "author", None,
        created_at, updated_at, "https://example.com",
    )


# ── _sort_hits 後方互換テスト ──

class TestSortHits:
    """Behavior of _sort_hits (backward compatibility)."""

    def _make_hits(self) -> list[dict]:
        return [
            {"source_type":"doc","repo":"test/repo","path":"docs/a.md","issue_no":None,"heading_path":"a","snippet":"aaa","language":"ja","state":"open","author":None,"line":None,"created_at":"2026-06-10T00:00:00+00:00","updated_at":"2026-06-12T00:00:00+00:00","url":"https://example.com/a","score":0.9},
            {"source_type":"issue","repo":"test/repo","path":None,"issue_no":1,"heading_path":None,"snippet":"bbb","language":"ja","state":"closed","author":"user1","line":None,"created_at":"2026-06-11T00:00:00+00:00","updated_at":"2026-06-13T00:00:00+00:00","url":"https://example.com/b","score":0.5},
            {"source_type":"code","repo":"test/repo","path":"src/main.py","issue_no":None,"heading_path":"main.py","snippet":"ccc","language":None,"state":None,"author":None,"line":1,"created_at":"2026-06-09T00:00:00+00:00","updated_at":"2026-06-14T00:00:00+00:00","url":"https://example.com/c","score":0.3},
        ]

    def test_sort_by_score_desc(self):
        hits = self._make_hits()
        result = _sort_hits(hits, "score", "desc")
        assert [h["score"] for h in result] == [0.9, 0.5, 0.3]

    def test_sort_by_score_asc(self):
        hits = self._make_hits()
        result = _sort_hits(hits, "score", "asc")
        assert [h["score"] for h in result] == [0.3, 0.5, 0.9]

    def test_sort_by_updated_at_desc(self):
        hits = self._make_hits()
        result = _sort_hits(hits, "updated_at", "desc")
        assert [h["updated_at"] for h in result] == ["2026-06-14T00:00:00+00:00","2026-06-13T00:00:00+00:00","2026-06-12T00:00:00+00:00"]

    def test_sort_by_updated_at_asc(self):
        hits = self._make_hits()
        result = _sort_hits(hits, "updated_at", "asc")
        assert [h["updated_at"] for h in result] == ["2026-06-12T00:00:00+00:00","2026-06-13T00:00:00+00:00","2026-06-14T00:00:00+00:00"]

    def test_sort_by_created_at_desc(self):
        hits = self._make_hits()
        result = _sort_hits(hits, "created_at", "desc")
        assert [h["created_at"] for h in result] == ["2026-06-11T00:00:00+00:00","2026-06-10T00:00:00+00:00","2026-06-09T00:00:00+00:00"]

    def test_sort_by_created_at_asc(self):
        hits = self._make_hits()
        result = _sort_hits(hits, "created_at", "asc")
        assert [h["created_at"] for h in result] == ["2026-06-09T00:00:00+00:00","2026-06-10T00:00:00+00:00","2026-06-11T00:00:00+00:00"]

    def test_unknown_sort_by_returns_unchanged(self):
        hits = self._make_hits()
        result = _sort_hits(hits, "invalid_key", "desc")
        assert [h["path"] for h in result] == ["docs/a.md", None, "src/main.py"]

    def test_empty_list(self):
        result = _sort_hits([], "score", "desc")
        assert result == []

    def test_single_item(self):
        hit = {"source_type":"doc","repo":"test/repo","path":"a.md","score":1.0,"updated_at":"2026-06-10T00:00:00+00:00","created_at":"2026-06-09T00:00:00+00:00"}
        result = _sort_hits([hit], "updated_at", "desc")
        assert result == [hit]

    def test_missing_sort_key_goes_last(self):
        hits = [{"score":0.5,"updated_at":"2026-06-10T00:00:00+00:00"},{"score":0.9}]
        result = _sort_hits(hits, "updated_at", "desc")
        assert result[0]["score"] == 0.5
        assert result[1]["score"] == 0.9


# ── _rank_candidates テスト（issue #69） ──

class TestRankCandidates:
    """Behavior of source-aware pool-stage composite ranking."""

    # ── 一次ソース（doc / code）: 関連度のみ ──

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
        """Primary sources maintain score order even with sort_by=updated_at."""
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("doc", state=None, updated_at=ts_old), 2: _make_row("doc", state=None, updated_at=ts_new)}
        ranked = [(1, 0.9), (2, 0.5)]
        result, method = _rank_candidates(ranked, rows_by_id, sort_by="updated_at")
        assert [rid for rid, _ in result] == [1, 2]
        assert method == "rrf"

    # ── 二次ソース（issue / pr_review）: 複合 tie-break ──

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

    # ── created_at は updated_at に集約（review #1） ──

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

    # ── 混合ソース ──

    def test_mixed_sources_score_primary(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("doc", state=None, updated_at=ts), 2: _make_row("issue", state="open", updated_at=ts), 3: _make_row("code", state=None, updated_at=ts)}
        ranked = [(1, 0.3), (2, 0.9), (3, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [2, 3, 1]

    def test_mixed_sources_primary_before_secondary_at_equal_score(self):
        """At equal scores, primary source comes before secondary via sentinel."""
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
        """sort_order=asc inverts the composite key so closed→open, old→new."""
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("issue", state="open", updated_at=ts_new), 2: _make_row("issue", state="closed", updated_at=ts_old)}
        ranked = [(1, 0.5), (2, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id, sort_order="asc")
        # asc: closed→open、古い→新しい
        assert [rid for rid, _ in result] == [2, 1]

    # ── 境界値・エッジケース ──

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
        """IDs not in rows_by_id sink to the bottom (defensive fallback)."""
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

    # ── pool 段適用の検証 ──

    def test_pool_stage_allows_newer_closed_to_enter_top_k(self):
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_mid = datetime(2026, 3, 15, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("issue", state="closed", updated_at=ts_old), 2: _make_row("issue", state="closed", updated_at=ts_mid), 3: _make_row("issue", state="open", updated_at=ts_old), 4: _make_row("issue", state="open", updated_at=ts_new)}
        ranked = [(1, 0.5), (2, 0.5), (3, 0.5), (4, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [4, 3, 2, 1]
