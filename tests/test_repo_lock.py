"""Advisory-lock lifecycle (issue #374): a skip leaves a durable trace, and a
release that did not actually release is distinguishable.

No PostgreSQL in the sandbox: the connection is mocked at the cursor boundary.
``pg_advisory_unlock``'s return value is the thing this issue is about, and a
mock returns whatever you tell it to -- so the tests are written so that a
mock returning False (did not hold the lock) produces a visibly different
outcome (warning log) from a mock returning True (debug only).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shiori import db, schema
from shiori.ingest import (
    SYNC_LOCK_KEY,
    _acquire_repo_lock,
    _release_repo_lock,
    repo_lock,
)


def _mock_conn():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn, cursor


# ===================================================================
# repo_lock context manager: the single place for acquire/skip/release
# ===================================================================


class TestRepoLockLifecycle:
    def test_skip_is_recorded_durably_and_release_not_called(self, caplog):
        """Acquire False -> skip recorded via record_sync_skip (not an
        attempt), logged, and no release is attempted (we never held it)."""
        conn, _ = _mock_conn()
        with (
            patch("shiori.ingest._acquire_repo_lock", return_value=False),
            patch("shiori.ingest._release_repo_lock") as mock_release,
            patch("shiori.ingest.db.record_sync_skip") as mock_skip,
            patch("shiori.ingest.db.record_sync_attempt") as mock_attempt,
            caplog.at_level("INFO", logger="shiori.ingest"),
        ):
            with repo_lock(conn, "owner/repo", phase="index") as held:
                assert held is False

        mock_skip.assert_called_once_with(conn, "owner/repo")
        mock_release.assert_not_called()
        mock_attempt.assert_not_called()  # a skip is not an attempt
        assert (
            "index owner/repo: skipped (sync already running for this repo)"
            in caplog.text
        )

    def test_phase_appears_in_skip_log(self, caplog):
        conn, _ = _mock_conn()
        with (
            patch("shiori.ingest._acquire_repo_lock", return_value=False),
            patch("shiori.ingest.db.record_sync_skip"),
            caplog.at_level("INFO", logger="shiori.ingest"),
        ):
            with repo_lock(conn, "owner/repo", phase="fetch") as held:
                assert held is False
        assert "fetch owner/repo: skipped" in caplog.text

    def test_held_lock_releases_on_exit_and_records_no_skip(self, caplog):
        conn, _ = _mock_conn()
        body_ran = False
        with (
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch("shiori.ingest._release_repo_lock", return_value=True) as mock_release,
            patch("shiori.ingest.db.record_sync_skip") as mock_skip,
            caplog.at_level("DEBUG", logger="shiori.ingest"),
        ):
            with repo_lock(conn, "owner/repo", phase="index") as held:
                assert held is True
                body_ran = True

        assert body_ran
        mock_release.assert_called_once_with(conn, "owner/repo")
        mock_skip.assert_not_called()
        assert "index owner/repo: advisory lock released" in caplog.text
        assert "skipped" not in caplog.text

    def test_release_false_logs_warning_and_is_distinct_from_released(self, caplog):
        """THE key distinction: pg_advisory_unlock returning False (lock lost
        mid-run) must produce a visibly different outcome from True."""
        conn, _ = _mock_conn()
        with (
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch("shiori.ingest._release_repo_lock", return_value=False),
            caplog.at_level("WARNING", logger="shiori.ingest"),
        ):
            with repo_lock(conn, "owner/repo", phase="sync"):
                pass

        assert any(
            r.levelno >= 30 and "advisory lock was NOT held at release time" in r.message
            for r in caplog.records
        )

    def test_release_false_warns_about_another_process(self, caplog):
        conn, _ = _mock_conn()
        with (
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch("shiori.ingest._release_repo_lock", return_value=False),
            caplog.at_level("WARNING", logger="shiori.ingest"),
        ):
            with repo_lock(conn, "owner/repo", phase="sync"):
                pass
        assert "another process could have entered this repo" in caplog.text

    def test_release_true_produces_no_warning(self, caplog):
        conn, _ = _mock_conn()
        with (
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch("shiori.ingest._release_repo_lock", return_value=True),
            caplog.at_level("WARNING", logger="shiori.ingest"),
        ):
            with repo_lock(conn, "owner/repo", phase="sync"):
                pass
        assert "NOT held" not in caplog.text

    def test_release_exception_logged_and_does_not_mask_body_outcome(self, caplog):
        conn, _ = _mock_conn()
        with (
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch(
                "shiori.ingest._release_repo_lock",
                side_effect=RuntimeError("connection lost"),
            ),
            caplog.at_level("ERROR", logger="shiori.ingest"),
        ):
            with repo_lock(conn, "owner/repo", phase="index"):
                pass  # body succeeded

        # The release failure is loud but does not turn a success into a failure.
        assert any(
            r.levelno >= 40 and "advisory-lock release statement failed" in r.message
            for r in caplog.records
        )

    def test_body_exception_still_releases_and_propagates(self):
        conn, _ = _mock_conn()
        with (
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch("shiori.ingest._release_repo_lock", return_value=True) as mock_release,
        ):
            with pytest.raises(RuntimeError, match="work exploded"):
                with repo_lock(conn, "owner/repo", phase="index"):
                    raise RuntimeError("work exploded")

        mock_release.assert_called_once_with(conn, "owner/repo")

    def test_body_exception_not_masked_by_release_error(self):
        """A release failure while the body already failed must not replace
        the body's exception (the old bare ``except: pass`` protected this;
        the new log-based release must preserve it)."""
        conn, _ = _mock_conn()
        with (
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch(
                "shiori.ingest._release_repo_lock",
                side_effect=RuntimeError("release exploded"),
            ),
        ):
            with pytest.raises(RuntimeError, match="work exploded"):
                with repo_lock(conn, "owner/repo", phase="index"):
                    raise RuntimeError("work exploded")

    def test_acquire_exception_propagates_and_records_nothing(self):
        """A raise from the acquire statement is a DB-level problem, not a
        skip: it propagates unchanged and records neither skip nor attempt."""
        conn, _ = _mock_conn()
        with (
            patch(
                "shiori.ingest._acquire_repo_lock",
                side_effect=RuntimeError("lock query failed"),
            ),
            patch("shiori.ingest.db.record_sync_skip") as mock_skip,
            patch("shiori.ingest.db.record_sync_attempt") as mock_attempt,
            patch("shiori.ingest._release_repo_lock") as mock_release,
        ):
            with pytest.raises(RuntimeError, match="lock query failed"):
                with repo_lock(conn, "owner/repo", phase="sync"):
                    pass  # pragma: no cover - never reached

        mock_skip.assert_not_called()
        mock_attempt.assert_not_called()
        mock_release.assert_not_called()


# ===================================================================
# _acquire_repo_lock / _release_repo_lock
# ===================================================================


class TestAcquireReleaseLock:
    def test_acquire_returns_row_value(self):
        conn, cursor = _mock_conn()
        cursor.fetchone.return_value = (True,)
        assert _acquire_repo_lock(conn, "owner/repo") is True

        cursor.fetchone.return_value = (False,)
        assert _acquire_repo_lock(conn, "owner/repo") is False

    def test_acquire_none_row_is_false(self):
        conn, cursor = _mock_conn()
        cursor.fetchone.return_value = None
        assert _acquire_repo_lock(conn, "owner/repo") is False

    def test_release_returns_true_when_held(self):
        conn, cursor = _mock_conn()
        cursor.fetchone.return_value = (True,)
        assert _release_repo_lock(conn, "owner/repo") is True

    def test_release_returns_false_when_not_held(self):
        """pg_advisory_unlock returning false = the session did not hold the
        lock; the return value is no longer discarded."""
        conn, cursor = _mock_conn()
        cursor.fetchone.return_value = (False,)
        assert _release_repo_lock(conn, "owner/repo") is False

    def test_release_exception_not_swallowed(self):
        """The release statement itself failing must raise -- no bare
        ``except: pass`` anymore."""
        conn, cursor = _mock_conn()
        cursor.execute.side_effect = RuntimeError("connection lost")
        with pytest.raises(RuntimeError, match="connection lost"):
            _release_repo_lock(conn, "owner/repo")

    def test_lock_key_is_pinned(self):
        # Changing the key would silently stop excluding against any
        # already-running process (the one corruption-mode failure).
        assert SYNC_LOCK_KEY == 0x5348494F


# ===================================================================
# record_sync_skip: durable, countable, and NOT an attempt
# ===================================================================


class TestRecordSyncSkip:
    def test_upserts_last_skipped_at_and_increments_skip_count(self):
        conn, cursor = _mock_conn()

        db.record_sync_skip(conn, "owner/repo")

        sql = cursor.execute.call_args[0][0]
        params = cursor.execute.call_args[0][1]
        assert "last_skipped_at" in sql
        assert "sync_runs.skip_count + 1" in sql
        assert params == ("owner/repo",)
        conn.commit.assert_called_once()

    def test_skip_does_not_touch_attempt_fields(self):
        """A skip must not set last_error, must not touch last_attempt_at and
        must not increment consecutive_failures (circuit breaker, #345)."""
        conn, cursor = _mock_conn()

        db.record_sync_skip(conn, "owner/repo")

        sql = cursor.execute.call_args[0][0]
        assert "consecutive_failures" not in sql
        assert "last_error" not in sql
        assert "last_attempt_at" not in sql
        assert "finished_at" not in sql

    def test_successful_attempt_resets_skip_count(self):
        conn, cursor = _mock_conn()

        db.record_sync_attempt(conn, "owner/repo", success=True)

        sql = cursor.execute.call_args[0][0]
        assert "skip_count = 0" in sql

    def test_failed_attempt_leaves_skip_count_alone(self):
        conn, cursor = _mock_conn()

        db.record_sync_attempt(conn, "owner/repo", success=False, error="boom")

        sql = cursor.execute.call_args[0][0]
        assert "skip_count" not in sql


# ===================================================================
# schema: the columns exist in DDL and migrate adds them to old DBs
# ===================================================================


class TestSkipTrackingSchema:
    def test_columns_in_create_table_ddl(self):
        assert "last_skipped_at TIMESTAMPTZ" in schema.SCHEMA_SQL
        assert "skip_count INTEGER NOT NULL DEFAULT 0" in schema.SCHEMA_SQL

    def test_migrate_adds_columns_when_missing(self):
        """On an old DB (columns absent), migrate_light's light migration adds
        them via the existing ALTER path."""
        conn, cursor = _mock_conn()
        # Catalog probe answers: every known column except the skip-tracking
        # pair (union across tables is fine -- the probes are keyed off the
        # column names, not the table).
        cursor.fetchall.return_value = [
            ("end_line", False),
            ("commit_sha", False),
            ("prog_lang", False),
            ("symbols", False),
            ("kind", False),
            ("code_indexed", False),
            ("last_attempt_at", False),
            ("last_error", False),
            ("consecutive_failures", False),
            ("finished_at", False),
            ("indexed_at", False),
            ("labels", False),
        ]

        schema._run_alter_statements(conn)

        executed = " ".join(str(c.args[0]) for c in cursor.execute.call_args_list)
        assert (
            "ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS "
            "last_skipped_at TIMESTAMPTZ" in executed
        )
        assert (
            "ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS skip_count "
            "INTEGER NOT NULL DEFAULT 0" in executed
        )


# ===================================================================
# call-site wiring: the helper is what the phases actually use
# ===================================================================


class TestCallSiteWiring:
    def _settings(self):
        s = MagicMock()
        s.repos = ["owner/repo"]
        s.dev_repos = set()
        s.ref_backfill_since = None
        s.fetch_concurrency = 4
        return s

    def test_run_index_skip_goes_through_record_sync_skip(self):
        """run_index's lock guard now records the skip via the helper on the
        loop's own connection (not the pre-flight one)."""
        from shiori.ingest import run_index

        pre_conn = MagicMock()
        idx_conn = MagicMock()
        settings = self._settings()

        with (
            patch("shiori.ingest.db.connect", side_effect=[pre_conn, idx_conn]),
            patch("shiori.ingest.schema.migrate"),
            patch("shiori.ingest.schema.migrate_light"),
            patch("shiori.ingest._is_bulk_path", return_value=False),
            patch("shiori.ingest._acquire_repo_lock", return_value=False),
            patch("shiori.ingest._release_repo_lock"),
            patch("shiori.ingest.db.record_sync_skip") as mock_skip,
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.index_docs"),
            patch("shiori.ingest.index_issues"),
            patch("shiori.ingest.index_code"),
        ):
            run_index(settings=settings)

        mock_skip.assert_called_once_with(idx_conn, "owner/repo")

    def test_run_fetch_skip_records_and_skips_fetch(self):
        """run_fetch's per-thread guard records the skip and still skips the
        actual fetch work."""
        from shiori.ingest import run_fetch

        mock_conn = MagicMock()
        settings = self._settings()

        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.schema.migrate_light"),
            patch("shiori.ingest._acquire_repo_lock", return_value=False),
            patch("shiori.ingest._release_repo_lock"),
            patch("shiori.ingest.db.record_sync_skip") as mock_skip,
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.fetch_docs") as mock_fetch_docs,
            patch("shiori.ingest.fetch_issues") as mock_fetch_issues,
        ):
            run_fetch(settings=settings)

        mock_skip.assert_called_once_with(mock_conn, "owner/repo")
        mock_fetch_docs.assert_not_called()
        mock_fetch_issues.assert_not_called()

    def test_forget_skip_is_recorded_before_system_exit(self):
        """ingest.py's SystemExit site (explicitly targeted repo) is still a
        'could not acquire' event and is recorded like the others."""
        from shiori.ingest import run_forget

        conn = MagicMock()
        settings = MagicMock()

        with (
            patch("shiori.ingest.db.connect", return_value=conn),
            patch("shiori.ingest._acquire_repo_lock", return_value=False),
            patch("shiori.ingest._release_repo_lock"),
            patch("shiori.ingest.db.record_sync_skip") as mock_skip,
        ):
            with pytest.raises(SystemExit, match="sync is running for owner/repo"):
                run_forget(repos=["owner/repo"], settings=settings)

        mock_skip.assert_called_once_with(conn, "owner/repo")
        # never held it -> no release attempted
        conn.cursor.return_value.__enter__.return_value.execute.assert_not_called()


class TestPipelineSentinel:
    """The pipeline records an *acquire* failure as a failed attempt (issue
    #196 behaviour, frozen by test_ingest_validation.py) but must not
    double-record a bulk-mode work failure that re-raises through the same
    wrapper."""

    def _mock_conn_cm(self, cursor_execute_side_effect=None):
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False
        mock_cursor = MagicMock()
        if cursor_execute_side_effect is not None:
            mock_cursor.execute.side_effect = cursor_execute_side_effect
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        return mock_conn

    def test_acquire_failure_recorded_once_as_attempt(self):
        from shiori.pipeline import _do_sync

        def explode_on_lock_query(query, *args, **kwargs):
            if "pg_try_advisory_lock" in query:
                raise RuntimeError("lock query failed")

        mock_conn = self._mock_conn_cm(
            cursor_execute_side_effect=explode_on_lock_query
        )

        with (
            patch("shiori.pipeline.settings") as mock_settings,
            patch("shiori.pipeline._sync_lock") as mock_lock,
            patch("shiori.pipeline.build_token_provider", return_value=MagicMock()),
            patch("shiori.pipeline._get_embedder", return_value=MagicMock()),
            patch("shiori.pipeline._conn", return_value=mock_conn),
            patch("shiori.pipeline._is_bulk_path", return_value=False),
            patch("shiori.pipeline.schema.migrate"),
            patch("shiori.pipeline.db.record_sync_attempt") as mock_record_attempt,
        ):
            mock_settings.repos = ["owner/repo"]
            mock_settings.fetch_concurrency = 4
            mock_lock.acquire.return_value = True

            with pytest.raises(RuntimeError, match="lock query failed"):
                _do_sync()

        mock_conn.rollback.assert_not_called()
        mock_record_attempt.assert_called_once_with(
            mock_conn, "owner/repo", success=False, error="lock query failed"
        )

    def test_bulk_work_failure_recorded_exactly_once(self):
        """In bulk mode a work failure records the attempt once (inner
        except), re-raises, and the outer acquire wrapper must not record it
        a second time (which would inflate consecutive_failures)."""
        from shiori.pipeline import _do_sync

        mock_conn = self._mock_conn_cm()

        with (
            patch("shiori.pipeline.settings") as mock_settings,
            patch("shiori.pipeline._sync_lock") as mock_lock,
            patch("shiori.pipeline.build_token_provider", return_value=MagicMock()),
            patch("shiori.pipeline._get_embedder", return_value=MagicMock()),
            patch("shiori.pipeline._conn", return_value=mock_conn),
            patch("shiori.pipeline._is_bulk_path", return_value=True),
            patch("shiori.pipeline.schema.migrate_light"),
            patch("shiori.pipeline.schema.truncate_all_repos"),
            patch("shiori.pipeline.schema.drop_heavy_indexes"),
            patch("shiori.pipeline.ChunkBuffer", return_value=MagicMock()),
            patch(
                "shiori.pipeline.sync_docs",
                side_effect=RuntimeError("embedding failed"),
            ),
            patch("shiori.pipeline.db.record_sync_attempt") as mock_record_attempt,
            patch("shiori.pipeline.db.record_repo_sync_error"),
        ):
            mock_settings.repos = ["owner/repo"]
            mock_settings.fetch_concurrency = 4
            mock_lock.acquire.return_value = True

            with pytest.raises(RuntimeError, match="embedding failed"):
                _do_sync()

        mock_record_attempt.assert_called_once_with(
            mock_conn, "owner/repo", success=False, error="embedding failed"
        )
