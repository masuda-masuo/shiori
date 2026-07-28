"""Catalog-guarded migrate DDL + lock_timeout scoping (issue #362).

Production incident: a multi-hour ``CREATE INDEX ... USING hnsw`` held a
SHARE lock on ``chunks`` while every migrate run unconditionally queued an
ACCESS EXCLUSIVE ALTER behind it. Postgres lock queues are FIFO, so that one
waiting request blocked every later request -- including plain reads --
taking the server offline for the duration.

These tests use a small fake cursor/connection instead of MagicMock so the
catalog-probe responses are keyed off the query just issued (matching on
"pg_attribute" / "pg_constraint" / "to_regclass" in the SQL text) rather than
call order -- the probe functions can be reordered without breaking tests.
tests/test_db.py already has a TestMigrateLight class covering migrate_light
against an unconfigured MagicMock cursor (SCHEMA_SQL executes, some ALTER
runs); it is left untouched. This file covers the new catalog-guard and
lock_timeout behavior.
"""

from __future__ import annotations

import psycopg
import pytest

from shiori import schema
from shiori.config import Settings
from shiori.schema import create_heavy_indexes, drop_heavy_indexes, migrate_light


class _FakeCursor:
    """Answers catalog probes based on the query just issued.

    ``columns``: {table: {col_name: attnotnull}} -- backs the pg_attribute
    probe (_existing_columns). A column absent from the dict is "missing".

    ``constraint_defs``: {conname: normalized def} -- backs the
    pg_constraint probe (_existing_constraint_defs). A name absent from the
    dict means the constraint doesn't currently exist.

    ``repo_index_state_exists``: backs the to_regclass('repo_index_state')
    probe.

    ``raise_on_execute_containing``: if set, cur.execute() raises
    ``error_to_raise`` the first time the executed SQL contains this
    substring (simulates a lock_timeout firing on a specific statement).
    """

    def __init__(
        self,
        columns: dict[str, dict[str, bool]] | None = None,
        constraint_defs: dict[str, str] | None = None,
        repo_index_state_exists: bool = True,
        raise_on_execute_containing: str | None = None,
        error_to_raise: Exception | None = None,
    ) -> None:
        self.columns = columns or {}
        self.constraint_defs = constraint_defs or {}
        self.repo_index_state_exists = repo_index_state_exists
        self.raise_on_execute_containing = raise_on_execute_containing
        self.error_to_raise = error_to_raise
        self.executed: list[str] = []
        self._last_sql = ""
        self._last_params: tuple | None = None

    def execute(self, sql_text, params=None) -> None:
        text = str(sql_text)
        if self.raise_on_execute_containing and self.raise_on_execute_containing in text:
            # Only fires once, like a real lock_timeout on one statement.
            self.raise_on_execute_containing = None
            raise self.error_to_raise
        self.executed.append(text)
        self._last_sql = text
        self._last_params = params

    def fetchall(self):
        text = self._last_sql
        if "pg_attribute" in text:
            table = self._last_params[0]
            return list(self.columns.get(table, {}).items())
        if "pg_constraint" in text:
            names = self._last_params[1]
            return [(n, self.constraint_defs[n]) for n in names if n in self.constraint_defs]
        raise AssertionError(f"unexpected fetchall() after: {text!r}")

    def fetchone(self):
        text = self._last_sql
        if "to_regclass" in text:
            return ("repo_index_state",) if self.repo_index_state_exists else (None,)
        raise AssertionError(f"unexpected fetchone() after: {text!r}")

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.commits = 0

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1


# schema.py deliberately keeps its expected-def strings as function-local
# constants next to their ADD CONSTRAINT SQL (not module-level), so they
# aren't importable here. These two literals must match schema.py's
# `source_type_check_expected_def` / `kind_check_expected_def` -- they're
# only used below to build a cursor that reports "already matches", so a
# mismatch would just make TestCatalogGuardedZeroDrift see (harmless) drift,
# not a false pass.
_SOURCE_TYPE_CHECK_EXPECTED_DEF = (
    "CHECK ((source_type = ANY (ARRAY['doc'::text, 'issue'::text, "
    "'pr_review'::text, 'code'::text])))"
)
_KIND_CHECK_EXPECTED_DEF = "CHECK (((kind = ANY (ARRAY['issue'::text, 'pr'::text])) OR (kind IS NULL)))"


def _up_to_date_columns() -> dict[str, dict[str, bool]]:
    return {
        "chunks": {
            "end_line": False,
            "commit_sha": False,
            "prog_lang": False,
            "symbols": False,
            "kind": False,
        },
        "doc_files": {"kind": False},
        "sync_runs": {
            "code_indexed": False,
            "last_attempt_at": False,
            "last_error": False,
            "consecutive_failures": False,
            "finished_at": False,
        },
        "issue_items": {"indexed_at": False, "labels": False},
    }


def _up_to_date_constraint_defs() -> dict[str, str]:
    return {
        "chunks_source_type_check": _SOURCE_TYPE_CHECK_EXPECTED_DEF,
        "chunks_kind_check": _KIND_CHECK_EXPECTED_DEF,
    }


class TestCatalogGuardedZeroDrift:
    """On an already-migrated DB, migrate_light must not touch anything."""

    def test_up_to_date_schema_issues_zero_alter_statements(self, caplog):
        cursor = _FakeCursor(
            columns=_up_to_date_columns(),
            constraint_defs=_up_to_date_constraint_defs(),
            repo_index_state_exists=True,
        )
        conn = _FakeConn(cursor)

        with caplog.at_level("INFO", logger="shiori.schema"):
            migrate_light(conn, Settings())

        alter_calls = [s for s in cursor.executed if s.strip().upper().startswith("ALTER TABLE")]
        create_table_calls = [s for s in cursor.executed if "CREATE TABLE repo_index_state" in s]
        assert alter_calls == []
        assert create_table_calls == []
        assert "schema up to date (no DDL executed)" in caplog.text


class TestCatalogGuardedDriftDetected:
    """Each drift kind still triggers exactly the DDL it needs -- and only that."""

    def test_missing_column_runs_only_that_alter(self):
        columns = _up_to_date_columns()
        del columns["chunks"]["symbols"]  # drift: symbols column missing
        cursor = _FakeCursor(
            columns=columns,
            constraint_defs=_up_to_date_constraint_defs(),
            repo_index_state_exists=True,
        )
        conn = _FakeConn(cursor)

        schema._run_alter_statements(conn)

        alter_calls = [s for s in cursor.executed if s.strip().upper().startswith("ALTER TABLE")]
        assert len(alter_calls) == 1
        assert "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS symbols TEXT" in alter_calls[0]

    def test_missing_constraint_runs_add(self):
        constraint_defs = _up_to_date_constraint_defs()
        del constraint_defs["chunks_source_type_check"]  # drift: constraint absent
        cursor = _FakeCursor(
            columns=_up_to_date_columns(),
            constraint_defs=constraint_defs,
            repo_index_state_exists=True,
        )
        conn = _FakeConn(cursor)

        schema._run_alter_statements(conn)

        joined = " ".join(cursor.executed)
        assert "DROP CONSTRAINT IF EXISTS chunks_source_type_check" in joined
        assert "ADD CONSTRAINT chunks_source_type_check" in joined
        # kind_check was untouched and matched -- no drop/add for it
        assert "DROP CONSTRAINT IF EXISTS chunks_kind_check" not in joined

    def test_stale_constraint_def_runs_drop_and_add(self):
        constraint_defs = _up_to_date_constraint_defs()
        # Simulates an old cluster whose constraint predates the 'code' value.
        constraint_defs["chunks_source_type_check"] = (
            "CHECK ((source_type = ANY (ARRAY['doc'::text, 'issue'::text, "
            "'pr_review'::text])))"
        )
        cursor = _FakeCursor(
            columns=_up_to_date_columns(),
            constraint_defs=constraint_defs,
            repo_index_state_exists=True,
        )
        conn = _FakeConn(cursor)

        schema._run_alter_statements(conn)

        joined = " ".join(cursor.executed)
        assert "DROP CONSTRAINT IF EXISTS chunks_source_type_check" in joined
        assert "ADD CONSTRAINT chunks_source_type_check" in joined

    def test_stale_finished_at_not_null_runs_drop_not_null(self):
        columns = _up_to_date_columns()
        columns["sync_runs"]["finished_at"] = True  # drift: still NOT NULL
        cursor = _FakeCursor(
            columns=columns,
            constraint_defs=_up_to_date_constraint_defs(),
            repo_index_state_exists=True,
        )
        conn = _FakeConn(cursor)

        schema._run_alter_statements(conn)

        joined = " ".join(cursor.executed)
        assert "ALTER TABLE sync_runs ALTER COLUMN finished_at DROP NOT NULL" in joined
        # the already-present attempt-tracking columns are not re-added
        assert "last_attempt_at" not in joined

    def test_missing_repo_index_state_table_still_created(self):
        cursor = _FakeCursor(
            columns=_up_to_date_columns(),
            constraint_defs=_up_to_date_constraint_defs(),
            repo_index_state_exists=False,
        )
        conn = _FakeConn(cursor)

        schema._run_alter_statements(conn)

        joined = " ".join(cursor.executed)
        assert "CREATE TABLE" in joined
        assert "repo_index_state" in joined


class TestLockTimeoutScoping:
    """lock_timeout must bound migrate's DDL and never leak into
    create_heavy_indexes, which runs later on the same connection."""

    def test_set_before_ddl_and_reset_before_create_heavy_indexes(self):
        cursor = _FakeCursor(
            columns=_up_to_date_columns(),
            constraint_defs=_up_to_date_constraint_defs(),
            repo_index_state_exists=True,
        )
        conn = _FakeConn(cursor)

        migrate_light(conn, Settings())
        migrate_light_call_count = len(cursor.executed)
        create_heavy_indexes(conn, Settings())

        # NB: "RESET lock_timeout" contains "SET lock_timeout" as a raw
        # substring (RE + SET overlap) -- exclude RESET explicitly rather
        # than relying on a naive "in" check for the SET statement.
        set_indices = [
            i for i, s in enumerate(cursor.executed) if "lock_timeout" in s and "RESET" not in s
        ]
        reset_indices = [i for i, s in enumerate(cursor.executed) if s == "RESET lock_timeout"]
        assert len(set_indices) == 1
        assert len(reset_indices) == 1
        # SET happens before any migrate DDL, RESET happens before
        # create_heavy_indexes's statements begin.
        assert set_indices[0] == 0
        assert reset_indices[0] < migrate_light_call_count
        heavy_index_calls = cursor.executed[migrate_light_call_count:]
        assert heavy_index_calls, "create_heavy_indexes should have executed something"
        assert not any("lock_timeout" in s for s in heavy_index_calls)

    def test_lock_timeout_error_propagates_unmodified(self):
        class _FakeLockTimeout(psycopg.errors.QueryCanceled):
            pass

        cursor = _FakeCursor(
            columns=_up_to_date_columns(),
            constraint_defs=_up_to_date_constraint_defs(),
            repo_index_state_exists=True,
            raise_on_execute_containing="SET lock_timeout",
            error_to_raise=_FakeLockTimeout("canceling statement due to lock timeout"),
        )
        conn = _FakeConn(cursor)

        with pytest.raises(_FakeLockTimeout, match="lock timeout"):
            migrate_light(conn, Settings())

        # No RESET should have run -- the failure happened before any DDL,
        # and nothing should attempt cleanup on the failure path.
        assert not any("RESET lock_timeout" in s for s in cursor.executed)

    def test_lock_timeout_error_on_alter_propagates_unmodified(self):
        columns = _up_to_date_columns()
        del columns["chunks"]["symbols"]  # force a real ALTER to run
        cursor = _FakeCursor(
            columns=columns,
            constraint_defs=_up_to_date_constraint_defs(),
            repo_index_state_exists=True,
            raise_on_execute_containing="ADD COLUMN IF NOT EXISTS symbols",
            error_to_raise=psycopg.errors.QueryCanceled(
                "canceling statement due to lock timeout"
            ),
        )
        conn = _FakeConn(cursor)

        with pytest.raises(psycopg.errors.QueryCanceled, match="lock timeout"):
            migrate_light(conn, Settings())

        # The RESET at the end of migrate_light must not have run -- the
        # exception propagated straight out instead of being swallowed.
        assert not any("RESET lock_timeout" in s for s in cursor.executed)


class TestDropHeavyIndexesLockTimeout:
    """drop_heavy_indexes gets the same lock_timeout bound as migrate's DDL
    (issue #362), extended to this call by issue #364: DROP INDEX takes an
    ACCESS EXCLUSIVE lock, and the #364 incident was exactly a DROP queuing
    (FIFO) behind a multi-hour CREATE INDEX and then blocking every reader.
    """

    def test_sets_lock_timeout_before_drops_and_resets_after(self):
        cursor = _FakeCursor()
        conn = _FakeConn(cursor)

        drop_heavy_indexes(conn)

        set_indices = [
            i for i, s in enumerate(cursor.executed) if "lock_timeout" in s and "RESET" not in s
        ]
        reset_indices = [i for i, s in enumerate(cursor.executed) if s == "RESET lock_timeout"]
        assert len(set_indices) == 1
        assert len(reset_indices) == 1
        assert set_indices[0] == 0
        assert reset_indices[0] == len(cursor.executed) - 1
        drop_calls = [s for s in cursor.executed if "DROP INDEX IF EXISTS" in s]
        assert len(drop_calls) == 3
        # SET precedes every DROP; RESET follows every DROP.
        drop_indices = [i for i, s in enumerate(cursor.executed) if "DROP INDEX IF EXISTS" in s]
        assert set_indices[0] < min(drop_indices)
        assert reset_indices[0] > max(drop_indices)

    def test_lock_timeout_error_propagates_and_skips_reset(self):
        class _FakeLockTimeout(psycopg.errors.QueryCanceled):
            pass

        cursor = _FakeCursor(
            raise_on_execute_containing="DROP INDEX IF EXISTS",
            error_to_raise=_FakeLockTimeout("canceling statement due to lock timeout"),
        )
        conn = _FakeConn(cursor)

        with pytest.raises(_FakeLockTimeout, match="lock timeout"):
            drop_heavy_indexes(conn)

        # No RESET should have run -- the failure propagates unmodified,
        # no retry, no swallow (this repo prefers crash-early).
        assert not any("RESET lock_timeout" in s for s in cursor.executed)
