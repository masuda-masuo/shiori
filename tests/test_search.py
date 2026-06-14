"""search モジュールのユニットテスト（issue #41）。"""

from __future__ import annotations

from shiori.search import _sort_hits


class TestSortHits:
    """_sort_hits の振る舞い。"""

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
