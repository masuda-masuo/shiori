"""Unit tests for shiori_read_issue exclude_noise_bots parameter (issue #44).

Test targets:
- read_issue: exclude_noise_bots=False (default) returns all items
- read_issue: exclude_noise_bots=True filters out bots outside allowlist
- read_issue: ValueError when all items filtered
- read_issue: ValueError when no rows (existing behavior)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shiori.mcp_server import read_issue, settings


# ── Test helpers ──


def _row(
    comment_id: int = 0,
    kind: str = "issue",
    title: str = "Test Issue",
    author: str | None = "human-user",
    is_bot: bool = False,
    state: str = "open",
    path: str | None = None,
    line: int | None = None,
    body: str = "test body",
    url: str = "https://github.com/o/r/issues/1",
    created_at: str | None = "2026-06-14T00:00:00+00:00",
) -> tuple:
    """Build a test row tuple (comment_id, kind, title, author, is_bot, state, path, line, body, url, created_at)."""
    from datetime import datetime
    ca = datetime.fromisoformat(created_at) if created_at else None
    return (comment_id, kind, title, author, is_bot, state, path, line, body, url, ca)


class TestReadIssueExcludeNoiseBots:
    """Behavior of read_issue exclude_noise_bots parameter."""

    def _mock_conn_with_rows(self, rows: list[tuple]) -> MagicMock:
        """Build a mock connection that returns the given rows."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn.cursor.return_value.__enter__.return_value = cursor
        # Ensure with _conn() as conn returns itself
        conn.__enter__.return_value = conn
        return conn

    def test_default_returns_all_items(self):
        """exclude_noise_bots=False (default) returns all items including bots."""
        rows = [
            _row(comment_id=0, author="human-user", is_bot=False),
            _row(comment_id=1, author="allowlisted-bot[bot]", is_bot=True),
            _row(comment_id=2, author="dependabot[bot]", is_bot=True),
        ]
        mock_conn = self._mock_conn_with_rows(rows)

        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            result = read_issue(42, exclude_noise_bots=False)

        assert len(result["items"]) == 3
        assert result["items"][0]["author"] == "human-user"
        assert result["items"][1]["author"] == "allowlisted-bot[bot]"
        assert result["items"][2]["author"] == "dependabot[bot]"

    def test_exclude_noise_bots_filters_non_allowlisted_bots(self, monkeypatch):
        """exclude_noise_bots=True filters only bots outside the allowlist."""
        rows = [
            _row(comment_id=0, author="human-user", is_bot=False),
            _row(comment_id=1, author="allowlisted-bot[bot]", is_bot=True),
            _row(comment_id=2, author="dependabot[bot]", is_bot=True),
        ]
        mock_conn = self._mock_conn_with_rows(rows)

        monkeypatch.setattr(settings, "index_bot_logins",
                            {"allowlisted-bot[bot]", "another-bot[bot]"})
        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            result = read_issue(42, exclude_noise_bots=True)

        assert len(result["items"]) == 2
        assert result["items"][0]["author"] == "human-user"
        assert result["items"][1]["author"] == "allowlisted-bot[bot]"

    def test_exclude_noise_bots_keeps_allowlisted_bot_with_different_case(self, monkeypatch):
        """allowlist match is case-insensitive."""
        rows = [
            _row(comment_id=0, author="Allowlisted-Bot[bot]", is_bot=True),
        ]
        mock_conn = self._mock_conn_with_rows(rows)

        monkeypatch.setattr(settings, "index_bot_logins",
                            {"allowlisted-bot[bot]"})
        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            result = read_issue(42, exclude_noise_bots=True)

        assert len(result["items"]) == 1
        assert result["items"][0]["author"] == "Allowlisted-Bot[bot]"

    def test_exclude_noise_bots_raises_when_all_filtered(self, monkeypatch):
        """Raises ValueError when all items are filtered."""
        rows = [
            _row(comment_id=0, author="dependabot[bot]", is_bot=True),
            _row(comment_id=1, author="ci-bot[bot]", is_bot=True),
        ]
        mock_conn = self._mock_conn_with_rows(rows)

        monkeypatch.setattr(settings, "index_bot_logins",
                            {"allowlisted-bot[bot]"})
        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            with pytest.raises(ValueError, match="all items are bots outside the allowlist"):
                read_issue(42, exclude_noise_bots=True)

    def test_exclude_noise_bots_with_author_none(self, monkeypatch):
        """Bot with author=None does not match allowlist and is excluded."""
        rows = [
            _row(comment_id=0, author="human-user", is_bot=False),
            _row(comment_id=1, author=None, is_bot=True),
        ]
        mock_conn = self._mock_conn_with_rows(rows)

        monkeypatch.setattr(settings, "index_bot_logins",
                            {"allowlisted-bot[bot]"})
        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            result = read_issue(42, exclude_noise_bots=True)

        # Bot with author=None is excluded, human remains
        assert len(result["items"]) == 1
        assert result["items"][0]["author"] == "human-user"

    def test_no_rows_raises_value_error(self):
        """Raises ValueError when there are 0 rows (existing behavior)."""
        mock_conn = self._mock_conn_with_rows([])

        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            with pytest.raises(ValueError, match="is not indexed"):
                read_issue(42)


class TestReadIssueNumbers:
    """Behavior of read_issue numbers parameter (batch fetch, issue #86)."""

    def _make_result(self, number: int, title: str = "Test") -> dict:
        return {
            "repo": "o/r",
            "number": number,
            "kind": "issue",
            "title": title,
            "state": "open",
            "url": f"https://github.com/o/r/issues/{number}",
            "items": [
                {
                    "author": "user",
                    "is_bot": False,
                    "kind": "issue",
                    "created_at": "2026-06-14T00:00:00+00:00",
                    "body": f"body of {number}",
                    "url": f"https://github.com/o/r/issues/{number}",
                }
            ],
        }

    def test_batch_all_success(self):
        """Fetches multiple issue numbers all successfully."""
        with patch("shiori.mcp_server._read_issue_single") as mock, \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            mock.side_effect = [
                self._make_result(42, "issue 42"),
                self._make_result(43, "issue 43"),
            ]
            result = read_issue(numbers=[42, 43])

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["number"] == 42
        assert result[0]["status"] == "ok"
        assert result[0]["title"] == "issue 42"
        assert result[1]["number"] == 43
        assert result[1]["status"] == "ok"
        assert result[1]["title"] == "issue 43"

    def test_batch_partial_unindexed(self):
        """Mixed indexed/unindexed numbers: returns success/error per item."""
        with patch("shiori.mcp_server._read_issue_single") as mock, \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            mock.side_effect = [
                self._make_result(42, "issue 42"),
                ValueError("#43 is not indexed (has ingest been run?)"),
                self._make_result(44, "issue 44"),
            ]
            result = read_issue(numbers=[42, 43, 44])

        assert isinstance(result, list)
        assert len(result) == 3
        assert result[0]["status"] == "ok"
        assert result[0]["number"] == 42
        assert result[1]["status"] == "error"
        assert result[1]["number"] == 43
        assert "is not indexed" in result[1]["error"]
        assert result[2]["status"] == "ok"
        assert result[2]["number"] == 44

    def test_batch_empty_array(self):
        """Empty list returns an empty list."""
        with patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            result = read_issue(numbers=[])

        assert isinstance(result, list)
        assert result == []

    def test_batch_duplicate_numbers(self):
        """Duplicate numbers are processed individually."""
        with patch("shiori.mcp_server._read_issue_single") as mock, \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            mock.side_effect = [
                self._make_result(42, "issue 42"),
                self._make_result(42, "issue 42"),
            ]
            result = read_issue(numbers=[42, 42])

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["number"] == 42
        assert result[0]["status"] == "ok"
        assert result[1]["number"] == 42
        assert result[1]["status"] == "ok"

    def test_batch_applies_exclude_noise_bots(self):
        """exclude_noise_bots applied to all fetches in batch."""
        with patch("shiori.mcp_server._read_issue_single") as mock, \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            mock.return_value = self._make_result(42, "issue 42")
            read_issue(numbers=[42, 43], exclude_noise_bots=True)

        assert mock.call_count == 2
        for call_args in mock.call_args_list:
            # _read_issue_single(target, number, exclude_noise_bots) — positional
            assert call_args[0][2] is True
            assert call_args[0][0] == "o/r"

    def test_batch_passes_repo_to_single(self):
        """repo is correctly passed to _resolve_repo and _read_issue_single."""
        with patch("shiori.mcp_server._read_issue_single") as mock_single, \
             patch("shiori.mcp_server._resolve_repo") as mock_resolve:
            mock_resolve.return_value = "my/repo"
            mock_single.return_value = self._make_result(42, "issue 42")
            read_issue(numbers=[42], repo="my/repo")

        mock_resolve.assert_called_once_with("my/repo")
        mock_single.assert_called_once_with("my/repo", 42, False)

    def test_batch_all_errors(self):
        """All errors returns error items without raising."""
        with patch("shiori.mcp_server._read_issue_single") as mock, \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            mock.side_effect = ValueError("#99 is not indexed")
            result = read_issue(numbers=[99])

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["status"] == "error"
        assert result[0]["number"] == 99
        assert "is not indexed" in result[0]["error"]

    def test_number_and_numbers_missing_raises(self):
        """Raises ValueError when neither number nor numbers is specified."""
        with patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            with pytest.raises(ValueError, match="specify number or numbers"):
                read_issue()

    def test_single_number_backward_compatible(self):
        """Single call without numbers returns dict as before (backward compat)."""
        rows = [
            _row(comment_id=0, author="human-user", is_bot=False),
        ]
        mock_conn = self._mock_conn_with_rows(rows)

        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            result = read_issue(42)

        assert isinstance(result, dict)
        assert result["number"] == 42
        assert "status" not in result
        assert len(result["items"]) == 1

    def test_number_and_numbers_mutually_exclusive(self):
        """Raises ValueError when both number and numbers are specified."""
        with patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            with pytest.raises(ValueError, match="cannot be specified together"):
                read_issue(number=42, numbers=[42, 43])

    def test_batch_too_many_numbers(self):
        """Raises ValueError for 51+ items."""
        with patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            with pytest.raises(ValueError, match="supports up to 50 items"):
                read_issue(numbers=list(range(51)))

    def _mock_conn_with_rows(self, rows):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn.cursor.return_value.__enter__.return_value = cursor
        conn.__enter__.return_value = conn
        return conn
