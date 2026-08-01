"""Issue #377: index-run time budget + per-repo progress/liveness visibility.

Covers the four pieces of the feature:

1. ``IndexBudget`` (config.py) -- working-time budget on
   ``time.monotonic()``, injectable clock, unbounded default.
2. ``SHIORI_INGEST_TIME_BUDGET`` parsing -- unset/empty/junk/non-positive
   must mean unbounded, never crash.
3. db.py -- ``PENDING_ISSUE_ITEMS_WHERE`` (the ONE definition of pending,
   MUST), the remaining-work counters built on it, ``record_sync_progress``
   (writes NO finished_at), ``touch_sync_progress`` (liveness heartbeat),
   and the readers surfacing ``pending_count`` / ``last_progress_at``.
4. Behavior -- ``index_issues`` stops at a batch boundary (durability
   invariant), ``run_index``/``run_ingest`` record progress without
   ``finished_at`` for a truncated repo and keep it out of the completed
   set that feeds the bulk heavy-index gate, and still exit 0.

Mock style follows tests/test_repo_lock.py and tests/test_github_sync.py:
cursor-boundary mocks, asserting on the SQL issued and on which fields are
written, never on mock echo.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from shiori.config import (
    IndexBudget,
    Settings,
    _ingest_time_budget_from_env,
)
from shiori.db import (
    PENDING_ISSUE_ITEMS_WHERE,
    count_pending_issue_items,
    count_pending_issue_items_for_repos,
    get_sync_run,
    get_sync_runs,
    record_sync_progress,
    record_sync_run,
    touch_sync_progress,
)
from shiori.sync_issues import BATCH_INDEX_SIZE, index_issues

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ===================================================================
# IndexBudget (config.py)
# ===================================================================


class TestIndexBudget:
    def test_unbounded_default_never_exhausts(self):
        for value in (None, 0, -5, "junk"):
            b = IndexBudget(value, monotonic=MagicMock())
            assert not b.exhausted()
            assert not b.exhausted()

    def test_bounded_exhausts_after_budget_seconds(self):
        clock = iter([100.0, 100.0, 199.9, 200.0, 201.0])

        def monotonic():
            return next(clock)

        b = IndexBudget(100.0, monotonic=monotonic)
        # construction consumed t=100; deadline = 200
        assert not b.exhausted()  # t=100 < 200
        assert not b.exhausted()  # t=199.9 < 200
        assert b.exhausted()      # t=200 >= 200
        assert b.exhausted()      # monotone: once True, stays True

    def test_exposed_budget_seconds(self):
        b = IndexBudget(42.0, monotonic=lambda: 0.0)
        assert b.budget_seconds == 42.0
        assert IndexBudget(None, monotonic=lambda: 0.0).budget_seconds is None


# ===================================================================
# SHIORI_INGEST_TIME_BUDGET parsing (config.py)
# ===================================================================


class TestIngestTimeBudgetEnv:
    def test_unset_means_unbounded(self, monkeypatch):
        monkeypatch.delenv("SHIORI_INGEST_TIME_BUDGET", raising=False)
        assert _ingest_time_budget_from_env() is None

    def test_empty_string_means_unbounded(self, monkeypatch):
        # The ${VAR:-} compose expansion delivers "" when unset -- this is
        # the exact trap that must not crash Settings construction.
        monkeypatch.setenv("SHIORI_INGEST_TIME_BUDGET", "")
        assert _ingest_time_budget_from_env() is None

    def test_whitespace_only_means_unbounded(self, monkeypatch):
        monkeypatch.setenv("SHIORI_INGEST_TIME_BUDGET", "   ")
        assert _ingest_time_budget_from_env() is None

    def test_junk_means_unbounded(self, monkeypatch):
        monkeypatch.setenv("SHIORI_INGEST_TIME_BUDGET", "lots")
        assert _ingest_time_budget_from_env() is None

    def test_non_positive_means_unbounded(self, monkeypatch):
        for value in ("0", "-1", "0.0"):
            monkeypatch.setenv("SHIORI_INGEST_TIME_BUDGET", value)
            assert _ingest_time_budget_from_env() is None

    def test_positive_parses_to_float(self, monkeypatch):
        monkeypatch.setenv("SHIORI_INGEST_TIME_BUDGET", "3600")
        assert _ingest_time_budget_from_env() == 3600.0
        monkeypatch.setenv("SHIORI_INGEST_TIME_BUDGET", "0.5")
        assert _ingest_time_budget_from_env() == 0.5

    def test_settings_field_defaults_to_unbounded(self, monkeypatch):
        monkeypatch.delenv("SHIORI_INGEST_TIME_BUDGET", raising=False)
        s = Settings()
        assert s.ingest_time_budget is None


# ===================================================================
# db.py: shared pending predicate + counters + progress records
# ===================================================================


class TestPendingDefinitionIsShared:
    def test_pending_constant_used_by_selection_and_counting(self):
        """The row-selection predicate in index_issues and the remaining-work
        counters are literally the same constant (MUST: one definition of
        "pending" -- two hand-copied predicates is the shipped-bug class this
        refactor eliminates)."""
        # index_issues embeds the constant in its SELECT
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value

        def _execute(sql, params=None):
            if "SELECT issue_no, comment_id, kind" in sql:
                cursor.fetchall.return_value = []

        cursor.execute.side_effect = _execute
        index_issues(Settings(), conn, MagicMock(), "o/r")
        select_sql = [c.args[0] for c in cursor.execute.call_args_list
                      if "SELECT issue_no, comment_id, kind" in c.args[0]]
        assert select_sql, "index_issues must issue its pending SELECT"
        assert PENDING_ISSUE_ITEMS_WHERE in select_sql[0]

        # count_pending_issue_items embeds the same constant
        cursor2 = MagicMock()
        conn2 = MagicMock()
        conn2.cursor.return_value.__enter__.return_value = cursor2
        cursor2.fetchone.return_value = (3,)
        assert count_pending_issue_items(conn2, "o/r") == 3
        count_sql = cursor2.execute.call_args.args[0]
        assert PENDING_ISSUE_ITEMS_WHERE in count_sql

        # ... and so does the cross-repo summary counter
        cursor3 = MagicMock()
        conn3 = MagicMock()
        conn3.cursor.return_value.__enter__.return_value = cursor3
        cursor3.fetchone.return_value = (7,)
        assert count_pending_issue_items_for_repos(conn3, ["o/a", "o/b"]) == 7
        multi_sql = cursor3.execute.call_args.args[0]
        assert PENDING_ISSUE_ITEMS_WHERE in multi_sql
        assert "repo = ANY(%s)" in multi_sql


class TestCountPending:
    def test_single_repo_count(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (42,)
        assert count_pending_issue_items(conn, "o/r") == 42
        sql, params = cursor.execute.call_args.args
        assert "WHERE repo = %s" in sql
        assert params == ("o/r",)

    def test_no_row_means_zero(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        assert count_pending_issue_items(conn, "o/r") == 0

    def test_multi_repo_passes_list(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (5,)
        assert count_pending_issue_items_for_repos(conn, ["o/a", "o/b"]) == 5
        _sql, params = cursor.execute.call_args.args
        assert params == (["o/a", "o/b"],)


class TestRecordSyncProgress:
    def test_writes_pending_count_and_liveness_but_no_finished_at(self):
        """record_sync_progress must NEVER write finished_at (MUST) -- it
        only records route, pending_count, and the last_progress_at
        heartbeat.  It must also not touch attempt/skip tracking."""
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value

        record_sync_progress(conn, "o/r", "cli", pending_count=137)

        sql = cursor.execute.call_args.args[0]
        params = cursor.execute.call_args.args[1]
        assert "pending_count" in sql
        assert "last_progress_at" in sql
        assert "finished_at" not in sql
        assert "last_attempt_at" not in sql
        assert "last_error" not in sql
        assert params == ("o/r", "cli", 137)
        conn.commit.assert_called_once()

    def test_upsert_refreshes_route(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        record_sync_progress(conn, "o/r", "mcp", pending_count=1)
        sql = cursor.execute.call_args.args[0]
        assert "ON CONFLICT (repo) DO UPDATE SET" in sql
        assert "route = EXCLUDED.route" in sql


class TestRecordSyncRunPendingCount:
    def test_completed_run_records_pending_count_zero(self):
        """record_sync_run is the *completed* path: it writes finished_at
        and the default pending_count of 0 (fully indexed)."""
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (_NOW,)

        finished = record_sync_run(conn, "o/r", "cli", 1, 2, 3)

        sql = cursor.execute.call_args.args[0]
        assert "finished_at" in sql
        assert "pending_count" in sql
        assert finished == _NOW
        conn.commit.assert_called_once()


class TestTouchSyncProgress:
    def test_heartbeat_advances_last_progress_at_only(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value

        touch_sync_progress(conn, "o/r")

        sql = cursor.execute.call_args.args[0]
        params = cursor.execute.call_args.args[1]
        assert "last_progress_at" in sql
        assert "finished_at" not in sql
        assert "pending_count" not in sql
        assert params == ("o/r",)
        conn.commit.assert_called_once()


class TestReadersSurfaceProgress:
    def _row(self, **overrides):
        base = {
            "repo": "o/r",
            "route": "cli",
            "finished_at": None,
            "age": None,
            "docs": 5,
            "issues": 6,
            "code": 7,
            "attempt_at": None,
            "error": None,
            "failures": 0,
            "pending_count": None,
            "last_progress_at": None,
        }
        base.update(overrides)
        return (
            base["repo"], base["route"], base["finished_at"], base["age"],
            base["docs"], base["issues"], base["code"], base["attempt_at"],
            base["error"], base["failures"], base["pending_count"],
            base["last_progress_at"],
        )

    def test_get_sync_runs_includes_pending_count_and_liveness(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [
            self._row(pending_count=137, last_progress_at=_NOW),
            self._row(repo="o/a", pending_count=0),
        ]

        runs = get_sync_runs(conn)

        assert runs["o/r"]["pending_count"] == 137
        assert runs["o/r"]["last_progress_at"] == _NOW.isoformat()
        assert runs["o/a"]["pending_count"] == 0
        assert runs["o/a"]["last_progress_at"] is None

    def test_get_sync_run_includes_pending_count(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        # get_sync_run SELECTs 11 columns (no repo): route, finished_at,
        # age, docs, issues, code, attempt_at, error, failures,
        # pending_count, last_progress_at
        cursor.fetchone.return_value = (
            "cli", None, None, 5, 6, 7, None, None, 0, 137, None
        )

        info = get_sync_run(conn, "o/r")

        assert info["pending_count"] == 137
        assert info["last_progress_at"] is None

    def test_short_rows_survive_the_row_guard(self):
        """_row_col must keep old-shape readers working with rows that lack
        the new trailing columns (they come from a pre-#377 schema)."""
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        # only 10 columns: no pending_count / last_progress_at
        cursor.fetchall.return_value = [
            ("o/r", "cli", None, None, 5, 6, 7, None, None, 0)
        ]

        runs = get_sync_runs(conn)

        assert runs["o/r"]["pending_count"] is None
        assert runs["o/r"]["last_progress_at"] is None


# ===================================================================
# index_issues: budget stop at a batch boundary (durability invariant)
# ===================================================================


class TestIndexIssuesBudget:
    def _item_row(self, issue_no, kind="issue", body="body"):
        return (issue_no, 0, kind, "T", "alice", False, "open", None,
                None, body, "url", None, None)

    def _make_conn(self, rows, track_updates=False):
        """cursor whose fetchall returns *rows* for the pending SELECT and
        [] for everything else; optionally records indexed_at UPDATEs."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        updates: list[tuple[int, int]] = []

        def _execute(sql, params=None):
            if "SELECT issue_no, comment_id, kind" in (sql or ""):
                cursor.fetchall.return_value = rows
            elif "SELECT issue_no, kind, state FROM issue_items" in (sql or ""):
                cursor.fetchall.return_value = []
            elif "UPDATE issue_items SET indexed_at" in (sql or ""):
                updates.append((params[1], params[2]))
            else:
                cursor.fetchall.return_value = []

        cursor.execute.side_effect = _execute
        conn._indexed_updates = updates
        return conn

    def test_exhausted_before_any_batch_indexes_nothing(self, caplog):
        settings = Settings()
        embedder = MagicMock()
        rows = [self._item_row(i) for i in range(1, 5)]
        conn = self._make_conn(rows)

        class _Exhausted:
            budget_seconds = 1.0
            def exhausted(self):
                return True

        with caplog.at_level(logging.INFO, logger="shiori.sync_issues"):
            n = index_issues(settings, conn, embedder, "o/r", budget=_Exhausted())

        assert n == 0
        embedder.embed_passages.assert_not_called()
        assert conn._indexed_updates == []
        messages = [r.getMessage() for r in caplog.records]
        assert any("stopped_by_budget=1" in m for m in messages)

    def test_exhausted_between_batches_commits_prior_batch_only(self, caplog):
        """Budget consumed while batch 1 (200 items) is mid-flight: batch 1
        must be fully committed (chunks + indexed_at + heartbeat) and batch
        2 must be left untouched -- that is the safe boundary by
        construction (durability invariant)."""
        settings = Settings()
        embedder = MagicMock()
        embedder.embed_passages.return_value = [[0.1, 0.2]]
        n_items = BATCH_INDEX_SIZE + 50  # two batches
        rows = [self._item_row(i) for i in range(1, n_items + 1)]
        conn = self._make_conn(rows, track_updates=True)

        class _FakeBudget:
            budget_seconds = 1.0
            def __init__(self):
                self._calls = 0
            def exhausted(self):
                self._calls += 1
                # first check (batch 1): not yet; second (batch 2): yes
                return self._calls > 1

        budget = _FakeBudget()
        with caplog.at_level(logging.INFO, logger="shiori.sync_issues"):
            n = index_issues(settings, conn, embedder, "o/r", budget=budget)

        # Only batch 1 was indexed...
        assert n == BATCH_INDEX_SIZE
        assert len(conn._indexed_updates) == BATCH_INDEX_SIZE
        assert conn._indexed_updates[-1] == (BATCH_INDEX_SIZE, 0)
        # ...and nothing from batch 2
        assert all(i <= BATCH_INDEX_SIZE for i, _ in conn._indexed_updates)

        # The durability tail ran: chunks commit + indexed_at commit +
        # heartbeat commit all happened (>= 3 commits, never a 4th for batch 2)
        assert conn.commit.call_count == 3

        # Stop is logged with the remaining count
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "stopped_by_budget=1" in m and "remaining=50" in m
            for m in messages
        )

    def test_no_budget_is_pre_377_behaviour(self):
        settings = Settings()
        embedder = MagicMock()
        embedder.embed_passages.return_value = [[0.1, 0.2]]
        rows = [self._item_row(i) for i in range(1, BATCH_INDEX_SIZE + 10)]
        conn = self._make_conn(rows, track_updates=True)

        n = index_issues(settings, conn, embedder, "o/r")

        assert n == BATCH_INDEX_SIZE + 9  # range(1, N+10) is N+9 items
        assert len(conn._indexed_updates) == BATCH_INDEX_SIZE + 9


# ===================================================================
# run_index / run_ingest: truncated repo -> progress, no finished_at,
# out of the completed set, still exit 0
# ===================================================================


class TestRunIndexBudgetCompletion:
    """run_index completion is decided from a post-pass pending count
    (MUST: finished_at only when fully indexed; truncated repo never in the
    completed set feeding the bulk heavy-index gate; exit 0 on truncation).
    """

    def _run(self, pending_counts, budget_exhausted=(), bulk=False):
        """Run run_index over two repos; *pending_counts* maps repo -> count
        returned by count_pending_issue_items; *budget_exhausted* is the list
        of repo names the (fake) budget stops before."""
        from shiori.ingest import run_index

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []  # chunks summary
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_conn.cursor.return_value.__enter__.side_effect = None

        settings = MagicMock()
        settings.repos = ["o/a", "o/b"]
        settings.dev_repos = set()
        settings.fetch_concurrency = 4

        class _FakeBudget:
            budget_seconds = 1.0
            def __init__(self, stop_before):
                self._stop_before = stop_before
            def exhausted(self):
                return repo in self._stop_before

        budget = _FakeBudget(budget_exhausted)

        repo = "o/a"  # referenced by _FakeBudget.exhausted's closure

        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.schema.migrate"),
            patch("shiori.ingest.schema.migrate_light"),
            patch("shiori.ingest._is_bulk_path", return_value=bulk),
            patch("shiori.ingest._bulk_covers_all_repos", return_value=True),
            patch("shiori.ingest._bulk_run_completed_all_repos",
                  side_effect=lambda completed, s: set(completed) == set(s.repos)),
            patch("shiori.ingest.schema.drop_heavy_indexes"),
            patch("shiori.ingest.schema.create_heavy_indexes") as mock_create,
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch("shiori.ingest._release_repo_lock"),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.index_docs", return_value=1),
            patch("shiori.ingest.index_issues", return_value=2),
            patch("shiori.ingest.index_code", return_value=3),
            patch("shiori.ingest._index_budget", return_value=budget),
            patch("shiori.ingest.db.count_pending_issue_items",
                  side_effect=lambda conn, r: pending_counts.get(r, 0)),
            patch("shiori.ingest.db.count_pending_issue_items_for_repos",
                  return_value=0),
            patch("shiori.ingest.db.record_sync_run") as mock_run,
            patch("shiori.ingest.db.record_sync_progress") as mock_progress,
            patch("shiori.ingest.db.record_sync_attempt") as mock_attempt,
        ):
            run_index(settings=settings)  # must not raise on truncation

        return {
            "create": mock_create,
            "run": mock_run,
            "progress": mock_progress,
            "attempt": mock_attempt,
        }

    def test_truncated_repo_records_progress_no_finished_at_no_completed(self, caplog):
        with caplog.at_level(logging.INFO, logger="shiori.ingest"):
            mocks = self._run(pending_counts={"o/a": 500, "o/b": 0})

        # o/a cut short: progress recorded (no finished_at), run NOT recorded
        mocks["progress"].assert_called_once_with(
            mocks["progress"].call_args.args[0], "o/a", mocks["progress"].call_args.args[2],
            pending_count=500,
        )
        # o/b fully indexed: normal run record
        mocks["run"].assert_called_once()
        assert mocks["run"].call_args.args[1] == "o/b"
        # attempt recorded only for the completed repo (truncation is not a failure)
        assert mocks["attempt"].call_count == 1
        assert mocks["attempt"].call_args.args[1] == "o/b"
        assert mocks["attempt"].call_args.kwargs == {"success": True}

        messages = [r.getMessage() for r in caplog.records]
        assert any("budget-truncated" in m and "o/a" in m for m in messages)
        assert any("truncated=1" in m for m in messages)

    def test_truncated_repo_defers_heavy_indexes(self, caplog):
        """A truncated bulk run must NOT rebuild HNSW/pgroonga over partial
        data: o/a was cut short, so completed == {o/b} != all repos and the
        gate defers (MUST)."""
        with caplog.at_level(logging.INFO, logger="shiori.ingest"):
            mocks = self._run(
                pending_counts={"o/a": 500, "o/b": 0},
                bulk=True,
            )

        mocks["create"].assert_not_called()
        messages = [r.getMessage() for r in caplog.records]
        assert any("heavy indexes deferred" in m and "o/a" in m for m in messages)

    def test_all_completed_still_rebuilds_heavy_indexes(self):
        mocks = self._run(pending_counts={"o/a": 0, "o/b": 0}, bulk=True)
        mocks["create"].assert_called_once()
        assert mocks["run"].call_count == 2

    def test_budget_stop_before_repo_skips_it_entirely(self, caplog):
        with caplog.at_level(logging.INFO, logger="shiori.ingest"):
            mocks = self._run(
                pending_counts={"o/a": 0, "o/b": 0},
                budget_exhausted=["o/a", "o/b"],
            )

        # nothing processed at all, clean exit 0
        mocks["run"].assert_not_called()
        mocks["progress"].assert_not_called()
        messages = [r.getMessage() for r in caplog.records]
        assert any("budget exhausted" in m for m in messages)

    def test_exit_zero_on_truncation_is_a_normal_outcome(self, caplog):
        """A budget-truncated run must NOT raise (only actual failures do):
        the next invocation resumes via indexed_at."""
        # _run() already returned normally; assert no exception surfaced
        # and the summary line reports the truncation
        with caplog.at_level(logging.INFO, logger="shiori.ingest"):
            self._run(pending_counts={"o/a": 500, "o/b": 0})
        messages = [r.getMessage() for r in caplog.records]
        assert any("index run summary:" in m for m in messages)
        summary = next(m for m in messages if "index run summary:" in m)
        assert "completed_repos=1" in summary
        assert "truncated=1" in summary


class TestRunIngestPassesBudgetToIndexIssues:
    def test_index_issues_receives_the_run_budget(self):
        """run_ingest Phase 2 constructs the budget after the fetch phase
        (fetch time is not billed) and hands it to index_issues."""
        from shiori.ingest import run_ingest

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (True,)
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        settings = MagicMock()
        settings.repos = ["owner/repo"]
        settings.dev_repos = set()
        settings.fetch_concurrency = 4
        settings.ingest_time_budget = 60.0

        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.schema.migrate"),
            patch("shiori.ingest.schema.migrate_light"),
            patch("shiori.ingest._is_bulk_path", return_value=False),
            patch("shiori.ingest._should_skip_repo", return_value=False),
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch("shiori.ingest._release_repo_lock"),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.fetch_docs", return_value="abc123"),
            patch("shiori.ingest.fetch_issues", return_value=5),
            patch("shiori.ingest.index_docs", return_value=1),
            patch("shiori.ingest.index_issues") as mock_index_issues,
            patch("shiori.ingest.index_code", return_value=3),
            patch("shiori.ingest.db.record_sync_run",
                   return_value=MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00")),
            patch("shiori.ingest.db.record_sync_attempt"),
            patch("shiori.ingest.db.count_pending_issue_items", return_value=0),
            patch("shiori.ingest.db.count_pending_issue_items_for_repos",
                  return_value=0),
        ):
            run_ingest(settings=settings)

        budget = mock_index_issues.call_args.kwargs["budget"]
        assert isinstance(budget, IndexBudget)
        assert budget.budget_seconds == 60.0
