"""db モジュールのユニットテスト（issue #54, #72）。"""

from __future__ import annotations

from unittest.mock import MagicMock

from shiori.db import (
    bulk_insert_chunks,
    create_heavy_indexes,
    drop_heavy_indexes,
    get_pr_changes,
    get_pr_head_sha,
    get_sync_runs,
    migrate_light,
    record_sync_attempt,
    upsert_pr_changes,
)


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
        """ファイル一覧と head_sha + base_sha も含めて返す。"""
        conn, cursor = self._mock_conn(
            [
                ("src/a.py", "modified", 5, 2, 7, "url_a", "abc1234", "def4567"),
                ("src/b.py", "added", 10, 0, 10, "url_b", "abc1234", "def4567"),
            ],
        )
        files, head_sha, base_sha = get_pr_changes(conn, "o/r", 42)

        assert head_sha == "abc1234"
        assert base_sha == "def4567"
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
        """pr_changes に行がない場合、空リストと None×2 を返す。"""
        conn, cursor = self._mock_conn([])

        files, head_sha, base_sha = get_pr_changes(conn, "o/r", 42)

        assert files == []
        assert head_sha is None
        assert base_sha is None

    def test_excludes_sentinel_rows(self):
        """path が空文字の sentinel 行は files に含まれず、head_sha と base_sha は取得される。"""
        conn, cursor = self._mock_conn(
            [
                ("", None, None, None, None, None, "abc1234", "def4567"),  # sentinel
            ],
        )
        files, head_sha, base_sha = get_pr_changes(conn, "o/r", 42)

        assert files == []
        assert head_sha == "abc1234"
        assert base_sha == "def4567"

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

        upsert_pr_changes(conn, "o/r", 42, "abc1234", "def4567", files)

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

        upsert_pr_changes(conn, "o/r", 42, "abc1234", "def4567", [])

        # DELETE + sentinel INSERT の 2 回
        assert cursor.execute.call_count == 2

        # sentinel INSERT のパラメータを検証
        # path='' は SQL に直書きのため、パラメータは (repo, issue_no, head_sha, base_sha) の 4 要素
        sentinel_params = cursor.execute.call_args_list[1][0][1]
        assert len(sentinel_params) == 4
        assert sentinel_params[0] == "o/r"       # repo
        assert sentinel_params[1] == 42           # issue_no
        assert sentinel_params[2] == "abc1234"    # head_sha
        assert sentinel_params[3] == "def4567"    # base_sha

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

        upsert_pr_changes(conn, "o/r", 42, "def5678", "base9999", files)

        insert_params = cursor.execute.call_args_list[1][0][1]
        assert insert_params[4] == "deleted.py"  # path (after repo, issue_no, head_sha, base_sha)
        assert insert_params[5] == "removed"     # status
        assert insert_params[9] is None          # blob_url


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
