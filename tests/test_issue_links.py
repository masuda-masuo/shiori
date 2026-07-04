
"""Tests for shiori_issue_links (issue #97)."""

from __future__ import annotations

from shiori.mcp_server import _extract_refs


class TestExtractRefs:
    """_extract_refs の振る舞い。"""

    def test_closes_pattern(self):
        refs = _extract_refs("Closes #42 and Fixes #43")
        assert len(refs) == 2
        assert {"issue_no": 42, "type": "closes"} in refs
        assert {"issue_no": 43, "type": "closes"} in refs

    def test_closes_variant(self):
        refs = _extract_refs("This will close #1, fix #2, resolve #3")
        assert len(refs) == 3
        assert all(r["type"] == "closes" for r in refs)

    def test_duplicate_pattern(self):
        refs = _extract_refs("Duplicate of #10")
        assert refs == [{"issue_no": 10, "type": "duplicate"}]

    def test_refs_pattern(self):
        refs = _extract_refs("Refs #20, see #30, related to #40")
        assert len(refs) == 3
        assert all(r["type"] == "refs" for r in refs)

    def test_mention_pattern(self):
        refs = _extract_refs("Discussed in #99")
        assert refs == [{"issue_no": 99, "type": "mention"}]

    def test_closes_takes_precedence(self):
        """closes overrides mention for same number."""
        refs = _extract_refs("closes #42 ... also see #42")
        types = {r["type"] for r in refs if r["issue_no"] == 42}
        assert types == {"closes"}

    def test_empty_text(self):
        assert _extract_refs(None) == []
        assert _extract_refs("") == []

    def test_ignores_self_reference(self):
        refs = _extract_refs("See #42 for details")
        assert {"issue_no": 42, "type": "refs"} in refs

    def test_github_url_not_matched_by_mention(self):
        """Mention regex with negative lookbehind ignores # after letters.
        But regular #123 is still caught."""
        refs = _extract_refs("See https://example.com/page#anchor and #55")
        assert len(refs) >= 1
        assert {"issue_no": 55, "type": "mention"} in refs

    def test_github_url_closes(self):
        refs = _extract_refs("Fix https://github.com/o/r/issues/42")
        assert len(refs) == 1
        assert refs[0] == {"issue_no": 42, "type": "closes"}

    def test_github_url_duplicate(self):
        refs = _extract_refs("Duplicate of https://github.com/o/r/pull/10")
        assert refs == [{"issue_no": 10, "type": "duplicate"}]

    def test_github_url_refs(self):
        refs = _extract_refs("See https://github.com/o/r/issues/30")
        assert refs == [{"issue_no": 30, "type": "refs"}]

    def test_github_url_with_hyphenated_repo(self):
        refs = _extract_refs("See https://github.com/my-org/my-repo/issues/42")
        assert refs == [{"issue_no": 42, "type": "refs"}]
