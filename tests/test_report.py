"""Tests for shiori_report (issue #153)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shiori.mcp_server import report


class TestReport:
    """shiori_report tool."""

    def test_stats_basic(self):
        """stats template returns markdown table."""
        tokei_out = '{"Python": {"lines": [{"name": "f.py", "code": 10, "comments": 2, "blanks": 3}]}, "Total": {"lines": [{"code": 10, "comments": 2, "blanks": 3, "name": "total"}]}}'
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = tokei_out
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="stats")

        assert result["repo"] == "o/r"
        assert result["template"] == "stats"
        assert "markdown" in result
        assert "| Python | 1 | 10 | 2 | 3 |" in result["markdown"]
        assert "| **Total** | **1** | **10** | **2** | **3** |" in result["markdown"]

    def test_stats_multiple_languages(self):
        """Multiple languages render as separate rows."""
        tokei_out = '''{
  "Python": { "lines": [{"name": "a.py", "code": 10, "comments": 2, "blanks": 3}] },
  "Rust": { "lines": [{"name": "b.rs", "code": 20, "comments": 4, "blanks": 5}] },
  "Total": { "lines": [{"code": 30, "comments": 6, "blanks": 8, "name": "total"}] }
}'''
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = tokei_out
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="stats")

        markdown = result["markdown"]
        assert "| Python | 1 | 10 | 2 | 3 |" in markdown
        assert "| Rust | 1 | 20 | 4 | 5 |" in markdown
        assert "| **Total** | **2** | **30** | **6** | **8** |" in markdown

    def test_unknown_template(self):
        """Unknown template name raises ValueError with valid list."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
        ):
            with pytest.raises(ValueError, match="Unknown template"):
                report(template="nonexistent")

    def test_tokei_not_installed(self):
        """Missing tokei raises RuntimeError."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.mcp_server.subprocess.run", side_effect=FileNotFoundError),
        ):
            with pytest.raises(RuntimeError, match="tokei is not installed"):
                report(template="stats")

    def test_stats_sorted_alphabetically(self):
        """Languages are sorted alphabetically (case-insensitive)."""
        tokei_out = '''{
  "Rust": { "lines": [{"name": "a.rs", "code": 1, "comments": 0, "blanks": 0}] },
  "c": { "lines": [{"name": "a.c", "code": 2, "comments": 0, "blanks": 0}] },
  "Python": { "lines": [{"name": "a.py", "code": 3, "comments": 0, "blanks": 0}] },
  "Total": { "lines": [{"code": 6, "comments": 0, "blanks": 0, "name": "total"}] }
}'''
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = tokei_out
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="stats")

        lines = [l.strip() for l in result["markdown"].split("\n")]
        data_rows = [l for l in lines if l.startswith("|") and "Language" not in l and "---" not in l and "Total" not in l]
        langs = [l.split("|")[1].strip() for l in data_rows]
        assert langs == ["c", "Python", "Rust"]

    def test_repo_path_param(self):
        """path parameter scopes tokei to subdirectory."""
        tokei_out = '{"Python": { "lines": [{"name": "a.py", "code": 1, "comments": 0, "blanks": 0}] }, "Total": { "lines": [{"code": 1, "comments": 0, "blanks": 0, "name": "total"}] }}'
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = tokei_out
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="stats", path="src/")

        cmd = mock_run.call_args[0][0]
        assert "src/" in cmd[-1]
        assert result["template"] == "stats"
