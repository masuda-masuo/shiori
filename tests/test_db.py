"""db モジュールのユニットテスト（issue #72）。"""

from __future__ import annotations

from unittest.mock import MagicMock

from shiori.db import (
    bulk_insert_chunks,
    get_code_chunks,
    get_sync_runs,
    record_sync_attempt,
)
from shiori.schema import (
    create_heavy_indexes,
    drop_heavy_indexes,
    migrate_light,
)



# ── migrate_light / create_heavy_indexes / drop_heavy_indexes（issue #72）──

class TestMigrateLight:
    """migrate_light: テーブル・btree 索引のみ作成し、重い索引は作らない。"""

    def _mock_conn(self):
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_creates_tables_and_btree_indexes_only(self):
        """migrate_light は SCHEMA_SQL を実行するが、HNSW 索引は作らない。

        SCHEMA_SQL 自体には CREATE EXTENSION pgroonga が含まれるが、
        それは拡張のロードであって索引作成ではない。
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
        """migrate_light は ALTER 文も実行する。"""
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
    """create_heavy_indexes: HNSW + pgroonga 索引を作成する。"""

    def _mock_conn(self):
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_creates_hnsw_and_pgroonga_indexes(self):
        """create_heavy_indexes は HNSW と pgroonga(content/symbols) を作成する。"""
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
    """drop_heavy_indexes: 3 つの重量索引を DROP する。"""

    def _mock_conn(self):
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_drops_three_indexes(self):
        """drop_heavy_indexes は 3 つの DROP INDEX IF EXISTS を実行する。"""
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
    """bulk_insert_chunks: executemany によるバルク挿入。"""

    def _mock_conn(self):
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_empty_list_noops(self):
        """空リストは何もしない。"""
        conn, cursor = self._mock_conn()

        bulk_insert_chunks(conn, [])

        cursor.executemany.assert_not_called()

    def test_calls_executemany_with_correct_number_of_rows(self):
        """executemany が正しい行数で呼ばれる。"""
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
        """embedding が vec_literal で '[x,y,z]' 形式に変換される。"""
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
        # embedding は 12 番目の要素（0-indexed: 11）
        # 0:chunk_key 1:chunk_index 2:source_type 3:repo 4:path
        # 5:issue_no 6:comment_id 7:kind 8:language 9:heading_path 10:content
        # 11:embedding
        embedding_str = params[11]
        assert embedding_str == "[0.100000,0.200000,0.300000]"

# ── record_sync_attempt / get_sync_runs (issue #187) ──

class TestRecordSyncAttempt:
    """record_sync_attempt の振る舞い（issue #187: 試行の記録）。"""

    def _mock_conn(self):
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_success_upserts_and_commits(self):
        """success=True では last_error=NULL / consecutive_failures=0 にリセットする。"""
        conn, cursor = self._mock_conn()

        record_sync_attempt(conn, "o/r", success=True)

        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]
        assert "consecutive_failures = 0" in sql
        assert "last_error = NULL" in sql
        assert params == ("o/r",)
        conn.commit.assert_called_once()

    def test_failure_increments_and_records_error(self):
        """success=False ではエラーを記録し、consecutive_failures をインクリメントする。"""
        conn, cursor = self._mock_conn()

        record_sync_attempt(conn, "o/r", success=False, error="git fetch failed (exit 128)")

        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]
        assert "consecutive_failures = sync_runs.consecutive_failures + 1" in sql
        assert params == ("o/r", "git fetch failed (exit 128)")
        conn.commit.assert_called_once()

    def test_failure_truncates_long_error(self):
        """異常に長いエラーメッセージは切り詰められる（行の無制限肥大化を防ぐ）。"""
        conn, cursor = self._mock_conn()
        long_error = "x" * 5000

        record_sync_attempt(conn, "o/r", success=False, error=long_error)

        params = cursor.execute.call_args[0][1]
        assert len(params[1]) <= 2000

    def test_failure_with_none_error_stores_empty_string(self):
        """error=None（success=False）でも例外にならず空文字列を記録する。"""
        conn, cursor = self._mock_conn()

        record_sync_attempt(conn, "o/r", success=False, error=None)

        params = cursor.execute.call_args[0][1]
        assert params == ("o/r", "")

class TestGetSyncRuns:
    """get_sync_runs の振る舞い（issue #187: last_attempt_at/last_error/consecutive_failures）。"""

    def _mock_conn(self, rows: list[tuple]):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn

    def test_successful_repo_includes_attempt_fields(self):
        """成功済みリポジトリは success detail と attempt detail の両方を返す。"""
        from datetime import datetime, timezone

        finished_at = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
        last_attempt_at = datetime(2026, 7, 10, 12, 0, 5, tzinfo=timezone.utc)
        conn = self._mock_conn(
            [("o/r", "auto", finished_at, 300, 5, 10, 2, last_attempt_at, None, 0)]
        )

        result = get_sync_runs(conn)

        info = result["o/r"]
        assert info["last_synced_at"] == finished_at.isoformat()
        assert info["age_seconds"] == 300
        assert info["last_attempt_at"] == last_attempt_at.isoformat()
        assert info["last_error"] is None
        assert info["consecutive_failures"] == 0

    def test_never_succeeded_repo_has_null_success_fields(self):
        """一度も成功していないリポジトリでも attempt 情報だけは行として存在する。"""
        from datetime import datetime, timezone

        last_attempt_at = datetime(2026, 7, 10, 12, 0, 5, tzinfo=timezone.utc)
        conn = self._mock_conn(
            [
                (
                    "o/r", None, None, None, None, None, None,
                    last_attempt_at, "git fetch failed (exit 128)", 5,
                )
            ]
        )

        result = get_sync_runs(conn)

        info = result["o/r"]
        assert info["last_synced_at"] is None
        assert info["age_seconds"] is None
        assert info["last_attempt_at"] == last_attempt_at.isoformat()
        assert info["last_error"] == "git fetch failed (exit 128)"
        assert info["consecutive_failures"] == 5

class TestGetCodeChunks:
    """get_code_chunks: API reference reports data retrieval."""

    def _mock_conn(self, rows: list[tuple]):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_basic_retrieval_excludes_module_gap_chunks(self):
        """get_code_chunks builds correct SQL query and executes it with filters."""
        conn, cursor = self._mock_conn([])

        get_code_chunks(conn, "o/r", prog_lang="python", path_prefix="src/")

        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]

        assert "SELECT path, heading_path, line, end_line, content, prog_lang" in sql
        assert "FROM chunks" in sql
        assert "WHERE repo = %s AND source_type = 'code' AND content NOT LIKE '[%%] (module)%%'" in sql
        assert "AND prog_lang = %s" in sql
        assert "AND path LIKE %s || '%'" in sql
        assert "ORDER BY path, line" in sql
        assert params == ["o/r", "python", "src/"]
