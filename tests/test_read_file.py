"""Tests for extended parts of shiori_read_file / shiori_read_pr_file / shiori_status (issue #101)."""

from __future__ import annotations

from unittest.mock import MagicMock, mock_open, patch

from shiori.mcp_server import (
    _LARGE_FILE_THRESHOLD,
    read_file,
    read_pr_file,
    status,
)


class TestReadFileLargeFileHint:
    """read_file large-file hint (issue #101)."""

    def _run_read_file(self, content: str, **kwargs):
        """Helper to run read_file in a mocked environment."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.mcp_server.os.path.isfile", return_value=True),
            patch("builtins.open", mock_open(read_data=content)),
        ):
            mock_settings.repo_dir.return_value = "/data/repos"
            return read_file(path="src/file.py", **kwargs)

    def test_hint_when_end_line_none_and_file_large(self):
        """Includes hints when end_line is unset and file exceeds threshold."""
        large_content = "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD + 10))
        result = self._run_read_file(large_content)

        assert "hints" in result
        assert len(result["hints"]) == 1
        assert "File is large" in result["hints"][0]

    def test_no_hint_when_file_small(self):
        """No hints when file is below threshold even without end_line."""
        small_content = "\n".join(f"line {i}" for i in range(10))
        result = self._run_read_file(small_content)

        assert "hints" not in result

    def test_no_hint_when_end_line_specified(self):
        """No hints when end_line is specified even for large files."""
        large_content = "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD + 10))
        result = self._run_read_file(large_content, end_line=50)

        assert "hints" not in result

    def test_hint_at_threshold_boundary(self):
        """Exactly at threshold: no hints; threshold+1: hints present."""
        result = self._run_read_file(
            "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD))
        )
        assert "hints" not in result

        result = self._run_read_file(
            "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD + 1))
        )
        assert "hints" in result


class TestReadPrFileLargeFileHint:
    """read_pr_file large-file hint (issue #101)."""

    def _run_read_pr_file(self, content: str, **kwargs):
        """Helper to run read_pr_file in a mocked environment."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.mcp_server.build_token_provider") as mock_build,
            patch("shiori.mcp_server._git_fetch_ref", return_value="refs/shiori/tmp-pr"),
            patch("shiori.mcp_server._git_delete_ref"),
            patch("shiori.mcp_server._git", return_value=content),
        ):
            mock_build.return_value = MagicMock()
            mock_settings.repo_dir.return_value = "/data/repos"
            return read_pr_file(number=42, path="src/file.py", **kwargs)

    def test_hint_when_end_line_none_and_file_large(self):
        """Includes hints when end_line is unset and file exceeds threshold."""
        large_content = "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD + 10))
        result = self._run_read_pr_file(large_content)

        assert "hints" in result
        assert len(result["hints"]) == 1
        assert "File is large" in result["hints"][0]

    def test_no_hint_when_file_small(self):
        """No hints when file is below threshold even without end_line."""
        small_content = "\n".join(f"line {i}" for i in range(10))
        result = self._run_read_pr_file(small_content)

        assert "hints" not in result

    def test_no_hint_when_end_line_specified(self):
        """No hints when end_line is specified even for large files."""
        large_content = "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD + 10))
        result = self._run_read_pr_file(large_content, end_line=50)

        assert "hints" not in result

    def test_hint_at_threshold_boundary(self):
        """Exactly at threshold: no hints; threshold+1: hints present."""
        result = self._run_read_pr_file(
            "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD))
        )
        assert "hints" not in result

        result = self._run_read_pr_file(
            "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD + 1))
        )
        assert "hints" in result


class TestStatusCodeChunks:
    """status() code_chunks field (issue #101)."""

    def test_code_chunks_present(self):
        """status() response includes code_chunks field."""
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_sync_runs", return_value={}),
            patch("shiori.mcp_server.db.get_chunk_counts",
                  return_value={"doc": 10, "issue": 20, "code": 1290}),
            patch("shiori.mcp_server.db.get_issue_item_count", return_value=0),
            patch("shiori.mcp_server.db.get_cursors", return_value={}),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 300
            result = status()

        repo_info = result["repos"]["o/r"]
        assert "code_chunks" in repo_info
        assert repo_info["code_chunks"] == 1290

    def test_code_chunks_zero_when_no_code_chunks(self):
        """code_chunks is 0 when no code chunks exist."""
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_sync_runs", return_value={}),
            patch("shiori.mcp_server.db.get_chunk_counts",
                  return_value={"doc": 5}),
            patch("shiori.mcp_server.db.get_issue_item_count", return_value=0),
            patch("shiori.mcp_server.db.get_cursors", return_value={}),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 300
            result = status()

        repo_info = result["repos"]["o/r"]
        assert repo_info["code_chunks"] == 0


class TestStatusCodeAdded:
    """status() code_added key name (issue #101)."""

    def test_code_added_key_present_in_status(self):
        """status() response includes code_added (not code_indexed)."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch(
                "shiori.mcp_server.db.get_sync_runs",
                return_value={
                    "o/r": {
                        "last_synced_at": now.isoformat(),
                        "age_seconds": 100,
                        "route": "mcp",
                        "docs_updated": 5,
                        "issues_indexed": 10,
                        "code_added": 3,
                    }
                },
            ),
            patch("shiori.mcp_server.db.get_chunk_counts",
                  return_value={"doc": 10, "issue": 20, "code": 1290}),
            patch("shiori.mcp_server.db.get_issue_item_count", return_value=0),
            patch("shiori.mcp_server.db.get_cursors", return_value={}),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 300
            result = status()

        repo_info = result["repos"]["o/r"]
        assert "code_added" in repo_info
        assert "code_indexed" not in repo_info
        assert repo_info["code_added"] == 3

    def test_code_added_default_when_no_sync_run(self):
        """code_added default is included when no sync record exists."""
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_sync_runs", return_value={}),
            patch("shiori.mcp_server.db.get_chunk_counts", return_value={}),
            patch("shiori.mcp_server.db.get_issue_item_count", return_value=0),
            patch("shiori.mcp_server.db.get_cursors", return_value={}),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 300
            result = status()

        repo_info = result["repos"]["o/r"]
        assert "code_added" in repo_info
        assert "code_indexed" not in repo_info
        assert repo_info["code_added"] is None
