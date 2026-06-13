"""github_sync のユニットテスト（issue #25）。

_should_index の allowlist 判定ロジックを検証する。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from shiori.config import Settings
from shiori.github_sync import _git, _is_bot, _should_index


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
