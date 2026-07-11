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


class TestStatusAutoSyncRunning:
    """status() の auto_sync_running フィールド（issue #187: 設定値でなく実際のスレッド生存）。"""

    def _run_status(self, mock_settings):
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings", mock_settings),
            patch("shiori.mcp_server.db.get_sync_runs", return_value={}),
            patch("shiori.mcp_server.db.get_chunk_counts", return_value={}),
            patch("shiori.mcp_server.db.get_issue_item_count", return_value=0),
            patch("shiori.mcp_server.db.get_cursors", return_value={}),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 10
            return status()

    def test_false_when_thread_never_started(self):
        """スレッドが一度も起動していない場合は False。"""
        with patch("shiori.mcp_server._auto_sync_thread", None):
            result = self._run_status(MagicMock())
        assert result["auto_sync_running"] is False

    def test_true_when_thread_alive(self):
        """生存しているスレッドがあれば True。"""
        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = True
        with patch("shiori.mcp_server._auto_sync_thread", fake_thread):
            result = self._run_status(MagicMock())
        assert result["auto_sync_running"] is True

    def test_false_when_thread_died(self):
        """スレッドオブジェクトは存在するが死んでいる場合は False。

        設定値（sync_interval_seconds > 0）だけを見て「動いている」と偽らないことを確認する。
        """
        dead_thread = MagicMock()
        dead_thread.is_alive.return_value = False
        with patch("shiori.mcp_server._auto_sync_thread", dead_thread):
            result = self._run_status(MagicMock())
        assert result["auto_sync_running"] is False
        assert result["sync_interval_seconds"] == 10  # config still reports enabled

    def test_last_attempt_and_error_fields_present_with_no_sync_run(self):
        """同期記録が無い(初回起動)リポジトリでも last_attempt_at/last_error/consecutive_failures を返す。"""
        with patch("shiori.mcp_server._auto_sync_thread", None):
            result = self._run_status(MagicMock())
        repo_info = result["repos"]["o/r"]
        assert repo_info["last_attempt_at"] is None
        assert repo_info["last_error"] is None
        assert repo_info["consecutive_failures"] == 0

    def test_attempt_fields_pass_through_from_sync_runs(self):
        """db.get_sync_runs が返す attempt 情報がそのまま status() の出力に反映される。"""
        mock_settings = MagicMock()
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings", mock_settings),
            patch("shiori.mcp_server._auto_sync_thread", None),
            patch(
                "shiori.mcp_server.db.get_sync_runs",
                return_value={
                    "o/r": {
                        "last_synced_at": None,
                        "age_seconds": None,
                        "route": None,
                        "docs_updated": None,
                        "issues_indexed": None,
                        "code_added": None,
                        "last_attempt_at": "2026-07-10T00:14:15+00:00",
                        "last_error": "git fetch failed (exit 128): Invalid username or token",
                        "consecutive_failures": 42,
                    }
                },
            ),
            patch("shiori.mcp_server.db.get_chunk_counts", return_value={}),
            patch("shiori.mcp_server.db.get_issue_item_count", return_value=0),
            patch("shiori.mcp_server.db.get_cursors", return_value={}),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 10
            result = status()

        repo_info = result["repos"]["o/r"]
        assert repo_info["last_attempt_at"] == "2026-07-10T00:14:15+00:00"
        assert repo_info["consecutive_failures"] == 42
        assert any("42 consecutive sync failures" in w for w in repo_info["warnings"])


class TestStatusTokenProvider:
    """status() の token_provider フィールド(issue #188)。

    build_token_provider() が実際に選んだ provider の .name を返すこと、
    mcp_token が anonymous へフォールバック中はそれを反映して "anonymous" を
    返し、対応する警告も出ることを確認する。
    """

    def _run_status(self, provider_name, fallback_reason):
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
            patch(
                "shiori.mcp_server.get_mcp_token_fallback_reason",
                return_value=fallback_reason,
            ),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 0
            return status()

    def test_reports_selected_provider_name(self):
        """フォールバックが無ければ選ばれた provider 名をそのまま返す。"""
        result = self._run_status("app", None)
        assert result["token_provider"] == "app"
        assert not any("falling back" in w for w in result["repos"]["o/r"]["warnings"])

    def test_reports_anonymous_when_mcp_token_falls_back(self):
        """mcp_token が観測済みフォールバック理由を持つとき、effective は anonymous になる。"""
        result = self._run_status(
            "mcp_token", "mcp-token binary unresolved or mint failed; falling back to anonymous"
        )
        assert result["token_provider"] == "anonymous"
        assert any(
            "falling back to anonymous" in w for w in result["repos"]["o/r"]["warnings"]
        )

    def test_no_downgrade_for_non_mcp_token_providers(self):
        """mcp_token 以外は get_mcp_token_fallback_reason の値を無視する(そもそも対象外)。"""
        result = self._run_status("static", None)
        assert result["token_provider"] == "static"

    def test_static_provider_no_warning(self):
        """フォールバック理由が無いときは警告が付かない。"""
        result = self._run_status("mcp_token", None)
        assert result["token_provider"] == "mcp_token"
        assert not any("falling back" in w for w in result["repos"]["o/r"]["warnings"])
