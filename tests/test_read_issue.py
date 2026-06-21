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

from shiori.mcp_server import read_issue, settings


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
        # with _conn() as conn で conn が自分自身を返すようにする
        conn.__enter__.return_value = conn
        return conn

    def test_default_returns_all_items(self):
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

        monkeypatch.setattr(settings, "index_bot_logins",
                            {"allowlisted-bot[bot]", "another-bot[bot]"})
        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
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

        monkeypatch.setattr(settings, "index_bot_logins",
                            {"allowlisted-bot[bot]"})
        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
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

        monkeypatch.setattr(settings, "index_bot_logins",
                            {"allowlisted-bot[bot]"})
        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            with pytest.raises(ValueError, match="全項目が allowlist 外の bot"):
                read_issue(42, exclude_noise_bots=True)

    def test_exclude_noise_bots_with_author_none(self, monkeypatch):
        """author が None の bot は allowlist にマッチせず除外される。"""
        rows = [
            _row(comment_id=0, author="human-user", is_bot=False),
            _row(comment_id=1, author=None, is_bot=True),
        ]
        mock_conn = self._mock_conn_with_rows(rows)

        monkeypatch.setattr(settings, "index_bot_logins",
                            {"allowlisted-bot[bot]"})
        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            result = read_issue(42, exclude_noise_bots=True)

        # author=None の bot は除外される、人間は残る
        assert len(result["items"]) == 1
        assert result["items"][0]["author"] == "human-user"

    def test_no_rows_raises_value_error(self):
        """行が 0 件の場合は ValueError（既存動作）。"""
        mock_conn = self._mock_conn_with_rows([])

        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            with pytest.raises(ValueError, match="索引されていません"):
                read_issue(42)


class TestReadIssueNumbers:
    """read_issue の numbers パラメータ（バッチ取得）の振る舞い（issue #86）。"""

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
        """複数番号をすべて成功で取得する。"""
        with patch("shiori.mcp_server._read_issue_single") as mock, \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
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

    def test_batch_partial_unindexed(self):
        """未索引の番号が混在しても全体を落とさず、success/error を併記する。"""
        with patch("shiori.mcp_server._read_issue_single") as mock, \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            mock.side_effect = [
                self._make_result(42, "issue 42"),
                ValueError("#43 は索引されていません（ingest 済みですか？）"),
                self._make_result(44, "issue 44"),
            ]
            result = read_issue(numbers=[42, 43, 44])

        assert isinstance(result, list)
        assert len(result) == 3
        assert result[0]["status"] == "ok"
        assert result[0]["number"] == 42
        assert result[1]["status"] == "error"
        assert result[1]["number"] == 43
        assert "索引されていません" in result[1]["error"]
        assert result[2]["status"] == "ok"
        assert result[2]["number"] == 44

    def test_batch_empty_array(self):
        """空配列は空リストを返す。"""
        with patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            result = read_issue(numbers=[])

        assert isinstance(result, list)
        assert result == []

    def test_batch_duplicate_numbers(self):
        """重複番号はそれぞれ個別に処理される。"""
        with patch("shiori.mcp_server._read_issue_single") as mock, \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            mock.side_effect = [
                self._make_result(42, "issue 42"),
                self._make_result(42, "issue 42"),
            ]
            result = read_issue(numbers=[42, 42])

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["number"] == 42
        assert result[0]["status"] == "ok"
        assert result[1]["number"] == 42
        assert result[1]["status"] == "ok"

    def test_batch_applies_exclude_noise_bots(self):
        """exclude_noise_bots がバッチ内の全取得に適用される。"""
        with patch("shiori.mcp_server._read_issue_single") as mock, \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            mock.return_value = self._make_result(42, "issue 42")
            read_issue(numbers=[42, 43], exclude_noise_bots=True)

        assert mock.call_count == 2
        for call_args in mock.call_args_list:
            # _read_issue_single(target, number, exclude_noise_bots) — positional
            assert call_args[0][2] is True
            assert call_args[0][0] == "o/r"

    def test_batch_passes_repo_to_single(self):
        """repo が _resolve_repo と _read_issue_single に正しく渡される。"""
        with patch("shiori.mcp_server._read_issue_single") as mock_single, \
             patch("shiori.mcp_server._resolve_repo") as mock_resolve:
            mock_resolve.return_value = "my/repo"
            mock_single.return_value = self._make_result(42, "issue 42")
            read_issue(numbers=[42], repo="my/repo")

        mock_resolve.assert_called_once_with("my/repo")
        mock_single.assert_called_once_with("my/repo", 42, False)

    def test_batch_all_errors(self):
        """全件エラーでも例外を送出せず、全件 error を返す。"""
        with patch("shiori.mcp_server._read_issue_single") as mock, \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            mock.side_effect = ValueError("#99 は索引されていません")
            result = read_issue(numbers=[99])

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["status"] == "error"
        assert result[0]["number"] == 99
        assert "索引されていません" in result[0]["error"]

    def test_number_and_numbers_missing_raises(self):
        """number も numbers も指定しない場合は ValueError。"""
        with patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            with pytest.raises(ValueError, match="number または numbers"):
                read_issue()

    def test_single_number_backward_compatible(self):
        """numbers なしの単発呼び出しは従来通り dict を返す（後方互換）。"""
        rows = [
            _row(comment_id=0, author="human-user", is_bot=False),
        ]
        mock_conn = self._mock_conn_with_rows(rows)

        with patch("shiori.mcp_server._conn", return_value=mock_conn), \
             patch("shiori.mcp_server._resolve_repo", return_value="o/r"):
            result = read_issue(42)

        assert isinstance(result, dict)
        assert result["number"] == 42
        assert "status" not in result
        assert len(result["items"]) == 1

    def _mock_conn_with_rows(self, rows):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn.cursor.return_value.__enter__.return_value = cursor
        conn.__enter__.return_value = conn
        return conn
