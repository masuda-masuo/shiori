"""search モジュールのユニットテスト（issue #41, #69）。"""

from __future__ import annotations

from shiori.search import _rank_candidates, _sort_hits

# _RESULT_COLS のインデックス（search.py の定数と一致）
_COL_SOURCE_TYPE = 1
_COL_STATE = 9
_COL_CREATED_AT = 12
_COL_UPDATED_AT = 13


def _make_row(source_type, state=None, updated_at=None, created_at=None):
    """_RESULT_COLS に合わせたモック row タプルを作る。

    row = (id, source_type, repo, path, issue_no, comment_id, language,
           heading_path, content, state, author, line, created_at, updated_at, url)
    """
    from datetime import datetime, timezone
    return (
        1,                           # id
        source_type,                 # source_type
        "test/repo",                 # repo
        "dummy/path",                # path
        None,                        # issue_no
        None,                        # comment_id
        "ja",                        # language
        "heading",                   # heading_path
        "content text",              # content
        state,                       # state
        "author",                    # author
        None,                        # line
        created_at,                  # created_at
        updated_at,                  # updated_at
        "https://example.com",       # url
    )


# _sort_hits tests

class TestSortHits:
    """_sort_hits (backward compat)."""

    def _make_hits(self):
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


# _rank_candidates tests (issue #69)

class TestRankCandidates:
    """source-aware pool-stage composite ranking."""

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
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("doc", state=None, updated_at=ts_old), 2: _make_row("doc", state=None, updated_at=ts_new)}
        ranked = [(1, 0.9), (2, 0.5)]
        result, method = _rank_candidates(ranked, rows_by_id, sort_by="updated_at")
        assert [rid for rid, _ in result] == [1, 2]
        assert method == "rrf+updated_at"

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

    def test_mixed_sources_score_primary(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("doc", state=None, updated_at=ts), 2: _make_row("issue", state="open", updated_at=ts), 3: _make_row("code", state=None, updated_at=ts)}
        ranked = [(1, 0.3), (2, 0.9), (3, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [2, 3, 1]

    def test_mixed_sources_tie_break_open_issue_beats_doc(self):
        """At equal score, primary source comes before secondary (sentinel protection).
        Primary sources (doc/code) use max sentinel values for tie-break elements,
        preventing secondary state/updated_at from pushing them back unfairly.
        """
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("doc", state=None, updated_at=ts), 2: _make_row("issue", state="open", updated_at=ts)}
        ranked = [(1, 0.5), (2, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [1, 2]

    def test_sort_order_asc(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("doc", state=None, updated_at=ts), 2: _make_row("doc", state=None, updated_at=ts)}
        ranked = [(1, 0.3), (2, 0.8)]
        result, _ = _rank_candidates(ranked, rows_by_id, sort_order="asc")
        assert [rid for rid, _ in result] == [1, 2]

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

    def test_missing_row(self):
        ranked = [(999, 0.5)]
        result, _ = _rank_candidates(ranked, {})
        assert result == [(999, 0.5)]

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

    def test_ranking_method_score(self):
        result, method = _rank_candidates([], {})
        assert method == "rrf"

    def test_ranking_method_updated_at(self):
        result, method = _rank_candidates([], {}, sort_by="updated_at")
        assert method == "rrf+updated_at"

    def test_ranking_method_created_at(self):
        result, method = _rank_candidates([], {}, sort_by="created_at")
        assert method == "rrf+created_at"

    def test_pool_stage_allows_newer_closed_to_enter_top_k(self):
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_mid = datetime(2026, 3, 15, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("issue", state="closed", updated_at=ts_old), 2: _make_row("issue", state="closed", updated_at=ts_mid), 3: _make_row("issue", state="open", updated_at=ts_old), 4: _make_row("issue", state="open", updated_at=ts_new)}
        ranked = [(1, 0.5), (2, 0.5), (3, 0.5), (4, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [4, 3, 2, 1]
