"""Unit tests for the db module (issue #54, #72)."""

from __future__ import annotations

from unittest.mock import MagicMock, call

from shiori.db import (
    bulk_insert_chunks,
    create_heavy_indexes,
    drop_heavy_indexes,
    get_pr_changes,
    get_pr_head_sha,
    migrate_light,
    upsert_pr_changes,
)


# ── get_pr_changes ──


class TestGetPrChanges:
    """Behavior of get_pr_changes."""

    def _mock_conn(self, rows: list[tuple]):
        """Create a mock connection returning pr_changes SELECT results."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_returns_files_and_head_sha(self):
        """Returns file list and head_sha correctly (both from single query)."""
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
        """Empty list and None when pr_changes has no rows."""
        conn, cursor = self._mock_conn([])

        files, sha = get_pr_changes(conn, "o/r", 42)

        assert files == []
        assert sha is None

    def test_excludes_sentinel_rows(self):
        """Sentinel rows (empty path) are excluded from files, head_sha is still returned."""
        conn, cursor = self._mock_conn(
            [
                ("", None, None, None, None, None, "abc1234"),  # sentinel
            ],
        )
        files, sha = get_pr_changes(conn, "o/r", 42)

        assert files == []
        assert sha == "abc1234"

    def test_uses_order_by_path_in_query(self):
        """SQL includes ORDER BY path; sorting delegated to DB."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        conn.cursor.return_value.__enter__.return_value = cursor

        get_pr_changes(conn, "o/r", 42)

        sql = cursor.execute.call_args_list[0][0][0]
        assert "ORDER BY path" in sql


# ── upsert_pr_changes ──


class TestUpsertPrChanges:
    """Behavior of upsert_pr_changes."""

    def test_deletes_existing_and_inserts_new(self):
        """Deletes existing rows then inserts new rows. Does not commit."""
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
        """When files list is empty, inserts a sentinel row to preserve head_sha."""
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
        """Handles None fields like blob_url correctly."""
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
    """Behavior of get_pr_head_sha."""

    def test_returns_sha_when_exists(self):
        """Returns stored head_sha (includes sentinel rows)."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = ("abc1234",)
        conn.cursor.return_value.__enter__.return_value = cursor

        result = get_pr_head_sha(conn, "o/r", 42)

        assert result == "abc1234"

    def test_returns_none_when_no_rows(self):
        """Returns None when pr_changes has no rows."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        conn.cursor.return_value.__enter__.return_value = cursor

        result = get_pr_head_sha(conn, "o/r", 42)

        assert result is None


# ── migrate_light / create_heavy_indexes / drop_heavy_indexes（issue #72）──


class TestMigrateLight:
    """migrate_light: creates tables and btree indexes only, no heavy indexes."""

    def _mock_conn(self):
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_creates_tables_and_btree_indexes_only(self):
        """migrate_light executes SCHEMA_SQL but does not create HNSW indexes.

        SCHEMA_SQL itself includes CREATE EXTENSION pgroonga, but that is
        an extension load, not index creation.
        """
        from shiori.config import Settings
        conn, cursor = self._mock_conn()

        settings = Settings()
        migrate_light(conn, settings)

        # SCHEMA_SQL が実行されている（CREATE TABLE 等を含む）
        executed_sqls = [
            str(call_args[0][0])
            for call_args in cursor.execute.call_args_list
            if call_args[0]
        ]
        joined = " ".join(executed_sqls)
        assert "CREATE TABLE IF NOT EXISTS chunks" in joined
        # HNSW 索引は作らない
        assert "hnsw" not in joined.lower()
        # pgroonga 索引（CREATE INDEX ... USING pgroonga）は作らない
        assert "create index" not in joined.lower() or "using pgroonga" not in joined.lower()

    def test_runs_alter_statements(self):
        """migrate_light also runs ALTER statements."""
        from shiori.config import Settings
        conn, cursor = self._mock_conn()

        settings = Settings()
        migrate_light(conn, settings)

        # ALTER が実行されている
        executed_sqls = [
            str(call_args[0][0])
            for call_args in cursor.execute.call_args_list
            if call_args[0]
        ]
        joined = " ".join(executed_sqls)
        assert "ALTER TABLE chunks" in joined
        assert "ADD COLUMN IF NOT EXISTS" in joined


class TestCreateHeavyIndexes:
    """create_heavy_indexes: creates HNSW + pgroonga indexes."""

    def _mock_conn(self):
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_creates_hnsw_and_pgroonga_indexes(self):
        """create_heavy_indexes creates HNSW and pgroonga(content/symbols) indexes."""
        conn, cursor = self._mock_conn()

        create_heavy_indexes(conn)

        executed_sqls = [
            str(call_args[0][0])
            for call_args in cursor.execute.call_args_list
            if call_args[0]
        ]
        joined_lower = " ".join(executed_sqls).lower()
        assert "hnsw" in joined_lower
        # pgroonga 索引（CREATE INDEX ... USING pgroonga）が含まれる
        assert "using pgroonga" in joined_lower


class TestDropHeavyIndexes:
    """drop_heavy_indexes: DROPs the 3 heavy indexes."""

    def _mock_conn(self):
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_drops_three_indexes(self):
        """drop_heavy_indexes executes 3 DROP INDEX IF EXISTS statements."""
        conn, cursor = self._mock_conn()

        drop_heavy_indexes(conn)

        executed_sqls = [
            str(call_args[0][0])
            for call_args in cursor.execute.call_args_list
            if call_args[0]
        ]
        drop_calls = [s for s in executed_sqls if "DROP INDEX IF EXISTS" in s]
        assert len(drop_calls) == 3

        joined = " ".join(drop_calls)
        assert "chunks_embedding_hnsw" in joined
        assert "chunks_content_pgroonga" in joined
        assert "chunks_symbols_pgroonga" in joined


# ── bulk_insert_chunks（issue #72）──


class TestBulkInsertChunks:
    """bulk_insert_chunks: batch insert via executemany."""

    def _mock_conn(self):
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_empty_list_noops(self):
        """Empty list does nothing."""
        conn, cursor = self._mock_conn()

        bulk_insert_chunks(conn, [])

        cursor.executemany.assert_not_called()

    def test_calls_executemany_with_correct_number_of_rows(self):
        """executemany is called with correct row count."""
        conn, cursor = self._mock_conn()

        rows = [
            {
                "chunk_key": f"doc:o/r:file{i}.md",
                "chunk_index": 0,
                "source_type": "doc",
                "repo": "o/r",
                "content": f"content {i}",
                "embedding": [0.1, 0.2, 0.3],
                "path": f"file{i}.md",
                "issue_no": None,
                "comment_id": None,
                "language": "ja",
                "heading_path": None,
                "state": None,
                "author": None,
                "line": None,
                "end_line": None,
                "commit_sha": None,
                "prog_lang": None,
                "symbols": None,
                "created_at": None,
                "updated_at": None,
                "url": None,
            }
            for i in range(3)
        ]

        bulk_insert_chunks(conn, rows)

        cursor.executemany.assert_called_once()
        args = cursor.executemany.call_args[0]
        # 第一引数: SQL 文字列
        assert "INSERT INTO chunks" in args[0]
        assert "ON CONFLICT" in args[0]
        # 第二引数: パラメータリスト（3 行）
        assert len(args[1]) == 3

    def test_embedding_is_vector_literal(self):
        """embedding is converted to '[x,y,z]' format via vec_literal."""
        conn, cursor = self._mock_conn()

        rows = [
            {
                "chunk_key": "doc:o/r:f.md",
                "chunk_index": 0,
                "source_type": "doc",
                "repo": "o/r",
                "content": "hello",
                "embedding": [0.1, 0.2, 0.3],
                "path": "f.md",
                "issue_no": None,
                "comment_id": None,
                "language": "en",
                "heading_path": None,
                "state": None,
                "author": None,
                "line": None,
                "end_line": None,
                "commit_sha": None,
                "prog_lang": None,
                "symbols": None,
                "created_at": None,
                "updated_at": None,
                "url": None,
            },
        ]

        bulk_insert_chunks(conn, rows)

        params = cursor.executemany.call_args[0][1][0]
        # embedding は 11 番目の要素（0-indexed: 10）
        # 0:chunk_key 1:chunk_index 2:source_type 3:repo 4:path
        # 5:issue_no 6:comment_id 7:language 8:heading_path 9:content
        # 10:embedding
        embedding_str = params[10]
        assert embedding_str == "[0.100000,0.200000,0.300000]"
