"""Tests for issue #376: volume-based bulk-path selection.

A run takes the bulk (batched) path when the pending indexing work for the
*targeted* repos reaches SHIORI_BULK_PENDING_THRESHOLD; runs below the
threshold keep today's behaviour exactly (normal path, heavy indexes
untouched, no ChunkBuffer). The volume check is a single COUNT over
issue_items scoped to the target repos with the same pending predicate
index_issues applies -- nothing is truncated, reset, or re-fetched.

The scoped-run heavy-index gate (issues #364/#365) must keep holding: a
scoped bulk run -- whether triggered by rebuild, a drain marker, or now by
volume -- neither drops nor creates the heavy indexes.

No PostgreSQL in the sandbox: everything mocks at the connection/cursor
boundary (see tests/test_reindex.py).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from shiori import ingest
from shiori.config import DEFAULT_BULK_PENDING_THRESHOLD, Settings


def _sql_of(call_obj) -> str:
    """execute() に渡された psycopg.sql.Composable を素の SQL 文字列にする。"""
    query = call_obj.args[0]
    return query if isinstance(query, str) else query.as_string(None)


def _bulk_path_conn(*, chunks_exists=True, chunk_count=5, hnsw_exists=True, pending=0):
    """Cursor whose fetchone results follow _is_bulk_path's query order:
    chunks regclass, chunks count, hnsw regclass, then (only when the
    volume check runs) the pending COUNT result."""
    conn = MagicMock()
    cursor = MagicMock()
    fetchone_results = [("chunks",) if chunks_exists else (None,)]
    if chunks_exists:
        fetchone_results.append((chunk_count,))
        if chunk_count != 0:
            fetchone_results.append(
                ("chunks_embedding_hnsw",) if hnsw_exists else (None,)
            )
            if hnsw_exists:
                fetchone_results.append((pending,))
    cursor.fetchone.side_effect = fetchone_results
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn, cursor


def _settings_with_threshold(threshold: int) -> MagicMock:
    s = MagicMock()
    s.bulk_pending_threshold = threshold
    return s


# ===================================================================
# _is_bulk_path: volume threshold (issue #376)
# ===================================================================


class TestVolumeThreshold:
    def test_pending_above_threshold_takes_bulk_path(self):
        """A healthy DB (heavy indexes present, non-empty chunks) with a
        large backlog must take the bulk path."""
        conn, _ = _bulk_path_conn(pending=20_000)
        assert (
            ingest._is_bulk_path(
                conn, rebuild=False,
                targets=["o/a"], settings=_settings_with_threshold(10_000),
            )
            is True
        )

    def test_pending_at_threshold_takes_bulk_path(self):
        conn, _ = _bulk_path_conn(pending=10_000)
        assert (
            ingest._is_bulk_path(
                conn, rebuild=False,
                targets=["o/a"], settings=_settings_with_threshold(10_000),
            )
            is True
        )

    def test_pending_below_threshold_stays_normal_path(self):
        conn, _ = _bulk_path_conn(chunk_count=42, pending=99)
        assert (
            ingest._is_bulk_path(
                conn, rebuild=False,
                targets=["o/a"], settings=_settings_with_threshold(10_000),
            )
            is False
        )

    def test_zero_pending_stays_normal_path(self):
        conn, _ = _bulk_path_conn(chunk_count=42, pending=0)
        assert (
            ingest._is_bulk_path(
                conn, rebuild=False,
                targets=["o/a"], settings=_settings_with_threshold(10_000),
            )
            is False
        )

    def test_count_is_single_query_scoped_to_target_repos(self):
        """Requirement: cheap -- one COUNT over issue_items scoped to the
        target repos, no per-repo loop, no row fetch."""
        conn, cursor = _bulk_path_conn(pending=5)
        assert (
            ingest._is_bulk_path(
                conn, rebuild=False,
                targets=["o/a", "o/b"], settings=_settings_with_threshold(3),
            )
            is True
        )
        issue_queries = [
            c for c in cursor.execute.call_args_list if "issue_items" in _sql_of(c)
        ]
        assert len(issue_queries) == 1
        sql = _sql_of(issue_queries[0])
        assert "SELECT count(*)" in sql
        assert "WHERE repo = ANY" in sql
        # Same pending predicate index_issues applies (issue #318).
        assert "indexed_at IS NULL" in sql
        assert "updated_at > indexed_at" in sql
        # Params: the targets list, as a single ANY() argument.
        assert issue_queries[0].args[1] == (["o/a", "o/b"],)

    def test_backward_compat_two_arg_call_skips_volume_check(self):
        """Callers that predate the volume check (e.g. shiori.pipeline's
        duplicated copy, tests) keep the exact pre-#376 behaviour: no
        volume query is issued."""
        conn, cursor = _bulk_path_conn(pending=99_999)
        assert ingest._is_bulk_path(conn, rebuild=False) is False
        sqls = [_sql_of(c) for c in cursor.execute.call_args_list]
        assert not any("issue_items" in s for s in sqls)

    def test_hnsw_absent_short_circuits_before_volume_query(self):
        """A drain in progress (heavy indexes absent) is still the persistent
        marker: bulk regardless of volume, and no extra COUNT is issued."""
        conn, cursor = _bulk_path_conn(chunk_count=42, hnsw_exists=False)
        assert (
            ingest._is_bulk_path(
                conn, rebuild=False,
                targets=["o/a"], settings=_settings_with_threshold(10_000),
            )
            is True
        )
        sqls = [_sql_of(c) for c in cursor.execute.call_args_list]
        assert not any("issue_items" in s for s in sqls)

    def test_mock_settings_without_threshold_skips_volume_check(self):
        """Defensive: a settings object without bulk_pending_threshold (e.g.
        a bare MagicMock) must not crash the check."""
        conn, cursor = _bulk_path_conn(pending=99_999)
        assert (
            ingest._is_bulk_path(
                conn, rebuild=False, targets=["o/a"], settings=MagicMock()
            )
            is False
        )
        sqls = [_sql_of(c) for c in cursor.execute.call_args_list]
        assert not any("issue_items" in s for s in sqls)


# ===================================================================
# Configuration parsing: SHIORI_BULK_PENDING_THRESHOLD (issue #376)
# ===================================================================


class TestThresholdConfig:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, DEFAULT_BULK_PENDING_THRESHOLD),   # unset
            ("", DEFAULT_BULK_PENDING_THRESHOLD),     # empty
            ("   ", DEFAULT_BULK_PENDING_THRESHOLD),  # whitespace
            ("abc", DEFAULT_BULK_PENDING_THRESHOLD),  # unparseable
            ("10.5", DEFAULT_BULK_PENDING_THRESHOLD),  # unparseable
            ("0", DEFAULT_BULK_PENDING_THRESHOLD),    # non-positive
            ("-5", DEFAULT_BULK_PENDING_THRESHOLD),   # negative
            ("2500", 2500),                           # valid
            (" 5000 ", 5000),                         # valid, trimmed
        ],
    )
    def test_env_value_or_fallback(self, monkeypatch, raw, expected):
        if raw is None:
            monkeypatch.delenv("SHIORI_BULK_PENDING_THRESHOLD", raising=False)
        else:
            monkeypatch.setenv("SHIORI_BULK_PENDING_THRESHOLD", raw)
        assert Settings().bulk_pending_threshold == expected


# ===================================================================
# run_index integration: volume-triggered bulk on a scoped target list
# (issues #364/#365 gates must hold for the new trigger)
# ===================================================================


class TestScopedVolumeBulkRun:
    """A run targeting a single repo with a large backlog takes the bulk
    path (gets a ChunkBuffer) but must still neither drop nor create the
    heavy indexes, and must never truncate anything."""

    def _run_index(self, pending: int, repos_arg: list[str] | None):
        from shiori.ingest import run_index

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            ("chunks",), (42,), ("chunks_embedding_hnsw",), (pending,),
        ]
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        settings = MagicMock()
        settings.repos = ["o/a", "o/b"]
        settings.dev_repos = set()
        settings.bulk_pending_threshold = 10_000

        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.schema.migrate_light") as mock_migrate_light,
            patch("shiori.ingest.schema.migrate") as mock_migrate,
            patch("shiori.ingest.schema.truncate_all_repos") as mock_truncate,
            patch("shiori.ingest.schema.drop_heavy_indexes") as mock_drop,
            patch("shiori.ingest.schema.create_heavy_indexes") as mock_create,
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch("shiori.ingest._release_repo_lock"),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.ChunkBuffer") as mock_buffer_cls,
            patch("shiori.ingest.index_docs", return_value=0),
            patch("shiori.ingest.index_issues", return_value=0),
            patch("shiori.ingest.index_code", return_value=0),
            patch(
                "shiori.ingest.db.record_sync_run",
                return_value=MagicMock(
                    isoformat=lambda: "2026-01-01T00:00:00+00:00"
                ),
            ),
            patch("shiori.ingest.db.record_sync_attempt"),
        ):
            run_index(settings=settings, repos=repos_arg)
        return {
            "migrate_light": mock_migrate_light,
            "migrate": mock_migrate,
            "truncate": mock_truncate,
            "drop": mock_drop,
            "create": mock_create,
            "buffer": mock_buffer_cls,
        }

    def test_large_backlog_scoped_run_is_bulk_but_defers_heavy_indexes(self, caplog):
        with caplog.at_level(logging.INFO, logger="shiori.ingest"):
            mocks = self._run_index(pending=50_000, repos_arg=["o/a"])

        # Bulk path taken: light schema, ChunkBuffer constructed.
        mocks["migrate_light"].assert_called_once()
        mocks["migrate"].assert_not_called()
        assert mocks["buffer"].called
        # Already-indexed items are never discarded (rebuild-only op).
        mocks["truncate"].assert_not_called()
        # Scoped run: the #364/#365 gates still refuse drop AND create.
        mocks["drop"].assert_not_called()
        mocks["create"].assert_not_called()
        # The deferral is logged, exactly as for any scoped bulk run.
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            "heavy indexes drop skipped" in m and "scoped bulk run" in m
            for m in messages
        )

    def test_unscoped_large_backlog_bulk_run_drops_and_recreates(self):
        """A volume-triggered bulk run that covers every configured repo may
        drop at the start and recreates at the end (existing gate, unchanged
        by #376 -- it only makes bulk runs reachable more often)."""
        mocks = self._run_index(pending=50_000, repos_arg=None)
        assert mocks["drop"].called
        assert mocks["create"].called
        mocks["truncate"].assert_not_called()
        assert mocks["buffer"].called

    def test_small_backlog_stays_normal_path_bit_for_bit(self):
        """Below the threshold: today's exact behaviour -- full migrate, no
        ChunkBuffer, heavy indexes untouched."""
        mocks = self._run_index(pending=99, repos_arg=["o/a"])
        mocks["migrate"].assert_called_once()
        mocks["migrate_light"].assert_not_called()
        mocks["buffer"].assert_not_called()
        mocks["truncate"].assert_not_called()
        mocks["drop"].assert_not_called()
        mocks["create"].assert_not_called()
