"""db モジュールのユニットテスト（issue #54）。"""

from __future__ import annotations

from unittest.mock import MagicMock

from shiori.db import get_pr_changes, get_pr_head_sha, upsert_pr_changes


# ── get_pr_changes ──


class TestGetPrChanges:
    """get_pr_changes の振る舞い。"""

    def _mock_conn(self, rows: list[tuple], head_sha: str | None = None):
        """pr_changes の SELECT 結果を返すモック接続を作る。"""
        conn = MagicMock()
        cursor = MagicMock()
        # 1 回目の fetchall: ファイル一覧
        # 2 回目の fetchone: head_sha
        cursor.fetchall.return_value = rows
        if rows:
            cursor.fetchone.return_value = (head_sha,) if head_sha else None
        else:
            cursor.fetchone.return_value = None
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_returns_files_and_head_sha(self):
        """ファイル一覧と head_sha を正しく返す。"""
        conn, cursor = self._mock_conn(
            [
                ("src/a.py", "modified", 5, 2, 7, "https://github.com/o/r/blob/sha/src/a.py"),
                ("src/b.py", "added", 10, 0, 10, "https://github.com/o/r/blob/sha/src/b.py"),
            ],
            head_sha="abc1234",
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
            "blob_url": "https://github.com/o/r/blob/sha/src/a.py",
        }
        assert files[1]["path"] == "src/b.py"
        assert files[1]["status"] == "added"

    def test_returns_empty_list_and_none_when_no_rows(self):
        """pr_changes に行がない場合、空リストと None を返す。"""
        conn, cursor = self._mock_conn([])

        files, sha = get_pr_changes(conn, "o/r", 42)

        assert files == []
        assert sha is None

    def test_returns_none_head_sha_when_files_exist_but_sha_is_null(self):
        """ファイルはあるが head_sha が NULL の場合。"""
        conn, cursor = self._mock_conn(
            [("src/x.py", "modified", 1, 1, 2, "url")],
            head_sha=None,  # fetchone が None を返す
        )
        cursor.fetchone.return_value = None

        files, sha = get_pr_changes(conn, "o/r", 42)

        assert len(files) == 1
        assert sha is None

    def test_files_sorted_by_path(self):
        """ファイル一覧が path でソートされている。"""
        conn, cursor = self._mock_conn(
            [
                ("c.txt", "modified", 1, 1, 2, "url3"),
                ("a.py", "added", 1, 0, 1, "url1"),
                ("b.go", "removed", 0, 1, 1, "url2"),
            ],
            head_sha="sha",
        )
        # SQL の ORDER BY path でソート済みのはず
        files, _ = get_pr_changes(conn, "o/r", 42)
        assert [f["path"] for f in files] == ["a.py", "b.go", "c.txt"]


# ── upsert_pr_changes ──


class TestUpsertPrChanges:
    """upsert_pr_changes の振る舞い。"""

    def test_deletes_existing_and_inserts_new(self):
        """既存行を DELETE してから新しい行を INSERT する。"""
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

    def test_empty_files_list(self):
        """空のファイルリストでも動作する（DELETE のみ）。"""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        upsert_pr_changes(conn, "o/r", 42, "abc1234", [])

        # DELETE のみ、INSERT はなし
        cursor.execute.assert_called_once_with(
            "DELETE FROM pr_changes WHERE repo = %s AND issue_no = %s",
            ("o/r", 42),
        )

    def test_handles_none_fields(self):
        """status 等が None でも正しく扱われる。"""
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
                "blob_url": None,  # None の blob_url
            },
        ]

        upsert_pr_changes(conn, "o/r", 42, "def5678", files)

        # INSERT の引数を検証
        insert_call = cursor.execute.call_args_list[1]  # 2 番目の呼び出しが INSERT
        assert insert_call[0][1][4] == "deleted.py"  # path
        assert insert_call[0][1][5] == "removed"  # status
        assert insert_call[0][1][9] is None  # blob_url


# ── get_pr_head_sha ──


class TestGetPrHeadSha:
    """get_pr_head_sha の振る舞い。"""

    def test_returns_sha_when_exists(self):
        """保存済みの head_sha を返す。"""
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
