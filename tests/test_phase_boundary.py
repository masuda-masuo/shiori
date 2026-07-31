"""Phase-boundary connection lifetime (issue #373).

``db.connect()`` opens with ``autocommit=False``: a pre-flight connection
carried across a phase boundary sits idle in an open transaction until
PostgreSQL kills it (``idle_in_transaction_session_timeout``), breaking the
next phase with ``IdleInTransactionSessionTimeout``.

These tests pin the fix: the pre-flight connection (bulk-path detection,
schema prep, circuit-breaker pre-check) is finished with and closed before
the fetch/embed phase begins, and each phase uses its own connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shiori.db import connect_scope


class TestConnectScope:
    """db.connect_scope: transaction and connection cannot leak out of the block."""

    def test_commits_and_closes_on_success(self):
        mock_conn = MagicMock()
        with patch("shiori.db.connect", return_value=mock_conn):
            with connect_scope(MagicMock()) as conn:
                assert conn is mock_conn

        mock_conn.commit.assert_called_once()
        mock_conn.close.assert_called_once()
        mock_conn.rollback.assert_not_called()

    def test_rolls_back_and_closes_on_exception(self):
        mock_conn = MagicMock()
        with patch("shiori.db.connect", return_value=mock_conn):
            with pytest.raises(RuntimeError, match="boom"):
                with connect_scope(MagicMock()):
                    raise RuntimeError("boom")

        mock_conn.rollback.assert_called_once()
        mock_conn.close.assert_called_once()
        mock_conn.commit.assert_not_called()

    def test_rolls_back_and_closes_on_base_exception(self):
        """SystemExit/KeyboardInterrupt must not leak the connection either."""
        mock_conn = MagicMock()
        with patch("shiori.db.connect", return_value=mock_conn):
            with pytest.raises(SystemExit):
                with connect_scope(MagicMock()):
                    raise SystemExit(1)

        mock_conn.rollback.assert_called_once()
        mock_conn.close.assert_called_once()


def _settings(repos=None):
    s = MagicMock()
    s.repos = repos if repos is not None else ["owner/repo"]
    s.dev_repos = set()
    s.ref_backfill_since = None
    s.fetch_concurrency = 4
    return s


class TestRunFetchPhaseBoundary:
    """run_fetch: pre-flight connection closed before the fetch phase."""

    def test_pre_flight_connection_closed_before_fetch_begins(self):
        from shiori.ingest import run_fetch

        pre_conn = MagicMock()
        fetch_conn = MagicMock()
        settings = _settings()

        with (
            patch("shiori.ingest.db.connect", side_effect=[pre_conn, fetch_conn]),
            patch("shiori.ingest.schema.migrate_light"),
            patch("shiori.ingest.schema.migrate"),
            patch("shiori.ingest._should_skip_repo", return_value=False) as mock_should_skip,
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch("shiori.ingest._release_repo_lock"),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.fetch_docs", return_value="abc123") as mock_fetch_docs,
            patch("shiori.ingest.fetch_issues", return_value=5) as mock_fetch_issues,
        ):
            run_fetch(settings=settings)

        # The circuit-breaker pre-check ran on the pre-flight connection...
        mock_should_skip.assert_called_once_with(pre_conn, "owner/repo", settings, False)
        # ...which was finished with (committed and closed) before the executor ran.
        pre_conn.commit.assert_called_once()
        pre_conn.close.assert_called_once()
        # The fetch phase ran on its own connection, never the pre-flight one.
        assert mock_fetch_docs.call_args[0][1] is fetch_conn
        assert mock_fetch_issues.call_args[0][1] is fetch_conn


class TestRunIndexPhaseBoundary:
    """run_index: pre-flight connection closed before the index loop."""

    def test_pre_flight_connection_closed_before_index_begins(self):
        from shiori.ingest import run_index

        pre_conn = MagicMock()
        idx_conn = MagicMock()
        settings = _settings()

        with (
            patch("shiori.ingest.db.connect", side_effect=[pre_conn, idx_conn]),
            patch("shiori.ingest.schema.migrate"),
            patch("shiori.ingest.schema.migrate_light"),
            patch("shiori.ingest._is_bulk_path", return_value=False) as mock_is_bulk,
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch("shiori.ingest._release_repo_lock"),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.index_docs", return_value=1) as mock_index_docs,
            patch("shiori.ingest.index_issues", return_value=2),
            patch("shiori.ingest.index_code", return_value=3),
            patch(
                "shiori.ingest.db.record_sync_run",
                return_value=MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00"),
            ),
            patch("shiori.ingest.db.record_sync_attempt"),
        ):
            run_index(settings=settings)

        # Bulk-path detection ran on the pre-flight connection...
        assert mock_is_bulk.call_args[0][0] is pre_conn
        # ...which was finished with before the index loop started.
        pre_conn.commit.assert_called_once()
        pre_conn.close.assert_called_once()
        # The index loop ran on its own fresh connection.
        assert mock_index_docs.call_args[0][1] is idx_conn


class TestRunIngestPhaseBoundary:
    """run_ingest: pre-flight connection closed before Phase 1; Phase 2 fresh."""

    def test_pre_flight_closed_and_phases_use_own_connections(self):
        from shiori.ingest import run_ingest

        pre_conn = MagicMock()
        fetch_conn = MagicMock()
        idx_conn = MagicMock()
        settings = _settings()

        with (
            patch("shiori.ingest.db.connect", side_effect=[pre_conn, fetch_conn, idx_conn]),
            patch("shiori.ingest.schema.migrate"),
            patch("shiori.ingest.schema.migrate_light"),
            patch("shiori.ingest._is_bulk_path", return_value=False),
            patch("shiori.ingest._should_skip_repo", return_value=False) as mock_should_skip,
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch("shiori.ingest._release_repo_lock"),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.fetch_docs", return_value="abc123") as mock_fetch_docs,
            patch("shiori.ingest.fetch_issues", return_value=5) as mock_fetch_issues,
            patch("shiori.ingest.index_docs", return_value=1) as mock_index_docs,
            patch("shiori.ingest.index_issues", return_value=2),
            patch("shiori.ingest.index_code", return_value=3),
            patch(
                "shiori.ingest.db.record_sync_run",
                return_value=MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00"),
            ),
            patch("shiori.ingest.db.record_sync_attempt"),
        ):
            run_ingest(settings=settings)

        # The circuit-breaker pre-check ran on the pre-flight connection...
        mock_should_skip.assert_called_once_with(pre_conn, "owner/repo", settings, False)
        # ...which was finished with (committed and closed) before Phase 1.
        pre_conn.commit.assert_called_once()
        pre_conn.close.assert_called_once()
        # Phase 1 fetch ran on its own per-thread connection, never pre_flight.
        assert mock_fetch_docs.call_args[0][1] is fetch_conn
        assert mock_fetch_issues.call_args[0][1] is fetch_conn
        # Phase 2 (index) opened its own fresh connection.
        assert mock_index_docs.call_args[0][1] is idx_conn

    def test_explicit_circuit_broken_repo_still_closes_pre_flight_conn(self):
        """_should_skip_repo raising (explicit CB-broken repo) must still end
        the pre-flight transaction and close the connection."""
        from shiori.ingest import run_ingest

        pre_conn = MagicMock()
        settings = _settings()

        with (
            patch("shiori.ingest.db.connect", return_value=pre_conn),
            patch("shiori.ingest.schema.migrate"),
            patch("shiori.ingest.schema.migrate_light"),
            patch("shiori.ingest._is_bulk_path", return_value=False),
            patch(
                "shiori.ingest._should_skip_repo",
                side_effect=ValueError("Repo owner/repo is circuit-broken"),
            ),
        ):
            with pytest.raises(ValueError, match="circuit-broken"):
                run_ingest(settings=settings)

        pre_conn.rollback.assert_called_once()
        pre_conn.close.assert_called_once()
