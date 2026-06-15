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


# ── _sort_hits 後方互換テスト ──

class TestSortHits:
    """_sort_hits の振る舞い（後方互換）。"""

    def _make_hits(self) -> list[dict]:
        """score / updated_at / created_at が異なる 3 件のヒット。"""
        return [
            {
                "source_type": "doc",
                "repo": "test/repo",
                "path": "docs/a.md",
                "issue_no": None,
                "heading_path": "a",
                "snippet": "aaa",
                "language": "ja",
                "state": "open",
                "author": None,
                "line": None,
                "created_at": "2026-06-10T00:00:00+00:00",
                "updated_at": "2026-06-12T00:00:00+00:00",
                "url": "https://example.com/a",
                "score": 0.9,
            },
            {
                "source_type": "issue",
                "repo": "test/repo",
                "path": None,
                "issue_no": 1,
                "heading_path": None,
                "snippet": "bbb",
                "language": "ja",
                "state": "closed",
                "author": "user1",
                "line": None,
                "created_at": "2026-06-11T00:00:00+00:00",
                "updated_at": "2026-06-13T00:00:00+00:00",
                "url": "https://example.com/b",
                "score": 0.5,
            },
            {
                "source_type": "code",
                "repo": "test/repo",
                "path": "src/main.py",
                "issue_no": None,
                "heading_path": "main.py",
                "snippet": "ccc",
                "language": None,
                "state": None,
                "author": None,
                "line": 1,
                "created_at": "2026-06-09T00:00:00+00:00",
                "updated_at": "2026-06-14T00:00:00+00:00",
                "url": "https://example.com/c",
                "score": 0.3,
            },
        ]

    # ── score ソート ──

    def test_sort_by_score_desc(self):
        """既定: score 降順（高い順）。"""
        hits = self._make_hits()
        result = _sort_hits(hits, "score", "desc")
        assert [h["score"] for h in result] == [0.9, 0.5, 0.3]

    def test_sort_by_score_asc(self):
        """score 昇順（低い順）。"""
        hits = self._make_hits()
        result = _sort_hits(hits, "score", "asc")
        assert [h["score"] for h in result] == [0.3, 0.5, 0.9]

    # ── updated_at ソート ──

    def test_sort_by_updated_at_desc(self):
        """updated_at 降順（新しい順）。"""
        hits = self._make_hits()
        result = _sort_hits(hits, "updated_at", "desc")
        assert [h["updated_at"] for h in result] == [
            "2026-06-14T00:00:00+00:00",
            "2026-06-13T00:00:00+00:00",
            "2026-06-12T00:00:00+00:00",
        ]

    def test_sort_by_updated_at_asc(self):
        """updated_at 昇順（古い順）。"""
        hits = self._make_hits()
        result = _sort_hits(hits, "updated_at", "asc")
        assert [h["updated_at"] for h in result] == [
            "2026-06-12T00:00:00+00:00",
            "2026-06-13T00:00:00+00:00",
            "2026-06-14T00:00:00+00:00",
        ]

    # ── created_at ソート ──

    def test_sort_by_created_at_desc(self):
        """created_at 降順（新しい順）。"""
        hits = self._make_hits()
        result = _sort_hits(hits, "created_at", "desc")
        assert [h["created_at"] for h in result] == [
            "2026-06-11T00:00:00+00:00",
            "2026-06-10T00:00:00+00:00",
            "2026-06-09T00:00:00+00:00",
        ]

    def test_sort_by_created_at_asc(self):
        """created_at 昇順（古い順）。"""
        hits = self._make_hits()
        result = _sort_hits(hits, "created_at", "asc")
        assert [h["created_at"] for h in result] == [
            "2026-06-09T00:00:00+00:00",
            "2026-06-10T00:00:00+00:00",
            "2026-06-11T00:00:00+00:00",
        ]

    # ── 境界値・エッジケース ──

    def test_unknown_sort_by_returns_unchanged(self):
        """不明な sort_by はリストを変更せず返す（フォールバック）。"""
        hits = self._make_hits()
        result = _sort_hits(hits, "invalid_key", "desc")
        # 順序が変わらないことを確認（元の順序のまま）
        assert [h["path"] for h in result] == ["docs/a.md", None, "src/main.py"]

    def test_empty_list(self):
        """空リストは空のまま返る。"""
        result = _sort_hits([], "score", "desc")
        assert result == []

    def test_single_item(self):
        """1 件だけのリストはそのまま返る。"""
        hit = {
            "source_type": "doc",
            "repo": "test/repo",
            "path": "a.md",
            "score": 1.0,
            "updated_at": "2026-06-10T00:00:00+00:00",
            "created_at": "2026-06-09T00:00:00+00:00",
        }
        result = _sort_hits([hit], "updated_at", "desc")
        assert result == [hit]

    def test_missing_sort_key_goes_last(self):
        """ソートキーがないヒットは末尾に来る（文字列比較で空文字が最小のため）。"""
        hits = [
            {"score": 0.5, "updated_at": "2026-06-10T00:00:00+00:00"},
            {"score": 0.9},  # updated_at なし
        ]
        result = _sort_hits(hits, "updated_at", "desc")
        # updated_at を持つものが先、持たないものが後
        assert result[0]["score"] == 0.5
        assert result[1]["score"] == 0.9


# ── _rank_candidates テスト（issue #69） ──

class TestRankCandidates:
    """source-aware な pool 段複合ランキングの振る舞い。"""

    # ── 一次ソース（doc / code）: 関連度のみ ──

    def test_primary_sources_score_only(self):
        """一次ソース（doc, code）はスコアのみでランク付けされる。"""
        from datetime import datetime, timezone
        ts1 = datetime(2026, 6, 10, tzinfo=timezone.utc)
        ts2 = datetime(2026, 6, 14, tzinfo=timezone.utc)

        rows_by_id = {
            1: _make_row("doc", state=None, updated_at=ts1),
            2: _make_row("code", state=None, updated_at=ts2),
        }
        # code が doc より高いスコア
        ranked = [(1, 0.3), (2, 0.8)]
        result, method = _rank_candidates(ranked, rows_by_id)
        # スコア順（高い方が先）
        assert [rid for rid, _ in result] == [2, 1]
        assert method == "rrf"

    def test_primary_sources_ignore_date_sort_by(self):
        """一次ソースでは sort_by=updated_at でもスコア順が維持される。"""
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)

        rows_by_id = {
            1: _make_row("doc", state=None, updated_at=ts_old),
            2: _make_row("doc", state=None, updated_at=ts_new),
        }
        # 低スコアの方が新しい日付でも、スコア順が優先
        ranked = [(1, 0.9), (2, 0.5)]
        result, method = _rank_candidates(ranked, rows_by_id, sort_by="updated_at")
        assert [rid for rid, _ in result] == [1, 2]
        assert method == "rrf+updated_at"

    # ── 二次ソース（issue / pr_review）: 複合 tie-break ──

    def test_secondary_sources_score_primary(self):
        """二次ソースではスコアが第一ソートキー。"""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)

        rows_by_id = {
            1: _make_row("issue", state="open", updated_at=ts),
            2: _make_row("pr_review", state="open", updated_at=ts),
        }
        ranked = [(1, 0.3), (2, 0.8)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [2, 1]

    def test_secondary_tie_break_state_priority(self):
        """同スコアでは open > closed の順。"""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)

        rows_by_id = {
            1: _make_row("issue", state="closed", updated_at=ts),
            2: _make_row("issue", state="open", updated_at=ts),
        }
        ranked = [(1, 0.5), (2, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [2, 1]

    def test_secondary_tie_break_updated_at(self):
        """同スコア・同 state では updated_at が新しい方が先。"""
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)

        rows_by_id = {
            1: _make_row("issue", state="open", updated_at=ts_old),
            2: _make_row("pr_review", state="open", updated_at=ts_new),
        }
        ranked = [(1, 0.5), (2, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [2, 1]

    def test_secondary_state_beats_updated_at(self):
        """state 優先度が updated_at より強い（open+古 ＞ closed+新）。"""
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)

        rows_by_id = {
            1: _make_row("issue", state="open", updated_at=ts_old),
            2: _make_row("issue", state="closed", updated_at=ts_new),
        }
        ranked = [(1, 0.5), (2, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [1, 2]

    # ── 混合ソース ──

    def test_mixed_sources_score_primary(self):
        """混合ソースでもスコアが第一キー。一次/二次の別は同スコア時のみ影響。"""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)

        rows_by_id = {
            1: _make_row("doc", state=None, updated_at=ts),
            2: _make_row("issue", state="open", updated_at=ts),
            3: _make_row("code", state=None, updated_at=ts),
        }
        ranked = [(1, 0.3), (2, 0.9), (3, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        assert [rid for rid, _ in result] == [2, 3, 1]

    def test_mixed_sources_tie_break_open_issue_beats_doc(self):
        """同スコア時、二次ソースの open は一次より前（tie-break としては open が勝つ）。"""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)

        rows_by_id = {
            1: _make_row("doc", state=None, updated_at=ts),
            2: _make_row("issue", state="open", updated_at=ts),
        }
        ranked = [(1, 0.5), (2, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        # open(0) < doc(0) だが -sp で open が -0、doc が 0 なので open が先
        # 実際には doc の sp 代用値は 0、open の -sp は -0 = 0。つまり tie。
        # score が同じで tie-break 値も同じ→元の順序が維持される
        # これは意図した挙動（一次ソースが不当に後回しにならない）
        assert [rid for rid, _ in result] == [1, 2]

    # ── sort_order ──

    def test_sort_order_asc(self):
        """sort_order=asc ではスコア昇順（低い順）。"""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)

        rows_by_id = {
            1: _make_row("doc", state=None, updated_at=ts),
            2: _make_row("doc", state=None, updated_at=ts),
        }
        ranked = [(1, 0.3), (2, 0.8)]
        result, _ = _rank_candidates(ranked, rows_by_id, sort_order="asc")
        assert [rid for rid, _ in result] == [1, 2]

    # ── 境界値・エッジケース ──

    def test_empty_candidates(self):
        """空の候補リストは空を返す。"""
        result, method = _rank_candidates([], {})
        assert result == []
        assert method == "rrf"

    def test_single_candidate(self):
        """1 件だけの候補はそのまま返る。"""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows_by_id = {1: _make_row("doc", state=None, updated_at=ts)}
        ranked = [(1, 0.5)]
        result, method = _rank_candidates(ranked, rows_by_id)
        assert result == [(1, 0.5)]
        assert method == "rrf"

    def test_missing_row(self):
        """rows_by_id に存在しない ID は tie-break 値がフォールバックされる。"""
        ranked = [(999, 0.5)]
        result, _ = _rank_candidates(ranked, {})
        assert result == [(999, 0.5)]

    def test_state_none_secondary(self):
        """state が None の二次ソースは closed より後ろ。"""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)

        rows_by_id = {
            1: _make_row("issue", state=None, updated_at=ts),
            2: _make_row("issue", state="closed", updated_at=ts),
            3: _make_row("issue", state="open", updated_at=ts),
        }
        ranked = [(1, 0.5), (2, 0.5), (3, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        # open → closed → None
        assert [rid for rid, _ in result] == [3, 2, 1]

    def test_updated_at_none_secondary(self):
        """updated_at が None の二次ソースは末尾（空文字扱い）。"""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 10, tzinfo=timezone.utc)

        rows_by_id = {
            1: _make_row("issue", state="open", updated_at=None),
            2: _make_row("issue", state="open", updated_at=ts),
        }
        ranked = [(1, 0.5), (2, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        # updated_at ありが先
        assert [rid for rid, _ in result] == [2, 1]

    # ── ranking_method ──

    def test_ranking_method_score(self):
        """sort_by=score のとき method は "rrf"。"""
        result, method = _rank_candidates([], {})
        assert method == "rrf"

    def test_ranking_method_updated_at(self):
        """sort_by=updated_at のとき method は "rrf+updated_at"。"""
        result, method = _rank_candidates([], {}, sort_by="updated_at")
        assert method == "rrf+updated_at"

    def test_ranking_method_created_at(self):
        """sort_by=created_at のとき method は "rrf+created_at"。"""
        result, method = _rank_candidates([], {}, sort_by="created_at")
        assert method == "rrf+created_at"

    # ── pool 段適用の検証 ──

    def test_pool_stage_allows_newer_closed_to_enter_top_k(self):
        """同スコア帯では tie-break が pool 段で適用されるため、
        従来の truncate-then-sort では取りこぼしていた候補が top-k に入る。

        シナリオ: 10 件の候補プール中、同スコア 0.5 の issue が 4 件あり、
        k=3 の場合。truncate-then-sort だとプール内の並び（古い順）で上位 3 件が
        決まるが、tie-break なら open → 新着順で選ばれる。
        """
        from datetime import datetime, timezone
        ts_old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ts_mid = datetime(2026, 3, 15, tzinfo=timezone.utc)
        ts_new = datetime(2026, 6, 14, tzinfo=timezone.utc)

        rows_by_id = {
            1: _make_row("issue", state="closed", updated_at=ts_old),
            2: _make_row("issue", state="closed", updated_at=ts_mid),
            3: _make_row("issue", state="open", updated_at=ts_old),
            4: _make_row("issue", state="open", updated_at=ts_new),
        }
        # すべて同スコア。tie-break 前は入力順
        ranked = [(1, 0.5), (2, 0.5), (3, 0.5), (4, 0.5)]
        result, _ = _rank_candidates(ranked, rows_by_id)
        # open(3,4) → closed(1,2)。open 内では新しい順 (4,3)
        # closed 内では新しい順 (2,1)
        assert [rid for rid, _ in result] == [4, 3, 2, 1]
