"""github_sync のユニットテスト（issue #25, #54）。

_should_index の allowlist 判定ロジック、_sync_pr_changes の head_sha 比較による
スキップ／再取得ロジックを検証する。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shiori.config import Settings
from shiori.github_sync import _git, _is_bot, _propagate_issue_state, _should_index, _sync_pr_changes


# ---------------------------------------------------------------------------
# _should_index
# ---------------------------------------------------------------------------


class TestShouldIndex:
    """_should_index の振る舞い。"""

    def test_human_always_indexed(self):
        """is_bot=False なら allowlist に関係なく常に True。"""
        settings = Settings()  # index_bot_logins 空
        assert _should_index(False, "alice", settings) is True
        assert _should_index(False, None, settings) is True
        assert _should_index(False, "", settings) is True

    def test_bot_excluded_when_allowlist_empty(self):
        """allowlist 未設定なら bot は常に False。"""
        settings = Settings()  # index_bot_logins 空
        assert _should_index(True, "dependabot[bot]", settings) is False
        assert _should_index(True, "mcp-launcher-masuda[bot]", settings) is False
        assert _should_index(True, None, settings) is False

    def test_bot_in_allowlist_indexed(self):
        """allowlist に含まれる bot login は索引対象。"""
        settings = Settings()
        settings.index_bot_logins = {"mcp-launcher-masuda[bot]"}
        assert _should_index(True, "mcp-launcher-masuda[bot]", settings) is True

    def test_bot_not_in_allowlist_excluded(self):
        """allowlist 外の bot は従来どおり除外。"""
        settings = Settings()
        settings.index_bot_logins = {"mcp-launcher-masuda[bot]"}
        assert _should_index(True, "dependabot[bot]", settings) is False
        assert _should_index(True, "renovate[bot]", settings) is False

    def test_allowlist_case_insensitive(self):
        """allowlist は大文字小文字を区別しない（config 側で lower 済み、author も lower で比較）。"""
        settings = Settings()
        settings.index_bot_logins = {"mcp-launcher-masuda[bot]"}
        assert _should_index(True, "Mcp-Launcher-Masuda[bot]", settings) is True
        assert _should_index(True, "MCP-LAUNCHER-MASUDA[BOT]", settings) is True

    def test_multiple_bots_in_allowlist(self):
        """カンマ区切りで複数 bot を指定できる。"""
        settings = Settings()
        settings.index_bot_logins = {"bot-a[bot]", "bot-b[bot]", "bot-c[bot]"}
        assert _should_index(True, "bot-a[bot]", settings) is True
        assert _should_index(True, "bot-b[bot]", settings) is True
        assert _should_index(True, "bot-c[bot]", settings) is True
        assert _should_index(True, "bot-d[bot]", settings) is False

    def test_author_none_with_allowlist(self):
        """author が None で is_bot=True なら allowlist に関わらず False。"""
        settings = Settings()
        settings.index_bot_logins = {"mcp-launcher-masuda[bot]"}
        assert _should_index(True, None, settings) is False


# ---------------------------------------------------------------------------
# _is_bot
# ---------------------------------------------------------------------------


class TestIsBot:
    """_is_bot の振る舞い。"""

    def test_none_user(self):
        assert _is_bot(None) is False

    def test_human_user(self):
        assert _is_bot({"login": "alice", "type": "User"}) is False

    def test_bot_type(self):
        assert _is_bot({"login": "dependabot", "type": "Bot"}) is True

    def test_bot_suffix_in_login(self):
        """login が [bot] で終わる場合も bot 判定。"""
        assert _is_bot({"login": "my-app[bot]", "type": "User"}) is True

    def test_bot_suffix_case_insensitive(self):
        """[bot] 判定は大文字小文字を区別しない。"""
        assert _is_bot({"login": "My-App[BOT]", "type": "User"}) is True


# ---------------------------------------------------------------------------
# _propagate_issue_state (issue #56)
# ---------------------------------------------------------------------------


class TestPropagateIssueState:
    """_propagate_issue_state の振る舞い（issue #56）。

    issue_items.state 変更時に chunks.state を一括更新する。
    """

    def _mock_cursor(self, rowcount: int = 0):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.rowcount = rowcount
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_propagate_state_from_issue_items_to_all_chunks(self):
        """issue_items の state を全 chunk に伝播する UPDATE が発行される。"""
        conn, cursor = self._mock_cursor(rowcount=5)

        _propagate_issue_state(conn, "masuda-masuo/shiori", 50, "closed")

        cursor.execute.assert_called_once_with(
            "UPDATE chunks SET state = %s WHERE repo = %s AND issue_no = %s",
            ("closed", "masuda-masuo/shiori", 50),
        )

    def test_propagate_state_none(self):
        """state=None も正しく伝播される（例: issue 本文が空のケース）。"""
        conn, cursor = self._mock_cursor(rowcount=0)

        _propagate_issue_state(conn, "masuda-masuo/shiori", 50, None)

        cursor.execute.assert_called_once_with(
            "UPDATE chunks SET state = %s WHERE repo = %s AND issue_no = %s",
            (None, "masuda-masuo/shiori", 50),
        )

    def test_propagate_state_open_to_closed_transition(self):
        """open → closed の遷移が正しく伝播される。"""
        conn, cursor = self._mock_cursor(rowcount=3)

        _propagate_issue_state(conn, "masuda-masuo/shiori", 50, "open")
        _propagate_issue_state(conn, "masuda-masuo/shiori", 50, "closed")

        assert cursor.execute.call_count == 2
        first_call = cursor.execute.call_args_list[0]
        second_call = cursor.execute.call_args_list[1]
        assert first_call[0][1] == ("open", "masuda-masuo/shiori", 50)
        assert second_call[0][1] == ("closed", "masuda-masuo/shiori", 50)


# ---------------------------------------------------------------------------
# _sync_pr_changes (issue #54)
# ---------------------------------------------------------------------------


class TestSyncPrChanges:
    """_sync_pr_changes の振る舞い（issue #54）。

    head_sha 比較によるスキップ／force-push 時の再取得ロジックを検証する。
    """

    API = "https://api.github.com"

    def _mock_client(self, head_sha: str, files: list[dict] | None = None,
                     pull_status: int = 200, files_status: int = 200):
        """httpx.Client をモックする。

        - pull_status/files_status: HTTP ステータスコード
        - head_sha: PR の head.sha
        - files: ファイル一覧（None なら files エンドポイントを呼ばせない）
        """
        client = MagicMock()

        def _get(url, **kwargs):
            resp = MagicMock()
            resp.links = {}
            if "/pulls/" in url and "/files" not in url:
                resp.status_code = pull_status
                if pull_status == 200:
                    resp.json.return_value = {"head": {"sha": head_sha}}
                else:
                    resp.raise_for_status.side_effect = __import__("httpx").HTTPStatusError(
                        "error", request=MagicMock(), response=resp
                    )
            elif "/files" in url:
                resp.status_code = files_status
                if files_status == 200:
                    resp.json.return_value = files or []
                else:
                    resp.raise_for_status.side_effect = __import__("httpx").HTTPStatusError(
                        "error", request=MagicMock(), response=resp
                    )
            else:
                resp.status_code = 404
                resp.raise_for_status.side_effect = __import__("httpx").HTTPStatusError(
                    "error", request=MagicMock(), response=resp
                )
            return resp

        client.get.side_effect = _get
        return client

    def _mock_conn(self, prev_sha: str | None = None):
        """pr_changes の head_sha 問い合わせをモックする。"""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (prev_sha,) if prev_sha else None
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    @patch("shiori.github_sync.upsert_pr_changes")
    @patch("shiori.github_sync.get_pr_head_sha")
    def test_skips_when_head_sha_unchanged(self, mock_get_sha, mock_upsert):
        """head_sha が前回と同じならファイル一覧を取得せずスキップする。"""
        client = self._mock_client(head_sha="abc1234")
        conn = MagicMock()
        mock_get_sha.return_value = "abc1234"  # 前回と同じ

        _sync_pr_changes(client, conn, "o/r", 42)

        # PR 詳細は取得されるが、files は取得されない
        pull_calls = [c for c in client.get.call_args_list
                      if "/files" not in str(c)]
        files_calls = [c for c in client.get.call_args_list
                       if "/files" in str(c)]
        assert len(pull_calls) >= 1
        assert len(files_calls) == 0
        mock_upsert.assert_not_called()

    @patch("shiori.github_sync.upsert_pr_changes")
    @patch("shiori.github_sync.get_pr_head_sha")
    def test_fetches_files_when_head_sha_changed(self, mock_get_sha, mock_upsert):
        """head_sha が変化した場合（force-push）、ファイル一覧を再取得する。"""
        files = [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "changes": 1, "blob_url": "url"}]
        client = self._mock_client(head_sha="new_sha", files=files)
        conn = MagicMock()
        mock_get_sha.return_value = "old_sha"  # 前回と異なる

        _sync_pr_changes(client, conn, "o/r", 42)

        mock_upsert.assert_called_once_with(conn, "o/r", 42, "new_sha", files)

    @patch("shiori.github_sync.upsert_pr_changes")
    @patch("shiori.github_sync.get_pr_head_sha")
    def test_fetches_files_when_not_yet_synced(self, mock_get_sha, mock_upsert):
        """未同期（prev_sha=None）の場合、ファイル一覧を取得する。"""
        files = [{"filename": "b.py", "status": "added", "additions": 10, "deletions": 0, "changes": 10, "blob_url": "url"}]
        client = self._mock_client(head_sha="abc", files=files)
        conn = MagicMock()
        mock_get_sha.return_value = None  # 未同期

        _sync_pr_changes(client, conn, "o/r", 42)

        mock_upsert.assert_called_once_with(conn, "o/r", 42, "abc", files)

    @patch("shiori.github_sync.upsert_pr_changes")
    @patch("shiori.github_sync.get_pr_head_sha")
    def test_handles_empty_head_sha(self, mock_get_sha, mock_upsert):
        """PR 詳細の head_sha が None なら何もせず return。"""
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"head": {"sha": None}}
        client.get.return_value = resp
        conn = MagicMock()

        _sync_pr_changes(client, conn, "o/r", 42)

        mock_upsert.assert_not_called()

    @patch("shiori.github_sync.upsert_pr_changes")
    @patch("shiori.github_sync.get_pr_head_sha")
    def test_handles_pull_api_error(self, mock_get_sha, mock_upsert):
        """PR 詳細 API がエラーを返しても例外を投げず return。"""
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 404
        resp.raise_for_status.side_effect = __import__("httpx").HTTPStatusError(
            "not found", request=MagicMock(), response=resp
        )
        client.get.return_value = resp
        conn = MagicMock()

        # 例外が投げられないことを確認
        _sync_pr_changes(client, conn, "o/r", 42)

        mock_upsert.assert_not_called()


# ---------------------------------------------------------------------------
# _git (safe.directory)
# ---------------------------------------------------------------------------


class TestGit:
    """_git の safe.directory 付与ロジック（issue #48）。"""

    @patch("shiori.github_sync.subprocess.run")
    def test_safe_directory_added_when_cwd_given(self, mock_run):
        """cwd 指定時に -c safe.directory=<cwd> が渡ること。"""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _git(["status"], cwd="/data/repos/foo")
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd[:3] == ["git", "-c", "safe.directory=/data/repos/foo"]

    @patch("shiori.github_sync.subprocess.run")
    def test_no_safe_directory_when_cwd_none(self, mock_run):
        """cwd=None（clone 時など）には safe.directory が付与されないこと。"""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _git(["clone", "url", "dest"])
        called_cmd = mock_run.call_args[0][0]
        assert "-c" not in called_cmd
