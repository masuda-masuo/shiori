"""shiori_read_issue の exclude_noise_bots パラメータのユニットテスト（issue #44）。

テスト対象:
- read_issue: exclude_noise_bots=False（既定）で全件返す
- read_issue: exclude_noise_bots=True で allowlist 外の bot を除外
- read_issue: 全件フィルタ時の ValueError
- read_issue: 行なし時の ValueError（既存動作）
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shiori.config import Settings
from shiori.mcp_server import read_issue


# ── テスト用のヘルパー ──


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
    """test 用の行タプルを作る (comment_id, kind, title, author, is_bot, state, path, line, body, url, created_at)。"""
    from datetime import datetime
    ca = datetime.fromisoformat(created_at) if created_at else None
    return (comment_id, kind, title, author, is_bot, state, path, line, body, url, ca)


class TestReadIssueExcludeNoiseBots:
    """read_issue の exclude_noise_bots パラメータの振る舞い。"""

    def _mock_conn_with_rows(self, rows: list[tuple]) -> MagicMock:
        """指定行を返すモック接続を作る。"""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn

    def test_default_returns_all_items(self, monkeypatch):
        """exclude_noise_bots=False（既定）では bot 含め全件返す。"""
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
        """exclude_noise_bots=True で allowlist 外の bot のみ除外する。"""
        rows = [
            _row(comment_id=0, author="human-user", is_bot=False),
            _row(comment_id=1, author="allowlisted-bot[bot]", is_bot=True),
            _row(comment_id=2, author="dependabot[bot]", is_bot=True),
        ]
        mock_conn = self._mock_conn_with_rows(rows)

        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"), \
             patch.object(Settings, "index_bot_logins", {"allowlisted-bot[bot]", "another-bot[bot]"}):
            result = read_issue(42, exclude_noise_bots=True)

        assert len(result["items"]) == 2
        assert result["items"][0]["author"] == "human-user"
        assert result["items"][1]["author"] == "allowlisted-bot[bot]"

    def test_exclude_noise_bots_keeps_allowlisted_bot_with_different_case(self, monkeypatch):
        """allowlist は大文字小文字無視でマッチする。"""
        rows = [
            _row(comment_id=0, author="Allowlisted-Bot[bot]", is_bot=True),
        ]
        mock_conn = self._mock_conn_with_rows(rows)

        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"), \
             patch.object(Settings, "index_bot_logins", {"allowlisted-bot[bot]"}):
            result = read_issue(42, exclude_noise_bots=True)

        assert len(result["items"]) == 1
        assert result["items"][0]["author"] == "Allowlisted-Bot[bot]"

    def test_exclude_noise_bots_raises_when_all_filtered(self, monkeypatch):
        """全件フィルタされた場合は ValueError を送出する。"""
        rows = [
            _row(comment_id=0, author="dependabot[bot]", is_bot=True),
            _row(comment_id=1, author="ci-bot[bot]", is_bot=True),
        ]
        mock_conn = self._mock_conn_with_rows(rows)

        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"), \
             patch.object(Settings, "index_bot_logins", {"allowlisted-bot[bot]"}):
            with pytest.raises(ValueError, match="全項目が allowlist 外の bot"):
                read_issue(42, exclude_noise_bots=True)

    def test_exclude_noise_bots_with_author_none(self, monkeypatch):
        """author が None の bot は allowlist にマッチせず除外される。"""
        rows = [
            _row(comment_id=0, author="human-user", is_bot=False),
            _row(comment_id=1, author=None, is_bot=True),
        ]
        mock_conn = self._mock_conn_with_rows(rows)

        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"), \
             patch.object(Settings, "index_bot_logins", {"allowlisted-bot[bot]"}):
            result = read_issue(42, exclude_noise_bots=True)

        # author=None の bot は除外される、人間は残る
        assert len(result["items"]) == 1
        assert result["items"][0]["author"] == "human-user"

    def test_no_rows_raises_value_error(self, monkeypatch):
        """行が 0 件の場合は ValueError（既存動作）。"""
        mock_conn = self._mock_conn_with_rows([])

        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            with pytest.raises(ValueError, match="索引されていません"):
                read_issue(42)
