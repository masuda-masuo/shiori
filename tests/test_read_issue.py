"""Unit tests for shiori_read_issue (issue #44, #86, #189, #260).

Test targets:
- read_issue: exclude_noise_bots=False (default) returns all items
- read_issue: exclude_noise_bots=True filters out bots outside allowlist
- read_issue: ValueError when all items filtered
- read_issue: ValueError when API returns 404
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from shiori.mcp_server import read_issue, settings


def _mock_api_response(data, status_code=200):
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    resp.links = {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp,
        )
    return resp


def _mock_issue_response(
    number=42,
    title="Test Issue",
    state="open",
    kind="issue",
    body="test body",
    user=None,
    created_at="2026-06-14T00:00:00Z",
):
    """Build a mock GitHub issue API response."""
    if user is None:
        user = {"login": "human-user", "type": "User"}
    data = {
        "number": number,
        "title": title,
        "state": state,
        "body": body,
        "user": user,
        "html_url": f"https://github.com/o/r/issues/{number}",
        "created_at": created_at,
    }
    if kind == "pr":
        data["pull_request"] = {"url": "https://api.github.com/repos/o/r/pulls/1"}
    return data


class TestReadIssueExcludeNoiseBots:
    """Behavior of read_issue exclude_noise_bots parameter."""

    def _mock_github_client(self, issue_data, comments_data=None):
        """Build a mock _github_client context manager returning mocked client."""
        client = MagicMock()
        # Issue response
        client.get.return_value = _mock_api_response(issue_data)
        if comments_data is not None:
            client.get.side_effect = [
                _mock_api_response(issue_data),
                _mock_api_response(comments_data),
            ]
        ctx = MagicMock()
        ctx.__enter__.return_value = client
        return ctx

    def test_default_returns_all_items(self):
        """exclude_noise_bots=False (default) returns all items including bots."""
        issue_data = _mock_issue_response(
            user={"login": "human-user", "type": "User"},
        )
        comments_data = [
            {"user": {"login": "allowlisted-bot[bot]", "type": "Bot"}, "body": "comment 1",
             "created_at": "2026-06-14T00:01:00Z", "html_url": "https://github.com/o/r/issues/42#issuecomment-1"},
            {"user": {"login": "dependabot[bot]", "type": "Bot"}, "body": "comment 2",
             "created_at": "2026-06-14T00:02:00Z", "html_url": "https://github.com/o/r/issues/42#issuecomment-2"},
        ]

        with patch("shiori.tools.read._github_client") as mock_gh:
            client = MagicMock()
            mock_gh.return_value.__enter__.return_value = client
            client.get.side_effect = [
                _mock_api_response(issue_data),
                _mock_api_response(comments_data),
            ]

            with patch("shiori.tools.read._resolve_repo", return_value="o/r"):
                result = read_issue(42, exclude_noise_bots=False)

        assert len(result["items"]) == 3
        assert result["items"][0]["author"] == "human-user"
        assert result["items"][1]["author"] == "allowlisted-bot[bot]"
        assert result["items"][2]["author"] == "dependabot[bot]"

    def test_exclude_noise_bots_filters_non_allowlisted_bots(self, monkeypatch):
        """exclude_noise_bots=True filters only bots outside the allowlist."""
        monkeypatch.setattr(settings, "index_bot_logins",
                            {"allowlisted-bot[bot]", "another-bot[bot]"})
        issue_data = _mock_issue_response(
            user={"login": "human-user", "type": "User"},
        )
        comments_data = [
            {"user": {"login": "allowlisted-bot[bot]", "type": "Bot"}, "body": "comment 1",
             "created_at": "2026-06-14T00:01:00Z", "html_url": "https://github.com/o/r/issues/42#issuecomment-1"},
            {"user": {"login": "dependabot[bot]", "type": "Bot"}, "body": "comment 2",
             "created_at": "2026-06-14T00:02:00Z", "html_url": "https://github.com/o/r/issues/42#issuecomment-2"},
        ]

        with patch("shiori.tools.read._github_client") as mock_gh:
            client = MagicMock()
            mock_gh.return_value.__enter__.return_value = client
            client.get.side_effect = [
                _mock_api_response(issue_data),
                _mock_api_response(comments_data),
            ]

            with patch("shiori.tools.read._resolve_repo", return_value="o/r"):
                result = read_issue(42, exclude_noise_bots=True)

        assert len(result["items"]) == 2
        assert result["items"][0]["author"] == "human-user"
        assert result["items"][1]["author"] == "allowlisted-bot[bot]"

    def test_exclude_noise_bots_keeps_allowlisted_bot_with_different_case(self, monkeypatch):
        """allowlist match is case-insensitive."""
        monkeypatch.setattr(settings, "index_bot_logins",
                            {"allowlisted-bot[bot]"})
        issue_data = _mock_issue_response(
            user={"login": "Allowlisted-Bot[bot]", "type": "Bot"},
        )

        with patch("shiori.tools.read._github_client") as mock_gh:
            client = MagicMock()
            mock_gh.return_value.__enter__.return_value = client
            client.get.side_effect = [
                _mock_api_response(issue_data),
                _mock_api_response([]),  # no comments
            ]

            with patch("shiori.tools.read._resolve_repo", return_value="o/r"):
                result = read_issue(42, exclude_noise_bots=True)

        assert len(result["items"]) == 1
        assert result["items"][0]["author"] == "Allowlisted-Bot[bot]"

    def test_exclude_noise_bots_raises_when_all_filtered(self, monkeypatch):
        """Raises ValueError when all items are filtered."""
        monkeypatch.setattr(settings, "index_bot_logins",
                            {"allowlisted-bot[bot]"})
        issue_data = _mock_issue_response(
            user={"login": "dependabot[bot]", "type": "Bot"},
        )

        with patch("shiori.tools.read._github_client") as mock_gh:
            client = MagicMock()
            mock_gh.return_value.__enter__.return_value = client
            client.get.side_effect = [
                _mock_api_response(issue_data),
                _mock_api_response([]),
            ]

            with patch("shiori.tools.read._resolve_repo", return_value="o/r"):
                with pytest.raises(ValueError, match="all items are bots outside the allowlist"):
                    read_issue(42, exclude_noise_bots=True)

    def test_exclude_noise_bots_excludes_bot_outside_allowlist(self, monkeypatch):
        """Bot with login not in allowlist is excluded."""
        monkeypatch.setattr(settings, "index_bot_logins",
                            {"allowlisted-bot[bot]"})
        issue_data = _mock_issue_response(
            user={"login": "human-user", "type": "User"},
        )
        comments_data = [
            {"user": {"login": "renovate[bot]", "type": "Bot"}, "body": "bot comment",
             "created_at": "2026-06-14T00:01:00Z", "html_url": "https://github.com/o/r/issues/42#issuecomment-1"},
        ]

        with patch("shiori.tools.read._github_client") as mock_gh:
            client = MagicMock()
            mock_gh.return_value.__enter__.return_value = client
            client.get.side_effect = [
                _mock_api_response(issue_data),
                _mock_api_response(comments_data),
            ]

            with patch("shiori.tools.read._resolve_repo", return_value="o/r"):
                result = read_issue(42, exclude_noise_bots=True)

        assert len(result["items"]) == 1  # human body remains, bot comment excluded

    def test_not_found_raises_value_error(self):
        """Raises ValueError when API returns 404."""
        with patch("shiori.tools.read._github_client") as mock_gh:
            client = MagicMock()
            mock_gh.return_value.__enter__.return_value = client
            client.get.side_effect = httpx.HTTPStatusError(
                "404 Not Found", request=MagicMock(),
                response=_mock_api_response({}, status_code=404),
            )

            with patch("shiori.tools.read._resolve_repo", return_value="o/r"):
                with pytest.raises(ValueError, match="not found on GitHub"):
                    read_issue(42)

    def test_network_error_raises(self):
        """Raises httpx.HTTPError when API is unreachable."""
        with patch("shiori.tools.read._github_client") as mock_gh:
            client = MagicMock()
            mock_gh.return_value.__enter__.return_value = client
            client.get.side_effect = httpx.ConnectError("connection refused")

            with patch("shiori.tools.read._resolve_repo", return_value="o/r"):
                with pytest.raises(httpx.ConnectError):
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
        with patch("shiori.tools.read._read_issue_single") as mock, \
             patch("shiori.tools.read._resolve_repo", return_value="o/r"):
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

    def test_batch_partial_not_found(self):
        with patch("shiori.tools.read._read_issue_single") as mock, \
             patch("shiori.tools.read._resolve_repo", return_value="o/r"):
            mock.side_effect = [
                self._make_result(42, "issue 42"),
                ValueError("#43 not found on GitHub"),
                self._make_result(44, "issue 44"),
            ]
            result = read_issue(numbers=[42, 43, 44])

        assert isinstance(result, list)
        assert len(result) == 3
        assert result[0]["status"] == "ok"
        assert result[0]["number"] == 42
        assert result[1]["status"] == "error"
        assert result[1]["number"] == 43
        assert "not found" in result[1]["error"]
        assert result[2]["status"] == "ok"
        assert result[2]["number"] == 44

    def test_batch_empty_array(self):
        with patch("shiori.tools.read._resolve_repo", return_value="o/r"):
            result = read_issue(numbers=[])
        assert isinstance(result, list)
        assert result == []

    def test_batch_duplicate_numbers(self):
        with patch("shiori.tools.read._read_issue_single") as mock, \
             patch("shiori.tools.read._resolve_repo", return_value="o/r"):
            mock.side_effect = [
                self._make_result(42, "issue 42"),
                self._make_result(42, "issue 42"),
            ]
            result = read_issue(numbers=[42, 42])
        assert isinstance(result, list)
        assert len(result) == 2

    def test_batch_applies_exclude_noise_bots(self):
        with patch("shiori.tools.read._read_issue_single") as mock, \
             patch("shiori.tools.read._resolve_repo", return_value="o/r"):
            mock.return_value = self._make_result(42, "issue 42")
            read_issue(numbers=[42, 43], exclude_noise_bots=True)
        assert mock.call_count == 2
        for call_args in mock.call_args_list:
            assert call_args[0][2] is True
            assert call_args[0][0] == "o/r"

    def test_batch_passes_repo_to_single(self):
        with patch("shiori.tools.read._read_issue_single") as mock_single, \
             patch("shiori.tools.read._resolve_repo") as mock_resolve:
            mock_resolve.return_value = "my/repo"
            mock_single.return_value = self._make_result(42, "issue 42")
            read_issue(numbers=[42], repo="my/repo")
        mock_resolve.assert_called_once_with("my/repo")
        mock_single.assert_called_once_with("my/repo", 42, False)

    def test_batch_all_errors(self):
        with patch("shiori.tools.read._read_issue_single") as mock, \
             patch("shiori.tools.read._resolve_repo", return_value="o/r"):
            mock.side_effect = ValueError("#99 not found on GitHub")
            result = read_issue(numbers=[99])
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["status"] == "error"
        assert result[0]["number"] == 99
        assert "not found" in result[0]["error"]

    def test_number_and_numbers_missing_raises(self):
        with patch("shiori.tools.read._resolve_repo", return_value="o/r"):
            with pytest.raises(ValueError, match="specify number or numbers"):
                read_issue()

    def test_single_number_backward_compatible(self):
        """Single call without numbers returns dict as before."""
        with patch("shiori.tools.read._github_client") as mock_gh:
            client = MagicMock()
            mock_gh.return_value.__enter__.return_value = client
            client.get.side_effect = [
                _mock_api_response(_mock_issue_response()),
                _mock_api_response([]),
            ]
            with patch("shiori.tools.read._resolve_repo", return_value="o/r"):
                result = read_issue(42)
        assert isinstance(result, dict)
        assert result["number"] == 42
        assert "status" not in result
        assert len(result["items"]) == 1

    def test_number_and_numbers_mutually_exclusive(self):
        with patch("shiori.tools.read._resolve_repo", return_value="o/r"):
            with pytest.raises(ValueError, match="cannot be specified together"):
                read_issue(number=42, numbers=[42, 43])

    def test_batch_too_many_numbers(self):
        with patch("shiori.tools.read._resolve_repo", return_value="o/r"):
            with pytest.raises(ValueError, match="supports up to 50 items"):
                read_issue(numbers=list(range(51)))


class TestReadIssueRepoResolution:
    """read_issue distinguishes unknown repo vs known-repo-not-found (issue #189)."""

    def test_known_repo_not_found_issue(self, monkeypatch):
        """A configured/known repo with 404 still raises the appropriate error."""
        monkeypatch.setattr(
            settings, "repos", ["masuda-masuo/shiori", "masuda-masuo/code-sandbox-mcp"]
        )
        with patch("shiori.tools.read._github_client") as mock_gh:
            client = MagicMock()
            mock_gh.return_value.__enter__.return_value = client
            client.get.side_effect = httpx.HTTPStatusError(
                "404 Not Found", request=MagicMock(),
                response=_mock_api_response({}, status_code=404),
            )
            with pytest.raises(ValueError) as exc:
                read_issue(531, repo="masuda-masuo/shiori")
        msg = str(exc.value)
        assert "not found on GitHub" in msg
        assert "unknown repo" not in msg

    def test_unknown_repo_raises_before_hitting_api(self, monkeypatch):
        """An unresolvable repo argument fails fast."""
        monkeypatch.setattr(
            settings, "repos", ["masuda-masuo/shiori", "masuda-masuo/code-sandbox-mcp"]
        )
        with patch("shiori.tools.read._read_issue_single") as mock_single:
            with pytest.raises(ValueError) as exc:
                read_issue(531, repo="totally-bogus-repo-xyz")
        msg = str(exc.value)
        assert "unknown repo" in msg
        assert "masuda-masuo/shiori" in msg
        assert "masuda-masuo/code-sandbox-mcp" in msg
        mock_single.assert_not_called()

    def test_short_name_resolves_and_reads(self, monkeypatch):
        """A short name that uniquely matches an indexed repo is accepted."""
        monkeypatch.setattr(
            settings, "repos", ["masuda-masuo/shiori", "masuda-masuo/code-sandbox-mcp"]
        )
        issue_data = _mock_issue_response(
            number=531,
            title="Title",
        )
        with patch("shiori.tools.read._github_client") as mock_gh:
            client = MagicMock()
            mock_gh.return_value.__enter__.return_value = client
            client.get.side_effect = [
                _mock_api_response(issue_data),
                _mock_api_response([]),
            ]
            result = read_issue(531, repo="code-sandbox-mcp")
        assert result["repo"] == "masuda-masuo/code-sandbox-mcp"