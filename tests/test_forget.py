"""forget（repo 単位の索引削除）と rebuild の TRUNCATE 網羅のテスト。"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

from shiori.db import (
    REPO_SCOPED_TABLES,
    SCHEMA_SQL,
    forget_repo,
    truncate_all_repos,
)


def _sql_of(call) -> str:
    """execute() に渡された psycopg.sql.Composable を素の SQL 文字列にする。"""
    query = call.args[0]
    return query if isinstance(query, str) else query.as_string(None)


def _mock_conn(rowcount: int = 0, table_exists: bool = True):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.rowcount = rowcount
    # to_regclass の戻り。存在しないテーブルは None
    cursor.fetchone.return_value = ("chunks",) if table_exists else (None,)
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn, cursor


# ── スキーマとの整合 ──


class TestRepoScopedTablesCoversSchema:
    """REPO_SCOPED_TABLES がスキーマ上の repo 列を持つテーブルを取りこぼさないこと。

    これが元のバグの本体だった: rebuild の TRUNCATE は 6 テーブル中 4 つしか
    消しておらず、pr_changes と sync_runs に前の索引の行が残っていた。
    テーブルを増やしたときに気付けるよう、スキーマ側から逆に検算する。
    """

    def _tables_with_repo_column(self) -> set[str]:
        found = set()
        for m in re.finditer(
            r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", SCHEMA_SQL, re.S
        ):
            name, body = m.group(1), m.group(2)
            if re.search(r"^\s*repo\s+TEXT", body, re.M):
                found.add(name)
        return found

    def test_schema_parse_finds_the_known_tables(self):
        """先に検算器自体を疑う（正規表現が空振りしていたら以降のテストは無意味）。"""
        assert self._tables_with_repo_column() == {
            "chunks",
            "doc_files",
            "issue_items",
            "repo_index_state",
            "sync_state",
            "sync_runs",
        }

    def test_no_repo_scoped_table_is_missing(self):
        assert self._tables_with_repo_column() == set(REPO_SCOPED_TABLES)


# ── forget_repo ──


class TestForgetRepo:
    def test_deletes_from_every_repo_scoped_table(self):
        conn, cursor = _mock_conn(rowcount=3)

        deleted = forget_repo(conn, "owner/gone")

        deletes = [
            _sql_of(c) for c in cursor.execute.call_args_list if "DELETE" in _sql_of(c)
        ]
        assert len(deletes) == len(REPO_SCOPED_TABLES)
        for table in REPO_SCOPED_TABLES:
            assert f'DELETE FROM "{table}" WHERE repo = %s' in deletes
        assert deleted == {t: 3 for t in REPO_SCOPED_TABLES}

    def test_scopes_the_delete_to_the_given_repo(self):
        """他の repo を巻き添えにしない（rebuild と違う点そのもの）。"""
        conn, cursor = _mock_conn(rowcount=1)

        forget_repo(conn, "owner/gone")

        for call in cursor.execute.call_args_list:
            if "DELETE" in _sql_of(call):
                assert call.args[1] == ("owner/gone",)

    def test_missing_table_counts_as_zero(self):
        """索引がまだ無い DB では、消すものが無いだけでエラーではない。"""
        conn, cursor = _mock_conn(table_exists=False)

        deleted = forget_repo(conn, "owner/gone")

        assert deleted == {t: 0 for t in REPO_SCOPED_TABLES}
        assert not [c for c in cursor.execute.call_args_list if "DELETE" in _sql_of(c)]


# ── truncate_all_repos (rebuild) ──


class TestTruncateAllRepos:
    def test_truncates_every_repo_scoped_table(self):
        conn, cursor = _mock_conn()

        truncate_all_repos(conn)

        stmt = _sql_of(cursor.execute.call_args_list[0])
        assert stmt.startswith("TRUNCATE ")
        for table in REPO_SCOPED_TABLES:
            assert f'"{table}"' in stmt
