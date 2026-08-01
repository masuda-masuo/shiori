"""Tests for shiori#352: bulk reindex mode that preserves fetched raw data.

Covers:
- schema.reindex_prepare (chunks/doc_files cleared; issue_items/sync_state/
  sync_runs/repo_index_state preserved)
- the _is_bulk_path HNSW-absence extension (both the shiori.ingest copy and
  the duplicated shiori.pipeline copy)
- ingest.run_reindex orchestration
- schema.create_heavy_indexes build knobs (SHIORI_MAINTENANCE_WORK_MEM /
  SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS)
- the run_fetch / mcp_server startup migrate -> migrate_light switch

No PostgreSQL in the sandbox: everything mocks at the connection/cursor
boundary (see tests/test_forget.py, tests/test_cli_ingest_args.py).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from shiori import schema
from shiori.config import Settings


def _sql_of(call_obj) -> str:
    """execute() に渡された psycopg.sql.Composable を素の SQL 文字列にする。"""
    query = call_obj.args[0]
    return query if isinstance(query, str) else query.as_string(None)


def _mock_conn():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn, cursor


# ===================================================================
# schema.reindex_prepare
# ===================================================================


class TestReindexPrepareUnscoped:
    """repos=None: every repo, via TRUNCATE (unscoped)."""

    def test_truncates_chunks(self):
        conn, cursor = _mock_conn()
        schema.reindex_prepare(conn, None)
        sqls = [_sql_of(c) for c in cursor.execute.call_args_list]
        assert "TRUNCATE chunks" in sqls

    def test_resets_indexed_at_without_deleting_issue_items(self):
        conn, cursor = _mock_conn()
        schema.reindex_prepare(conn, None)
        sqls = [_sql_of(c) for c in cursor.execute.call_args_list]
        assert "UPDATE issue_items SET indexed_at = NULL" in sqls
        assert not any("issue_items" in s and "DELETE" in s for s in sqls)
        assert not any("issue_items" in s and "TRUNCATE" in s for s in sqls)

    def test_deletes_doc_files(self):
        conn, cursor = _mock_conn()
        schema.reindex_prepare(conn, None)
        sqls = [_sql_of(c) for c in cursor.execute.call_args_list]
        assert "DELETE FROM doc_files" in sqls

    def test_never_touches_sync_state_sync_runs_repo_index_state(self):
        conn, cursor = _mock_conn()
        schema.reindex_prepare(conn, None)
        joined = " ".join(_sql_of(c) for c in cursor.execute.call_args_list)
        for table in ("sync_state", "sync_runs", "repo_index_state"):
            assert table not in joined


class TestReindexPrepareScoped:
    """repos=[...]: DELETE/UPDATE scoped with WHERE repo = ANY(...)."""

    def test_scopes_chunks_delete_to_repos(self):
        conn, cursor = _mock_conn()
        schema.reindex_prepare(conn, ["owner/a", "owner/b"])
        calls = [
            c for c in cursor.execute.call_args_list
            if _sql_of(c).startswith("DELETE FROM chunks")
        ]
        assert len(calls) == 1
        assert "WHERE repo = ANY" in _sql_of(calls[0])
        assert calls[0].args[1] == (["owner/a", "owner/b"],)

    def test_scopes_indexed_at_reset_to_repos(self):
        conn, cursor = _mock_conn()
        schema.reindex_prepare(conn, ["owner/a"])
        calls = [
            c for c in cursor.execute.call_args_list
            if _sql_of(c).startswith("UPDATE issue_items")
        ]
        assert len(calls) == 1
        assert "WHERE repo = ANY" in _sql_of(calls[0])
        assert calls[0].args[1] == (["owner/a"],)

    def test_scopes_doc_files_delete_to_repos(self):
        conn, cursor = _mock_conn()
        schema.reindex_prepare(conn, ["owner/a"])
        calls = [
            c for c in cursor.execute.call_args_list
            if _sql_of(c).startswith("DELETE FROM doc_files")
        ]
        assert len(calls) == 1
        assert "WHERE repo = ANY" in _sql_of(calls[0])
        assert calls[0].args[1] == (["owner/a"],)

    def test_never_touches_other_tables_or_full_issue_items_delete(self):
        conn, cursor = _mock_conn()
        schema.reindex_prepare(conn, ["owner/a"])
        joined = " ".join(_sql_of(c) for c in cursor.execute.call_args_list)
        for table in ("sync_state", "sync_runs", "repo_index_state"):
            assert table not in joined
        assert "DELETE FROM issue_items" not in joined
        assert "TRUNCATE issue_items" not in joined
        assert "TRUNCATE chunks" not in joined  # scoped: DELETE, not TRUNCATE


# ===================================================================
# _is_bulk_path: HNSW-absence extension (issue #352)
# ===================================================================


class TestIsBulkPathHnswExtension:
    """shiori.ingest._is_bulk_path and shiori.pipeline._is_bulk_path are two
    independent copies (pipeline.py predates the shared-helper extraction,
    issue #281) that must both treat an absent HNSW index as a bulk-path
    signal, while leaving the existing True/False cases unchanged.
    """

    def _mock_conn(self, *, chunks_exists=True, chunk_count=5, hnsw_exists=True):
        conn = MagicMock()
        cursor = MagicMock()
        fetchone_results = [("chunks",) if chunks_exists else (None,)]
        if chunks_exists:
            fetchone_results.append((chunk_count,))
            if chunk_count != 0:
                fetchone_results.append(
                    ("chunks_embedding_hnsw",) if hnsw_exists else (None,)
                )
        cursor.fetchone.side_effect = fetchone_results
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn

    @pytest.mark.parametrize("module_name", ["shiori.ingest", "shiori.pipeline"])
    def test_hnsw_absent_is_bulk_even_with_nonempty_chunks(self, module_name):
        import importlib
        mod = importlib.import_module(module_name)
        conn = self._mock_conn(chunks_exists=True, chunk_count=42, hnsw_exists=False)
        assert mod._is_bulk_path(conn, rebuild=False) is True

    @pytest.mark.parametrize("module_name", ["shiori.ingest", "shiori.pipeline"])
    def test_hnsw_present_nonempty_chunks_is_not_bulk(self, module_name):
        import importlib
        mod = importlib.import_module(module_name)
        conn = self._mock_conn(chunks_exists=True, chunk_count=42, hnsw_exists=True)
        assert mod._is_bulk_path(conn, rebuild=False) is False

    @pytest.mark.parametrize("module_name", ["shiori.ingest", "shiori.pipeline"])
    def test_missing_chunks_table_is_bulk_regardless_of_hnsw(self, module_name):
        import importlib
        mod = importlib.import_module(module_name)
        conn = self._mock_conn(chunks_exists=False)
        assert mod._is_bulk_path(conn, rebuild=False) is True

    @pytest.mark.parametrize("module_name", ["shiori.ingest", "shiori.pipeline"])
    def test_empty_chunks_table_is_bulk_without_checking_hnsw(self, module_name):
        import importlib
        mod = importlib.import_module(module_name)
        conn = self._mock_conn(chunks_exists=True, chunk_count=0)
        assert mod._is_bulk_path(conn, rebuild=False) is True

    @pytest.mark.parametrize("module_name", ["shiori.ingest", "shiori.pipeline"])
    def test_rebuild_true_short_circuits_without_querying(self, module_name):
        import importlib
        mod = importlib.import_module(module_name)
        conn = MagicMock()
        assert mod._is_bulk_path(conn, rebuild=True) is True
        conn.cursor.assert_not_called()


# ===================================================================
# ingest.run_reindex orchestration
# ===================================================================


class TestRunReindex:
    def _mock_settings(self, repos=None):
        s = MagicMock()
        s.repos = repos if repos is not None else ["owner/a", "owner/b"]
        return s

    def test_unscoped_prepares_migrates_drops_then_reindexes_all(self):
        from shiori.ingest import run_reindex

        mock_conn = MagicMock()
        settings = self._mock_settings()
        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.schema.migrate_light") as mock_migrate_light,
            patch("shiori.ingest.schema.reindex_prepare") as mock_prepare,
            patch("shiori.ingest.schema.drop_heavy_indexes") as mock_drop,
            patch("shiori.ingest.run_index") as mock_run_index,
        ):
            run_reindex(settings=settings, repos=None)

        mock_migrate_light.assert_called_once_with(mock_conn, settings)
        mock_prepare.assert_called_once_with(mock_conn, None)
        mock_conn.commit.assert_called_once()
        mock_drop.assert_called_once_with(mock_conn)
        mock_conn.close.assert_called_once()
        mock_run_index.assert_called_once_with(settings=settings, repos=None)

    def test_scoped_passes_repo_list_through_unresolved(self):
        """The original (possibly-None) repos arg is what schema.reindex_prepare
        and run_index receive -- not the allowlist-resolved target list --
        so the scoped/unscoped distinction from the CLI survives.
        """
        from shiori.ingest import run_reindex

        mock_conn = MagicMock()
        settings = self._mock_settings(repos=["owner/a", "owner/b"])
        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.schema.migrate_light"),
            patch("shiori.ingest.schema.reindex_prepare") as mock_prepare,
            patch("shiori.ingest.schema.drop_heavy_indexes"),
            patch("shiori.ingest.run_index") as mock_run_index,
        ):
            run_reindex(settings=settings, repos=["owner/a"])

        mock_prepare.assert_called_once_with(mock_conn, ["owner/a"])
        mock_run_index.assert_called_once_with(settings=settings, repos=["owner/a"])

    def test_invalid_repo_rejected_before_any_db_write(self):
        from shiori.ingest import run_reindex

        settings = self._mock_settings(repos=["owner/a"])
        with (
            patch("shiori.ingest.db.connect") as mock_connect,
            patch("shiori.ingest.schema.reindex_prepare") as mock_prepare,
        ):
            with pytest.raises(SystemExit):
                run_reindex(settings=settings, repos=["evil/repo"])

        mock_connect.assert_not_called()
        mock_prepare.assert_not_called()

    def test_unscoped_with_no_shiori_repos_configured_raises(self):
        """--repo validation reuses _validate_repos (brief): unscoped
        reindex with no SHIORI_REPOS configured must not silently run.
        """
        from shiori.ingest import run_reindex

        settings = self._mock_settings(repos=[])
        with patch("shiori.ingest.db.connect") as mock_connect:
            with pytest.raises(SystemExit):
                run_reindex(settings=settings, repos=None)
        mock_connect.assert_not_called()


# ===================================================================
# schema.create_heavy_indexes: build knobs (issue #352)
# ===================================================================


class TestCreateHeavyIndexesKnobs:
    def _sql_texts(self, cursor):
        return [_sql_of(c) for c in cursor.execute.call_args_list]

    def test_default_settings_force_serial_build(self):
        conn, cursor = _mock_conn()
        settings = Settings()
        assert settings.max_parallel_maintenance_workers == 0
        assert settings.maintenance_work_mem is None

        schema.create_heavy_indexes(conn, settings)

        sqls = self._sql_texts(cursor)
        assert "SET max_parallel_maintenance_workers = 0" in sqls
        assert not any(s.startswith("SET maintenance_work_mem") for s in sqls)

    def test_maintenance_work_mem_applied_when_set(self):
        conn, cursor = _mock_conn()
        settings = Settings()
        settings.maintenance_work_mem = "256MB"
        settings.max_parallel_maintenance_workers = 2

        schema.create_heavy_indexes(conn, settings)

        sqls = self._sql_texts(cursor)
        assert "SET maintenance_work_mem = '256MB'" in sqls
        assert "SET max_parallel_maintenance_workers = 2" in sqls

    def test_set_statements_precede_index_creation(self):
        conn, cursor = _mock_conn()
        settings = Settings()
        settings.maintenance_work_mem = "128MB"

        schema.create_heavy_indexes(conn, settings)

        sqls = self._sql_texts(cursor)
        last_set_idx = max(i for i, s in enumerate(sqls) if s.startswith("SET "))
        hnsw_idx = next(i for i, s in enumerate(sqls) if "hnsw" in s.lower())
        assert last_set_idx < hnsw_idx

    def test_env_default_is_serial(self, monkeypatch):
        monkeypatch.delenv("SHIORI_MAINTENANCE_WORK_MEM", raising=False)
        monkeypatch.delenv("SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS", raising=False)
        settings = Settings()
        assert settings.max_parallel_maintenance_workers == 0
        assert settings.maintenance_work_mem is None

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("SHIORI_MAINTENANCE_WORK_MEM", "512MB")
        monkeypatch.setenv("SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS", "4")
        settings = Settings()
        assert settings.maintenance_work_mem == "512MB"
        assert settings.max_parallel_maintenance_workers == 4


# ===================================================================
# run_fetch: migrate_light only, never full migrate (issue #352)
# ===================================================================


class TestRunFetchNeverCallsFullMigrate:
    def test_migrate_light_called_migrate_not(self):
        from shiori.ingest import run_fetch

        mock_conn = MagicMock()
        settings = MagicMock()
        settings.repos = ["owner/repo"]
        settings.fetch_concurrency = 4

        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.schema.migrate") as mock_migrate,
            patch("shiori.ingest.schema.migrate_light") as mock_migrate_light,
            patch("shiori.ingest._acquire_repo_lock", return_value=False),
            patch("shiori.ingest._release_repo_lock"),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.fetch_docs"),
            patch("shiori.ingest.fetch_issues"),
        ):
            run_fetch(settings=settings)

        mock_migrate.assert_not_called()
        assert mock_migrate_light.called


# ===================================================================
# mcp_server.run(): migrate_light only at startup (issue #352)
# ===================================================================


class TestMcpServerStartupNeverCallsFullMigrate:
    def test_migrate_light_called_migrate_not(self):
        from shiori import mcp_server

        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False

        with (
            patch("shiori.mcp_server._conn", return_value=mock_conn),
            patch("shiori.mcp_server.schema.migrate") as mock_migrate,
            patch("shiori.mcp_server.schema.migrate_light") as mock_migrate_light,
            patch("shiori.mcp_server.mcp.run"),
        ):
            mcp_server.run(transport="stdio")

        mock_migrate.assert_not_called()
        mock_migrate_light.assert_called_once_with(mock_conn, mcp_server.settings)


# ===================================================================
# CLI: `shiori ingest reindex`
# ===================================================================


class TestCliReindexDispatch:
    def _run(self, argv):
        from shiori.__main__ import main
        with patch.object(sys, "argv", ["shiori"] + argv):
            main()

    def test_help_exits_0(self):
        with pytest.raises(SystemExit) as exc:
            self._run(["ingest", "reindex", "--help"])
        assert exc.value.code == 0

    def test_help_documents_preserved_vs_rebuilt(self, capsys):
        with pytest.raises(SystemExit):
            self._run(["ingest", "reindex", "--help"])
        out = capsys.readouterr().out
        assert "chunks" in out.lower()
        assert "issue_items" in out.lower()

    def test_no_repo_is_allowed_and_means_unscoped(self):
        """Unlike fetch/index/run (issue #338), reindex does not require
        --repo: omitting it means every configured repo."""
        with patch("shiori.ingest.run_reindex") as mock_reindex:
            self._run(["ingest", "reindex"])
        mock_reindex.assert_called_once_with(repos=None)

    def test_repo_scopes_reindex(self):
        with patch("shiori.ingest.run_reindex") as mock_reindex:
            self._run(["ingest", "reindex", "--repo", "a/b"])
        mock_reindex.assert_called_once_with(repos=["a/b"])

    def test_multiple_repo_flags(self):
        with patch("shiori.ingest.run_reindex") as mock_reindex:
            self._run(["ingest", "reindex", "--repo", "a/b", "--repo", "c/d"])
        mock_reindex.assert_called_once_with(repos=["a/b", "c/d"])

    def test_parent_repo_before_reindex_subcommand(self):
        """shiori ingest --repo a/b reindex -> repos flows from the parent
        flag, same backward-compatible pattern as fetch/index/run."""
        with patch("shiori.ingest.run_reindex") as mock_reindex:
            self._run(["ingest", "--repo", "a/b", "reindex"])
        mock_reindex.assert_called_once_with(repos=["a/b"])


# ===================================================================
# Bulk-path heavy index rebuild is gated on full repo coverage (#352)
# ===================================================================


class TestBulkHeavyIndexCoverageGate:
    """A scoped bulk run must defer create_heavy_indexes; only a run covering
    every configured repo rebuilds them (reviewer finding: an MCP ingest(repo=X)
    or CLI `index --repo X` during a drain would otherwise rebuild the heavy
    indexes early and flip later invocations off the fast bulk path)."""

    def _run_index(self, repos_arg, configured, lock_fails_for=None):
        """*lock_fails_for*: repo names for which _acquire_repo_lock returns
        False (issue #365 -- simulates a per-repo advisory-lock skip)."""
        from shiori.ingest import run_index

        lock_fails_for = lock_fails_for or set()

        def _lock(conn, repo):
            return repo not in lock_fails_for

        mock_conn = MagicMock()
        settings = MagicMock()
        settings.repos = configured
        settings.dev_repos = set()
        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.schema.migrate_light"),
            patch("shiori.ingest.schema.drop_heavy_indexes") as mock_drop,
            patch("shiori.ingest.schema.create_heavy_indexes") as mock_create,
            patch("shiori.ingest._acquire_repo_lock", side_effect=_lock),
            patch("shiori.ingest._release_repo_lock"),
            patch("shiori.ingest._is_bulk_path", return_value=True),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.ChunkBuffer", return_value=MagicMock()),
            patch("shiori.ingest.index_docs", return_value=0),
            patch("shiori.ingest.index_issues", return_value=0),
            patch("shiori.ingest.index_code", return_value=0),
            patch("shiori.ingest.db.record_sync_run",
                   return_value=MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00")),
            patch("shiori.ingest.db.record_sync_attempt"),
            # Issue #377: completion is decided from a post-pass pending
            # count; a zero count means "fully indexed" (success path) --
            # only completed repos feed the bulk heavy-index gate.
            patch("shiori.ingest.db.count_pending_issue_items", return_value=0),
            patch(
                "shiori.ingest.db.count_pending_issue_items_for_repos",
                return_value=0,
            ),
        ):
            run_index(settings=settings, repos=repos_arg)
        return mock_create, mock_drop

    def test_scoped_bulk_run_defers_heavy_indexes(self):
        mock_create, _ = self._run_index(["o/a"], ["o/a", "o/b"])
        mock_create.assert_not_called()

    def test_unscoped_bulk_run_creates_heavy_indexes(self):
        mock_create, _ = self._run_index(None, ["o/a", "o/b"])
        assert mock_create.called

    def test_explicit_full_repo_list_creates_heavy_indexes(self):
        mock_create, _ = self._run_index(["o/b", "o/a"], ["o/a", "o/b"])
        assert mock_create.called

    def test_covers_all_helper(self):
        from shiori.ingest import _bulk_covers_all_repos

        settings = MagicMock()
        settings.repos = ["o/a", "o/b"]
        assert _bulk_covers_all_repos(["o/a", "o/b"], settings)
        assert not _bulk_covers_all_repos(["o/a"], settings)
        settings.repos = []
        assert not _bulk_covers_all_repos([], settings)

    # ---------------------------------------------------------------
    # issue #364: the DROP side must be gated the same way as CREATE
    # ---------------------------------------------------------------

    def test_scoped_bulk_run_never_drops_heavy_indexes(self):
        """A scoped bulk run must not touch the DROP side either: during a
        genuine drain the indexes are already absent, and if they exist,
        dropping them is exactly the #364 accident (a scoped dev-lane run
        destroying another lane's in-progress or just-finished HNSW
        build)."""
        _, mock_drop = self._run_index(["o/a"], ["o/a", "o/b"])
        mock_drop.assert_not_called()

    def test_unscoped_bulk_run_drops_heavy_indexes(self):
        _, mock_drop = self._run_index(None, ["o/a", "o/b"])
        assert mock_drop.called

    # ---------------------------------------------------------------
    # issue #365: CREATE gates on repos that actually completed, not the
    # intended target list
    # ---------------------------------------------------------------

    def test_lock_skip_defers_create_despite_full_target_list(self):
        """The coverage gate on the *intended* target list (o/a, o/b) still
        passes, but o/b never actually completed (advisory-lock skip) --
        create_heavy_indexes must not run over the resulting partial data.
        The DROP side gates on intention only (it runs before the loop, so
        completion isn't knowable yet), so an unscoped run still drops."""
        mock_create, mock_drop = self._run_index(
            None, ["o/a", "o/b"], lock_fails_for={"o/b"}
        )
        mock_create.assert_not_called()
        assert mock_drop.called

    def test_lock_skip_logs_reason_distinct_from_scoped_defer(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="shiori.ingest"):
            self._run_index(None, ["o/a", "o/b"], lock_fails_for={"o/b"})
        messages = [r.getMessage() for r in caplog.records]
        assert any("advisory lock" in m and "o/b" in m for m in messages)
        assert not any("scoped bulk run" in m for m in messages)

    def test_completed_all_helper(self):
        from shiori.ingest import _bulk_run_completed_all_repos

        settings = MagicMock()
        settings.repos = ["o/a", "o/b"]
        assert _bulk_run_completed_all_repos(["o/a", "o/b"], settings)
        assert not _bulk_run_completed_all_repos(["o/a"], settings)
        settings.repos = []
        assert not _bulk_run_completed_all_repos([], settings)


# ===================================================================
# CLI: `shiori ingest index --all` (unscoped resume path, #352)
# ===================================================================


class TestCliIndexAll:
    def _run(self, argv):
        from shiori.__main__ import main
        with patch.object(sys, "argv", ["shiori"] + argv):
            main()

    def test_index_all_runs_unscoped(self):
        with patch("shiori.ingest.run_index") as mock_run_index:
            self._run(["ingest", "index", "--all"])
        mock_run_index.assert_called_once_with(repos=None, rebuild=False)

    def test_index_without_repo_or_all_still_errors(self):
        with pytest.raises(SystemExit) as exc:
            self._run(["ingest", "index"])
        assert exc.value.code == 2

    def test_all_and_repo_are_mutually_exclusive(self):
        with pytest.raises(SystemExit) as exc:
            self._run(["ingest", "index", "--all", "--repo", "a/b"])
        assert exc.value.code == 2


# ===================================================================
# Bulk-path heavy index drop/create gating at the run_ingest call site
# (issues #364/#365 -- extends TestBulkHeavyIndexCoverageGate's fixture
# style to the fetch+index-combined entry point)
# ===================================================================


class TestRunIngestBulkHeavyIndexGates:
    """Same gates as run_index (#364 DROP gate, #365 completed-set CREATE
    gate), exercised through run_ingest's fetch-then-index path."""

    def _mock_conn(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (True,)
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        return mock_conn

    def _run(self, repos_arg, configured, lock_fails_for=None):
        """*lock_fails_for*: repo names for which _acquire_repo_lock returns
        False in both the fetch and index phases (issue #365)."""
        from shiori.ingest import run_ingest

        lock_fails_for = lock_fails_for or set()

        def _lock(conn, repo):
            return repo not in lock_fails_for

        mock_conn = self._mock_conn()
        mock_settings = MagicMock()
        mock_settings.repos = configured
        mock_settings.dev_repos = set()
        mock_settings.fetch_concurrency = 4

        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.schema.migrate_light"),
            patch("shiori.ingest.schema.migrate"),
            patch("shiori.ingest.schema.drop_heavy_indexes") as mock_drop,
            patch("shiori.ingest.schema.create_heavy_indexes") as mock_create,
            patch("shiori.ingest._is_bulk_path", return_value=True),
            patch("shiori.ingest._acquire_repo_lock", side_effect=_lock),
            patch("shiori.ingest._release_repo_lock"),
            patch("shiori.ingest.ChunkBuffer", return_value=MagicMock()),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.Embedder", return_value=MagicMock()),
            patch("shiori.ingest.fetch_docs", return_value="abc123"),
            patch("shiori.ingest.fetch_issues", return_value=5),
            patch("shiori.ingest.index_docs", return_value=1),
            patch("shiori.ingest.index_issues", return_value=2),
            patch("shiori.ingest.index_code", return_value=3),
            patch(
                "shiori.ingest.db.record_sync_run",
                return_value=MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00"),
            ),
            patch("shiori.ingest.db.record_sync_attempt"),
            # Issue #377: completion is decided from a post-pass pending
            # count; a zero count means "fully indexed" (success path) --
            # only completed repos feed the bulk heavy-index gate.
            patch("shiori.ingest.db.count_pending_issue_items", return_value=0),
            patch(
                "shiori.ingest.db.count_pending_issue_items_for_repos",
                return_value=0,
            ),
        ):
            run_ingest(settings=mock_settings, repos=repos_arg)
        return mock_create, mock_drop

    def test_scoped_bulk_run_never_drops_or_creates(self):
        mock_create, mock_drop = self._run(
            ["owner/repo1"], ["owner/repo1", "owner/repo2"]
        )
        mock_drop.assert_not_called()
        mock_create.assert_not_called()

    def test_unscoped_bulk_run_drops_and_creates(self):
        mock_create, mock_drop = self._run(None, ["owner/repo1", "owner/repo2"])
        assert mock_drop.called
        assert mock_create.called

    def test_lock_skip_drops_but_defers_create(self):
        """DROP gates on intention (runs before the loop, so completion
        isn't knowable yet): an unscoped run still drops. CREATE gates on
        completion (#365): a repo skipped via advisory lock in the index
        phase must not be counted, so create_heavy_indexes must not run."""
        mock_create, mock_drop = self._run(
            None, ["owner/repo1", "owner/repo2"], lock_fails_for={"owner/repo2"}
        )
        assert mock_drop.called
        mock_create.assert_not_called()


# ===================================================================
# Bulk-path heavy index drop/create gating at the pipeline sync mirror
# (issues #364/#365 -- shiori.pipeline._do_sync duplicates ingest.py's
# gating rather than importing it, see the comment near pipeline.py:55)
# ===================================================================


class TestDoSyncBulkHeavyIndexGates:
    def _mock_conn_cm(self):
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        return mock_conn

    def _run(self, repos_arg, configured, lock_fails_for=None):
        from shiori.pipeline import _do_sync

        lock_fails_for = lock_fails_for or set()

        def _lock(conn, repo):
            return repo not in lock_fails_for

        mock_conn = self._mock_conn_cm()

        with (
            patch("shiori.pipeline.settings") as mock_settings,
            patch("shiori.pipeline._sync_lock") as mock_lock,
            patch("shiori.pipeline.build_token_provider", return_value=MagicMock()),
            patch("shiori.pipeline._get_embedder", return_value=MagicMock()),
            patch("shiori.pipeline._conn", return_value=mock_conn),
            patch("shiori.pipeline._is_bulk_path", return_value=True),
            patch("shiori.pipeline.schema.migrate_light"),
            patch("shiori.pipeline.schema.drop_heavy_indexes") as mock_drop,
            patch("shiori.pipeline.schema.create_heavy_indexes") as mock_create,
            patch("shiori.pipeline.ChunkBuffer", return_value=MagicMock()),
            patch("shiori.pipeline._acquire_repo_lock", side_effect=_lock),
            patch("shiori.pipeline._release_repo_lock"),
            patch("shiori.pipeline.sync_docs", return_value=1),
            patch("shiori.pipeline.sync_issues", return_value=2),
            patch("shiori.pipeline.sync_code", return_value=3),
            patch(
                "shiori.pipeline.db.record_sync_run",
                return_value=MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00"),
            ),
            patch("shiori.pipeline.db.record_sync_attempt"),
        ):
            mock_settings.repos = configured
            mock_settings.fetch_concurrency = 4
            mock_lock.acquire.return_value = True

            _do_sync(repos=repos_arg)
        return mock_create, mock_drop

    def test_scoped_bulk_sync_never_drops_or_creates(self):
        mock_create, mock_drop = self._run(
            ["owner/repo1"], ["owner/repo1", "owner/repo2"]
        )
        mock_drop.assert_not_called()
        mock_create.assert_not_called()

    def test_unscoped_bulk_sync_drops_and_creates(self):
        mock_create, mock_drop = self._run(None, ["owner/repo1", "owner/repo2"])
        assert mock_drop.called
        assert mock_create.called

    def test_lock_skip_drops_but_defers_create(self):
        mock_create, mock_drop = self._run(
            None, ["owner/repo1", "owner/repo2"], lock_fails_for={"owner/repo2"}
        )
        assert mock_drop.called
        mock_create.assert_not_called()
