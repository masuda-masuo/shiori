"""shiori_read_file / shiori_read_pr_file / shiori_status の拡張部分のテスト（issue #101）。"""

from __future__ import annotations

from unittest.mock import MagicMock, mock_open, patch

from shiori.mcp_server import (
    _LARGE_FILE_THRESHOLD,
    read_file,
    read_pr_file,
    status,
)


class TestReadFileLargeFileHint:
    """read_file の large-file hint（issue #101）。"""

    def _run_read_file(self, content: str, **kwargs):
        """read_file をモック環境で実行するヘルパー。"""
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
        """end_line 未指定かつファイルが閾値を超える場合に hints を含む。"""
        large_content = "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD + 10))
        result = self._run_read_file(large_content)

        assert "hints" in result
        assert len(result["hints"]) == 1
        assert "File is large" in result["hints"][0]

    def test_no_hint_when_file_small(self):
        """end_line 未指定でもファイルが閾値以下の場合は hints を含まない。"""
        small_content = "\n".join(f"line {i}" for i in range(10))
        result = self._run_read_file(small_content)

        assert "hints" not in result

    def test_no_hint_when_end_line_specified(self):
        """end_line 指定時はファイルが大きくても hints を含まない。"""
        large_content = "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD + 10))
        result = self._run_read_file(large_content, end_line=50)

        assert "hints" not in result

    def test_hint_at_threshold_boundary(self):
        """閾値ちょうどのファイルは hints なし、閾値+1 で hints あり。"""
        result = self._run_read_file(
            "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD))
        )
        assert "hints" not in result

        result = self._run_read_file(
            "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD + 1))
        )
        assert "hints" in result


class TestReadPrFileLargeFileHint:
    """read_pr_file の large-file hint（issue #101）。"""

    def _run_read_pr_file(self, content: str, **kwargs):
        """read_pr_file をモック環境で実行するヘルパー。"""
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
        """end_line 未指定かつファイルが閾値を超える場合に hints を含む。"""
        large_content = "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD + 10))
        result = self._run_read_pr_file(large_content)

        assert "hints" in result
        assert len(result["hints"]) == 1
        assert "File is large" in result["hints"][0]

    def test_no_hint_when_file_small(self):
        """end_line 未指定でもファイルが閾値以下の場合は hints を含まない。"""
        small_content = "\n".join(f"line {i}" for i in range(10))
        result = self._run_read_pr_file(small_content)

        assert "hints" not in result

    def test_no_hint_when_end_line_specified(self):
        """end_line 指定時はファイルが大きくても hints を含まない。"""
        large_content = "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD + 10))
        result = self._run_read_pr_file(large_content, end_line=50)

        assert "hints" not in result

    def test_hint_at_threshold_boundary(self):
        """閾値ちょうどのファイルは hints なし、閾値+1 で hints あり。"""
        result = self._run_read_pr_file(
            "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD))
        )
        assert "hints" not in result

        result = self._run_read_pr_file(
            "\n".join(f"line {i}" for i in range(_LARGE_FILE_THRESHOLD + 1))
        )
        assert "hints" in result


class TestStatusCodeChunks:
    """status() の code_chunks フィールド（issue #101）。"""

    def test_code_chunks_present(self):
        """status() のレスポンスに code_chunks フィールドが含まれる。"""
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_sync_runs", return_value={}),
            patch("shiori.mcp_server.db.get_all_repo_index_state", return_value={}),
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
        """コードチャンクが存在しない場合、code_chunks は 0。"""
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_sync_runs", return_value={}),
            patch("shiori.mcp_server.db.get_all_repo_index_state", return_value={}),
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
    """status() の code_added キー名（issue #101）。"""

    def test_code_added_key_present_in_status(self):
        """status() のレスポンスに code_added（code_indexed ではない）が含まれる。"""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_all_repo_index_state", return_value={}),
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
        """同期記録がない場合のデフォルトに code_added が含まれる。"""
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_sync_runs", return_value={}),
            patch("shiori.mcp_server.db.get_all_repo_index_state", return_value={}),
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


class TestStatusTokenProvider:
    """status() の token_provider フィールド(issue #188)。"""

    def _run_status(self, provider_name):
        mock_provider = MagicMock()
        mock_provider.name = provider_name
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_sync_runs", return_value={}),
            patch("shiori.mcp_server.db.get_chunk_counts", return_value={}),
            patch("shiori.mcp_server.db.get_issue_item_count", return_value=0),
            patch("shiori.mcp_server.db.get_cursors", return_value={}),
            patch("shiori.mcp_server.build_token_provider", return_value=mock_provider),
            patch("shiori.mcp_server.db.get_all_repo_index_state", return_value={}),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 0
            return status()

    def test_reports_selected_provider_name(self):
        result = self._run_status("token_socket")
        assert result["token_provider"] == "token_socket"

    def test_reports_token_command(self):
        result = self._run_status("token_command")
        assert result["token_provider"] == "token_command"

    def test_reports_anonymous_only_when_nothing_configured(self):
        result = self._run_status("anonymous")
        assert result["token_provider"] == "anonymous"

    def test_static_provider_no_warning(self):
        result = self._run_status("static")
        assert result["token_provider"] == "static"


class TestStatusTokenProviderError:
    """status() must never raise even when build_token_provider() itself raises (issue #193)."""

    def _run_status(self, build_token_provider_side_effect):
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_sync_runs", return_value={}),
            patch("shiori.mcp_server.db.get_chunk_counts", return_value={}),
            patch("shiori.mcp_server.db.get_issue_item_count", return_value=0),
            patch("shiori.mcp_server.db.get_cursors", return_value={}),
            patch("shiori.mcp_server.db.get_all_repo_index_state", return_value={}),
            patch(
                "shiori.mcp_server.build_token_provider",
                side_effect=build_token_provider_side_effect,
            ),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 0
            return status()

    def test_does_not_raise_on_build_provider_valueerror(self):
        result = self._run_status(ValueError("token provider config invalid"))
        assert result["token_provider"] == "error"

    def test_warning_includes_exception_message(self):
        result = self._run_status(ValueError("token provider config invalid"))
        warnings = result["repos"]["o/r"]["warnings"]
        assert any("token provider config invalid" in w for w in warnings)

    def test_does_not_raise_on_arbitrary_exception(self):
        result = self._run_status(RuntimeError("boom"))
        assert result["token_provider"] == "error"
        assert any("boom" in w for w in result["repos"]["o/r"]["warnings"])

    def test_normal_path_unaffected_when_no_error(self):
        mock_provider = MagicMock()
        mock_provider.name = "token_socket"
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_sync_runs", return_value={}),
            patch("shiori.mcp_server.db.get_chunk_counts", return_value={}),
            patch("shiori.mcp_server.db.get_issue_item_count", return_value=0),
            patch("shiori.mcp_server.db.get_cursors", return_value={}),
            patch("shiori.mcp_server.db.get_all_repo_index_state", return_value={}),
            patch("shiori.mcp_server.build_token_provider", return_value=mock_provider),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 0
            result = status()
        assert result["token_provider"] == "token_socket"
        assert not any(
            "token_provider could not be determined" in w
            for w in result["repos"]["o/r"]["warnings"]
        )
