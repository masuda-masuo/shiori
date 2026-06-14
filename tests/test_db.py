"""db モジュールのユニットテスト（issue #54）。"""

from __future__ import annotations

from unittest.mock import MagicMock

from shiori.db import get_pr_changes, get_pr_head_sha, upsert_pr_changes


# ── get_pr_changes ──


class TestGetPrChanges:
    """get_pr_changes の振る舞い。"""

    def _mock_conn(self, rows: list[tuple]):
        """pr_changes の SELECT 結果を返すモック接続を作る。"""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_returns_files_and_head_sha(self):
        """ファイル一覧と head_sha を正しく返す（1クエリで両方取得）。"""
        conn, cursor = self._mock_conn(
            [
                ("src/a.py", "modified", 5, 2, 7, "url_a", "abc1234"),
                ("src/b.py", "added", 10, 0, 10, "url_b", "abc1234"),
            ],
        )
        files, sha = get_pr_changes(conn, "o/r", 42)

        assert sha == "abc1234"
        assert len(files) == 2
        assert files[0] == {
            "path": "src/a.py",
            "status": "modified",
            "additions": 5,
            "deletions": 2,
            "changes": 7,
            "blob_url": "url_a",
        }
        assert files[1]["path"] == "src/b.py"

    def test_returns_empty_list_and_none_when_no_rows(self):
        """pr_changes に行がない場合、空リストと None を返す。"""
        conn, cursor = self._mock_conn([])

        files, sha = get_pr_changes(conn, "o/r", 42)

        assert files == []
        assert sha is None

    def test_excludes_sentinel_rows(self):
        """path が空文字の sentinel 行は files に含まれず、head_sha は取得される。"""
        conn, cursor = self._mock_conn(
            [
                ("", None, None, None, None, None, "abc1234"),  # sentinel
            ],
        )
        files, sha = get_pr_changes(conn, "o/r", 42)

        assert files == []
        assert sha == "abc1234"

    def test_uses_order_by_path_in_query(self):
        """SQL に ORDER BY path が含まれている。ソートは DB 側に委譲。"""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn.cursor.return_value.__enter__.return_value = cursor

        get_pr_changes(conn, "o/r", 42)

        sql = cursor.execute.call_args_list[0][0][0]
        assert "ORDER BY path" in sql


# ── upsert_pr_changes ──


class TestUpsertPrChanges:
    """upsert_pr_changes の振る舞い。"""

    def test_deletes_existing_and_inserts_new(self):
        """既存行を DELETE してから新しい行を INSERT する。commit は呼ばない。"""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        files = [
            {
                "filename": "src/a.py",
                "status": "modified",
                "additions": 5,
                "deletions": 2,
                "changes": 7,
                "blob_url": "https://example.com/blob",
            },
            {
                "filename": "src/b.py",
                "status": "added",
                "additions": 10,
                "deletions": 0,
                "changes": 10,
                "blob_url": "https://example.com/blob2",
            },
        ]

        upsert_pr_changes(conn, "o/r", 42, "abc1234", files)

        # DELETE が 1 回呼ばれたか
        cursor.execute.assert_any_call(
            "DELETE FROM pr_changes WHERE repo = %s AND issue_no = %s",
            ("o/r", 42),
        )

        # INSERT がファイル数だけ呼ばれたか
        assert cursor.execute.call_count == 3  # 1 DELETE + 2 INSERT

        # commit は呼ばれない（呼び出し側の責任）
        conn.commit.assert_not_called()

    def test_empty_files_list_inserts_sentinel(self):
        """ファイル0件の場合、head_sha を保持する sentinel 行を挿入する。"""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        upsert_pr_changes(conn, "o/r", 42, "abc1234", [])

        # DELETE + sentinel INSERT の 2 回
        assert cursor.execute.call_count == 2

        # sentinel INSERT のパラメータを検証
        # path='' は SQL に直書きのため、パラメータは (repo, issue_no, head_sha) の 3 要素
        sentinel_params = cursor.execute.call_args_list[1][0][1]
        assert len(sentinel_params) == 3
        assert sentinel_params[0] == "o/r"       # repo
        assert sentinel_params[1] == 42           # issue_no
        assert sentinel_params[2] == "abc1234"    # head_sha

    def test_handles_none_fields(self):
        """blob_url 等が None でも正しく扱われる。"""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        files = [
            {
                "filename": "deleted.py",
                "status": "removed",
                "additions": 0,
                "deletions": 5,
                "changes": 5,
                "blob_url": None,
            },
        ]

        upsert_pr_changes(conn, "o/r", 42, "def5678", files)

        insert_params = cursor.execute.call_args_list[1][0][1]
        assert insert_params[3] == "deleted.py"  # path
        assert insert_params[4] == "removed"     # status
        assert insert_params[8] is None          # blob_url


# ── get_pr_head_sha ──


class TestGetPrHeadSha:
    """get_pr_head_sha の振る舞い。"""

    def test_returns_sha_when_exists(self):
        """保存済みの head_sha を返す（sentinel 行含む）。"""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = ("abc1234",)
        conn.cursor.return_value.__enter__.return_value = cursor

        result = get_pr_head_sha(conn, "o/r", 42)

        assert result == "abc1234"

    def test_returns_none_when_no_rows(self):
        """pr_changes に行がない場合は None を返す。"""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        conn.cursor.return_value.__enter__.return_value = cursor

        result = get_pr_head_sha(conn, "o/r", 42)

        assert result is None
