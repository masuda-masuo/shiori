"""Tests for shiori_grep (issue #146)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shiori.mcp_server import grep_search


class TestGrepSearch:
    """shiori_grep tool (ripgrep-based clone search)."""

    def _run_grep(
        self,
        rg_stdout: str = "",
        rg_returncode: int = 0,
        repo_isdir: bool = True,
        repo_count: int = 1,
        **kwargs,
    ):
        repos = ["o/r"] * repo_count
        with (
            patch("shiori.mcp_server._resolve_repos", return_value=repos),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=repo_isdir),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = rg_stdout
            mock_result.returncode = rg_returncode
            mock_result.stderr = ""
            mock_run.return_value = mock_result
            return grep_search(pattern="test", **kwargs)

    def test_basic_match(self):
        """Basic pattern match returns structured results."""
        rg_out = "src/file.py:42:def test_function():\n"
        result = self._run_grep(rg_stdout=rg_out)

        assert result["pattern"] == "test"
        assert result["total_matches"] == 1
        assert len(result["matches"]) == 1
        assert result["matches"][0]["repo"] == "o/r"
        assert result["matches"][0]["line"] == 42
        assert result["matches"][0]["text"] == "def test_function():"

    def test_multiple_matches(self):
        """Multiple matching lines are returned."""
        rg_out = (
            "src/a.py:10:import os\n"
            "src/a.py:20:import sys\n"
            "src/b.py:5:import json\n"
        )
        result = self._run_grep(rg_stdout=rg_out)

        assert result["total_matches"] == 3
        assert len(result["matches"]) == 3
        for m in result["matches"]:
            assert m["repo"] == "o/r"

    def test_no_match(self):
        """rg exit code 1 means no match."""
        result = self._run_grep(rg_returncode=1)

        assert result["total_matches"] == 0
        assert result["matches"] == []

    def test_empty_stdout(self):
        """Empty stdout returns empty matches."""
        result = self._run_grep(rg_stdout="")

        assert result["total_matches"] == 0
        assert result["matches"] == []

    def test_separator_lines_skipped(self):
        """-- separator lines are not counted as matches."""
        rg_out = "src/a.py:1:first\n--\nsrc/b.py:1:second\n"
        result = self._run_grep(rg_stdout=rg_out)

        assert result["total_matches"] == 2
        assert len(result["matches"]) == 2

    def test_truncation(self):
        """Results exceeding max_results are truncated."""
        lines = "\n".join(f"f.py:{i}:line {i}" for i in range(1, 51))
        result = self._run_grep(rg_stdout=lines, max_results=10)

        assert result["truncated"] is True
        assert result["total_matches"] == 50
        assert len(result["matches"]) == 10

    def test_no_truncation(self):
        """Results within max_results are not truncated."""
        lines = "\n".join(f"f.py:{i}:line {i}" for i in range(1, 6))
        result = self._run_grep(rg_stdout=lines, max_results=10)

        assert result["truncated"] is False
        assert result["total_matches"] == 5
        assert len(result["matches"]) == 5

    def test_fixed_string_mode(self):
        """regex=False passes --fixed-strings to rg."""
        with (
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.returncode = 1
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            grep_search(pattern="foo.bar", regex=False)

            cmd = mock_run.call_args[0][0]
            assert "--fixed-strings" in cmd

    def test_path_param(self):
        """path parameter limits search scope."""
        with (
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.returncode = 1
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            grep_search(pattern="test", path="src/utils.py")

            cmd = mock_run.call_args[0][0]
            assert "src/utils.py" in cmd[-1]

    def test_path_escape_raises(self):
        """Path traversal outside the repo raises ValueError."""
        def _mock_realpath(p: str) -> str:
            if "/../" in p:
                return "/etc/passwd"
            return p

        with (
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=_mock_realpath,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.returncode = 1
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            with pytest.raises(ValueError, match="inside the repository"):
                grep_search(pattern="test", path="../etc")

    def test_clone_missing_is_skipped(self):
        """Missing clone is reported in skipped_repos instead of raising."""
        with (
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=False),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            result = grep_search(pattern="test")

            assert result["skipped_repos"] == ["o/r"]
            assert result["total_matches"] == 0
            assert result["matches"] == []

    def test_default_regex_is_true(self):
        """Default regex=True does NOT pass --fixed-strings to rg."""
        with (
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.returncode = 1
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            grep_search(pattern="foo.bar")

            cmd = mock_run.call_args[0][0]
            assert "--fixed-strings" not in cmd

    def test_explicit_regex_true(self):
        """Explicit regex=True does NOT pass --fixed-strings to rg."""
        with (
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.returncode = 1
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            grep_search(pattern="foo.bar", regex=True)

            cmd = mock_run.call_args[0][0]
            assert "--fixed-strings" not in cmd

    def test_regex_parse_error_hint(self):
        """rg exit code 2 includes self-healing hint in error."""
        with (
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.returncode = 2
            mock_result.stderr = "regex parse error: unmatched ("
            mock_run.return_value = mock_result

            with pytest.raises(RuntimeError) as excinfo:
                grep_search(pattern="foo(")

            assert "regex parse error" in str(excinfo.value)
            assert "regex=False" in str(excinfo.value)

    def test_e_flag_for_pattern(self):
        """Pattern is passed via -e to avoid flag interpretation."""
        with (
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.returncode = 1
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            grep_search(pattern="--debug")

            cmd = mock_run.call_args[0][0]
            assert "-e" in cmd
            assert cmd[cmd.index("-e") + 1] == "--debug"

    def test_path_strip_absolute_prefix(self):
        """Path in match entries is stripped to repo-relative."""
        with (
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = "/data/repos/o__r/src/file.py:42:content\n"
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = grep_search(pattern="test")

            assert result["matches"][0]["path"] == "src/file.py"
            assert result["matches"][0]["repo"] == "o/r"

    def test_path_kept_when_within_base(self):
        """Path inside base without prefix is kept as-is."""
        with (
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = "/data/repos/o__r/src/other.py:1:line\n"
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = grep_search(pattern="test")

            assert result["matches"][0]["path"] == "src/other.py"

    def test_ignore_case_default(self):
        """Default ignore_case=True passes -i to rg."""
        with (
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.returncode = 1
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            grep_search(pattern="Error")

            cmd = mock_run.call_args[0][0]
            assert "-i" in cmd

    def test_skipped_repos_in_response(self):
        """skipped_repos lists repos without a clone."""
        with (
            patch("shiori.mcp_server._ensure_phase1"),
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r1", "o/r2"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", side_effect=[False, True]),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = "src/file.py:1:match\n"
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = grep_search(pattern="test")

            assert result["skipped_repos"] == ["o/r1"]
            assert result["total_matches"] == 1
            assert result["matches"][0]["repo"] == "o/r2"

    def test_all_repos_wildcard(self):
        """repo='*' searches all configured repos."""
        with (
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r1", "o/r2", "o/r3"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = "file.py:1:match\n"
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = grep_search(pattern="test")

            # 3 repos searched, 3 matches expected
            assert result["total_matches"] == 3
            repos_found = {m["repo"] for m in result["matches"]}
            assert repos_found == {"o/r1", "o/r2", "o/r3"}
            assert result["skipped_repos"] == []

    def test_file_path_parses_rg_line_only_format(self):
        """File path with rg FILE:LINE:CONTENT output parses correctly (regression #210).

        When -H/--with-filename is passed, rg always emits FILE:LINE:CONTENT
        even for a single file argument.  The parser's single branch must strip
        the absolute prefix and return a repo-relative path, consistent with
        directory-mode results.
        """
        base = "/data/repos/o__r"
        rg_out = f"{base}/src/file.py:42:def foo():\n"
        result = self._run_grep(rg_stdout=rg_out, path="src/file.py")

        assert result["total_matches"] == 1
        assert len(result["matches"]) == 1
        assert result["matches"][0]["path"] == "src/file.py"
        assert result["matches"][0]["line"] == 42
        assert result["matches"][0]["text"] == "def foo():"

    def test_with_filename_in_rg_command(self):
        """--with-filename is in the rg command so output is always FILE:LINE:CONTENT."""
        with (
            patch("shiori.mcp_server._resolve_repos", return_value=["o/r"]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch(
                "shiori.mcp_server.os.path.realpath",
                side_effect=lambda p: p,
            ),
            patch("shiori.mcp_server.subprocess.run") as mock_run,
        ):
            mock_settings.repo_dir.side_effect = lambda r: f"/data/repos/{r.replace('/', '__')}"
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.returncode = 1
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            grep_search(pattern="test", path="src/file.py")

            cmd = mock_run.call_args[0][0]
            assert "--with-filename" in cmd
