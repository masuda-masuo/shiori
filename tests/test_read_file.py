"""shiori_read_file / shiori_read_pr_file / shiori_status の拡張部分のテスト（issue #101）。"""

from __future__ import annotations

from unittest.mock import MagicMock, mock_open, patch

import pytest

from shiori import mcp_server
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

    build_token_provider() が実際に選んだ provider の .name をそのまま返す。
    「設定した provider が静かに anonymous へ降格する」経路(旧 McpTokenProvider)は
    撤去済みなので、status が anonymous を返すのは「何も設定していない」ときだけ。
    """

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
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 0
            return status()

    def test_reports_selected_provider_name(self):
        """選ばれた provider 名をそのまま返す。"""
        result = self._run_status("token_socket")
        assert result["token_provider"] == "token_socket"
        assert not any("falling back" in w for w in result["repos"]["o/r"]["warnings"])

    def test_reports_token_command(self):
        """token-file 橋渡し / ネイティブ mint はどちらも token_command として出る。"""
        result = self._run_status("token_command")
        assert result["token_provider"] == "token_command"

    def test_reports_anonymous_only_when_nothing_configured(self):
        """anonymous は「何も設定していない」終端状態としてのみ現れる。降格経路は無い。"""
        result = self._run_status("anonymous")
        assert result["token_provider"] == "anonymous"
        assert not any("falling back" in w for w in result["repos"]["o/r"]["warnings"])

    def test_static_provider_no_warning(self):
        result = self._run_status("static")
        assert result["token_provider"] == "static"
        assert not any("falling back" in w for w in result["repos"]["o/r"]["warnings"])


class TestStatusTokenProviderError:
    """status() must never raise even when build_token_provider() itself
    raises (issue #193: GitHub App config only partially set causes
    build_token_provider() to raise ValueError, which previously propagated
    out of status() unhandled -- a regression from #188/#192).
    """

    def _run_status(self, build_token_provider_side_effect):
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_sync_runs", return_value={}),
            patch("shiori.mcp_server.db.get_chunk_counts", return_value={}),
            patch("shiori.mcp_server.db.get_issue_item_count", return_value=0),
            patch("shiori.mcp_server.db.get_cursors", return_value={}),
            patch(
                "shiori.mcp_server.build_token_provider",
                side_effect=build_token_provider_side_effect,
            ),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 0
            return status()

    def test_does_not_raise_on_build_provider_valueerror(self):
        """build_token_provider() raising ValueError does not propagate out of status()."""
        result = self._run_status(ValueError("token provider config invalid"))
        assert result["token_provider"] == "error"

    def test_warning_includes_exception_message(self):
        """The warning surfaced to the caller includes the original exception message."""
        result = self._run_status(ValueError("token provider config invalid"))
        warnings = result["repos"]["o/r"]["warnings"]
        assert any("token provider config invalid" in w for w in warnings)

    def test_does_not_raise_on_arbitrary_exception(self):
        """Any exception from build_token_provider(), not just ValueError, is caught."""
        result = self._run_status(RuntimeError("boom"))
        assert result["token_provider"] == "error"
        assert any(
            "boom" in w for w in result["repos"]["o/r"]["warnings"]
        )

    def test_normal_path_unaffected_when_no_error(self):
        """When build_token_provider() succeeds normally, no error warning is added."""
        mock_provider = MagicMock()
        mock_provider.name = "token_socket"
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_sync_runs", return_value={}),
            patch("shiori.mcp_server.db.get_chunk_counts", return_value={}),
            patch("shiori.mcp_server.db.get_issue_item_count", return_value=0),
            patch("shiori.mcp_server.db.get_cursors", return_value={}),
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


class TestStatusAutoSyncLastError:
    """status() の auto_sync_last_error フィールド(issue #196)。

    _auto_sync_last_error は _auto_sync_loop が更新するモジュールレベル状態で、
    DB 接続自体が死んでいて record_sync_attempt が書き込めないケースでも
    最後の auto-sync 失敗を可視化する最後の手段。
    """

    def _run_status(self):
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_sync_runs", return_value={}),
            patch("shiori.mcp_server.db.get_chunk_counts", return_value={}),
            patch("shiori.mcp_server.db.get_issue_item_count", return_value=0),
            patch("shiori.mcp_server.db.get_cursors", return_value={}),
            patch("shiori.mcp_server.build_token_provider", return_value=MagicMock()),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 10
            return status()

    def test_reports_error_when_set(self):
        """_auto_sync_last_error がセットされているとき status() に反映される。"""
        with patch("shiori.mcp_server._auto_sync_last_error", "connection refused"):
            result = self._run_status()
        assert result["auto_sync_last_error"] == "connection refused"

    def test_reports_none_when_not_set(self):
        """_auto_sync_last_error が None のときは None を返す。"""
        with patch("shiori.mcp_server._auto_sync_last_error", None):
            result = self._run_status()
        assert result["auto_sync_last_error"] is None


class TestAutoSyncLoopLastError:
    """_auto_sync_loop() が _auto_sync_last_error を更新すること(issue #196)。

    _auto_sync_loop は無限ループなので、time.sleep をモックして2周だけ回した後に
    脱出させる(StopIteration は try/except の外側の time.sleep から送出されるため
    auto-sync の失敗として握りつぶされない)。
    """

    def test_sets_error_on_failure_then_clears_on_next_success(self):
        """1周目の失敗でエラーがセットされ、2周目の成功でクリアされる。"""
        outcomes = [RuntimeError("db unreachable"), {"status": "ok", "repos": {}}]

        def fake_do_sync(route):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        sleep_calls = {"n": 0}

        def fake_sleep(interval):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 2:
                raise StopIteration("stop the loop")

        with (
            patch("shiori.mcp_server._do_sync", side_effect=fake_do_sync),
            patch("shiori.mcp_server.time.sleep", side_effect=fake_sleep),
            patch("shiori.mcp_server._auto_sync_last_error", None),
        ):
            with pytest.raises(StopIteration):
                mcp_server._auto_sync_loop(10)

            # Two _do_sync calls happened (fail, then succeed) before the
            # loop was interrupted by the 3rd sleep() call.
            assert outcomes == []
            assert mcp_server._auto_sync_last_error is None

    def test_error_message_captured_on_failure(self):
        """失敗直後(次の成功前)は例外メッセージが _auto_sync_last_error に入る。"""
        def fake_do_sync(route):
            raise RuntimeError("db unreachable")

        sleep_calls = {"n": 0}

        def fake_sleep(interval):
            sleep_calls["n"] += 1
            if sleep_calls["n"] > 1:
                raise StopIteration("stop the loop")

        with (
            patch("shiori.mcp_server._do_sync", side_effect=fake_do_sync),
            patch("shiori.mcp_server.time.sleep", side_effect=fake_sleep),
            patch("shiori.mcp_server._auto_sync_last_error", None),
        ):
            with pytest.raises(StopIteration):
                mcp_server._auto_sync_loop(10)

            assert mcp_server._auto_sync_last_error == "db unreachable"



class TestStatusAutoSyncDegraded:
    """status() の auto_sync_degraded フィールド（issue #234）。"""

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

    def test_not_degraded_when_no_error(self):
        """_auto_sync_last_error が None のとき degraded=False。"""
        with patch("shiori.mcp_server._auto_sync_last_error", None), \
             patch("shiori.mcp_server._auto_sync_thread", MagicMock(is_alive=lambda: True)):
            result = self._run_status(MagicMock())
        assert result["auto_sync_degraded"] is False

    def test_degraded_when_last_error_set(self):
        """_auto_sync_last_error が設定されていれば degraded=True。"""
        with patch("shiori.mcp_server._auto_sync_last_error", "db unreachable"), \
             patch("shiori.mcp_server._auto_sync_thread", MagicMock(is_alive=lambda: True)):
            result = self._run_status(MagicMock())
        assert result["auto_sync_degraded"] is True

    def test_degraded_when_consecutive_failures_exceed_threshold(self):
        """per-repo consecutive_failures が閾値(10s interval → 6)を超えていれば degraded=True。"""
        mock_settings = MagicMock()
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings", mock_settings),
            patch("shiori.mcp_server._auto_sync_last_error", None),
            patch("shiori.mcp_server._auto_sync_thread", MagicMock(is_alive=lambda: True)),
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
                        "last_error": "git fetch failed",
                        "consecutive_failures": 7,
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
        assert result["auto_sync_degraded"] is True

    def test_not_degraded_when_consecutive_failures_below_threshold(self):
        """per-repo consecutive_failures が閾値未満なら degraded=False。"""
        mock_settings = MagicMock()
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings", mock_settings),
            patch("shiori.mcp_server._auto_sync_last_error", None),
            patch("shiori.mcp_server._auto_sync_thread", MagicMock(is_alive=lambda: True)),
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
                        "last_error": "git fetch failed",
                        "consecutive_failures": 2,
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
        assert result["auto_sync_degraded"] is False
