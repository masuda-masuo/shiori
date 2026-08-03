"""Regression tests for issue #409: CLI ingest completion must advance
``repo_index_state.indexed_head``.

The pull-sync path (pipeline.py) has always written ``indexed_head`` after a
successful sync; the two CLI ingest completion paths (``run_index`` and the
sequential index loop inside ``run_ingest``) recorded the run but never
wrote it, so ``shiori_status`` kept reporting the index stale -- or
never-indexed -- on hosts maintained only by the CLI ingest runner.

These tests pin the observable contract:

- ``run_index`` with zero pending items  -> ``indexed_head`` = "docs" cursor
- ``run_ingest`` sequential loop, same   -> ``indexed_head`` = "docs" cursor
- budget-truncated repo (pending > 0)    -> ``indexed_head`` NOT advanced
- ``run_fetch`` / ``run_ingest`` fetch   -> ``clone_head`` = fetched HEAD

Mock-based, following the harness style of test_ingest_budget.py.  Patches
are entered via ``ExitStack`` (a plain ``with`` of this many mocks trips
CPython's statically-nested-blocks limit).
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from shiori.ingest import run_fetch, run_index, run_ingest

DOCS_HEAD = "abc123def456"


def _mock_conn() -> MagicMock:
    """Connection whose cursor() context manager yields a stub cursor."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []  # chunks summary
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_conn.cursor.return_value.__enter__.side_effect = None
    return mock_conn


def _mock_settings(repos: list[str]) -> MagicMock:
    settings = MagicMock()
    settings.repos = repos
    settings.dev_repos = set()
    settings.fetch_concurrency = 4
    return settings


def _docs_cursor_side(conn, repo, kind):
    """get_cursor stand-in: only the "docs" cursor is set (the others are not)."""
    return DOCS_HEAD if kind == "docs" else None


def _enter_patches(stack: ExitStack, spec: dict[str, tuple[str, dict]]) -> dict[str, MagicMock]:
    """Enter ``patch(target, **kwargs)`` for each ``name -> (target, kwargs)``
    entry; returns ``name -> mock``.  Avoids the nested-``with`` block limit."""
    mocks: dict[str, MagicMock] = {}
    for name, (target, kwargs) in spec.items():
        mocks[name] = stack.enter_context(patch(target, **kwargs))
    return mocks


# ===================================================================
# db.advance_indexed_head: the shared completion write
# ===================================================================


class TestAdvanceIndexedHeadHelper:
    """``db.advance_indexed_head`` -- single write point for all three
    completion paths (pipeline pull-sync + both CLI ingest sites)."""

    def test_upserts_docs_cursor_when_set(self):
        from shiori.db import advance_indexed_head

        mock_conn = MagicMock()
        with (
            patch("shiori.db.get_cursor", return_value=DOCS_HEAD) as mock_get,
            patch("shiori.db.upsert_indexed_head") as mock_upsert,
        ):
            advance_indexed_head(mock_conn, "o/r")

        mock_get.assert_called_once_with(mock_conn, "o/r", "docs")
        mock_upsert.assert_called_once_with(mock_conn, "o/r", DOCS_HEAD)

    def test_noop_when_docs_cursor_missing(self):
        """A repo with nothing indexed yet must not claim an indexed head."""
        from shiori.db import advance_indexed_head

        mock_conn = MagicMock()
        with (
            patch("shiori.db.get_cursor", return_value=None),
            patch("shiori.db.upsert_indexed_head") as mock_upsert,
        ):
            advance_indexed_head(mock_conn, "o/r")

        mock_upsert.assert_not_called()


# ===================================================================
# run_index: indexed_head advancement at completion
# ===================================================================


class TestRunIndexAdvancesIndexedHead:
    """``run_index``: zero pending items -> ``indexed_head`` = docs cursor;
    budget-truncated repo -> never advanced."""

    def _run(self, pending_counts, budget_exhausted=(), bulk=False):
        mock_conn = _mock_conn()
        settings = _mock_settings(["o/a", "o/b"])

        class _FakeBudget:
            budget_seconds = 1.0

            def __init__(self, stop_before):
                self._stop_before = stop_before

            def exhausted(self):
                return repo in self._stop_before

        budget = _FakeBudget(budget_exhausted)
        repo = "o/a"  # referenced by _FakeBudget.exhausted's closure

        with ExitStack() as stack:
            m = _enter_patches(stack, {
                "connect": ("shiori.ingest.db.connect", {"return_value": mock_conn}),
                "migrate": ("shiori.ingest.schema.migrate", {}),
                "migrate_light": ("shiori.ingest.schema.migrate_light", {}),
                "is_bulk": ("shiori.ingest._is_bulk_path", {"return_value": bulk}),
                "covers_all": ("shiori.ingest._bulk_covers_all_repos", {"return_value": True}),
                "completed_all": (
                    "shiori.ingest._bulk_run_completed_all_repos",
                    {"side_effect": lambda completed, s: set(completed) == set(s.repos)},
                ),
                "drop_heavy": ("shiori.ingest.schema.drop_heavy_indexes", {}),
                "create_heavy": ("shiori.ingest.schema.create_heavy_indexes", {}),
                "lock_acquire": ("shiori.ingest._acquire_repo_lock", {"return_value": True}),
                "lock_release": ("shiori.ingest._release_repo_lock", {}),
                "embedder": ("shiori.ingest.Embedder", {"return_value": MagicMock()}),
                "index_docs": ("shiori.ingest.index_docs", {"return_value": 1}),
                "index_issues": ("shiori.ingest.index_issues", {"return_value": 2}),
                "index_code": ("shiori.ingest.index_code", {"return_value": 3}),
                "budget": ("shiori.ingest._index_budget", {"return_value": budget}),
                "count_pending": (
                    "shiori.ingest.db.count_pending_issue_items",
                    {"side_effect": lambda conn, r: pending_counts.get(r, 0)},
                ),
                "count_pending_for_repos": (
                    "shiori.ingest.db.count_pending_issue_items_for_repos",
                    {"return_value": 0},
                ),
                "record_run": ("shiori.ingest.db.record_sync_run", {}),
                "record_progress": ("shiori.ingest.db.record_sync_progress", {}),
                "record_attempt": ("shiori.ingest.db.record_sync_attempt", {}),
                "get_cursor": ("shiori.ingest.db.get_cursor", {"side_effect": _docs_cursor_side}),
                "upsert": ("shiori.ingest.db.upsert_indexed_head", {}),
            })
            run_index(settings=settings)

        return {"run": m["record_run"], "upsert": m["upsert"]}

    def test_completed_repo_advances_indexed_head_to_docs_cursor(self):
        """Zero pending items for every repo -> indexed_head == docs cursor
        for each of them (regression: was never written by run_index)."""
        mocks = self._run(pending_counts={"o/a": 0, "o/b": 0})

        upsert = mocks["upsert"]
        assert upsert.call_count == 2
        advanced = {call.args[1]: call.args[2] for call in upsert.call_args_list}
        assert advanced == {"o/a": DOCS_HEAD, "o/b": DOCS_HEAD}
        # every write went through the shared helper with the working conn
        assert all(call.args[0] is not None for call in upsert.call_args_list)

    def test_truncated_repo_does_not_advance_indexed_head(self):
        """pending > 0 -> that repo's indexed_head must NOT move; only the
        fully-indexed repo advances."""
        mocks = self._run(pending_counts={"o/a": 500, "o/b": 0})

        upsert = mocks["upsert"]
        assert upsert.call_count == 1
        assert upsert.call_args.args[1] == "o/b"
        assert upsert.call_args.args[2] == DOCS_HEAD


# ===================================================================
# run_ingest: sequential index loop advances indexed_head
# ===================================================================


class TestRunIngestAdvancesIndexedHead:
    """``run_ingest`` (combined fetch + index, route=cli): the sequential
    index loop must advance ``indexed_head`` exactly like ``run_index``."""

    def _run(self, pending_counts):
        mock_conn = _mock_conn()
        settings = _mock_settings(["o/a", "o/b"])

        with ExitStack() as stack:
            m = _enter_patches(stack, {
                "connect": ("shiori.ingest.db.connect", {"return_value": mock_conn}),
                "migrate": ("shiori.ingest.schema.migrate", {}),
                "migrate_light": ("shiori.ingest.schema.migrate_light", {}),
                "is_bulk": ("shiori.ingest._is_bulk_path", {"return_value": False}),
                "should_skip": ("shiori.ingest._should_skip_repo", {"return_value": False}),
                "lock_acquire": ("shiori.ingest._acquire_repo_lock", {"return_value": True}),
                "lock_release": ("shiori.ingest._release_repo_lock", {}),
                "token_provider": ("shiori.ingest.build_token_provider", {"return_value": MagicMock()}),
                "embedder": ("shiori.ingest.Embedder", {"return_value": MagicMock()}),
                "fetch_docs": ("shiori.ingest.fetch_docs", {"return_value": DOCS_HEAD}),
                "fetch_issues": ("shiori.ingest.fetch_issues", {"return_value": 5}),
                "index_docs": ("shiori.ingest.index_docs", {"return_value": 1}),
                "index_issues": ("shiori.ingest.index_issues", {"return_value": 2}),
                "index_code": ("shiori.ingest.index_code", {"return_value": 3}),
                "record_run": (
                    "shiori.ingest.db.record_sync_run",
                    {"return_value": MagicMock(
                        isoformat=lambda: "2026-01-01T00:00:00+00:00")},
                ),
                "record_attempt": ("shiori.ingest.db.record_sync_attempt", {}),
                "count_pending": (
                    "shiori.ingest.db.count_pending_issue_items",
                    {"side_effect": lambda conn, r: pending_counts.get(r, 0)},
                ),
                "count_pending_for_repos": (
                    "shiori.ingest.db.count_pending_issue_items_for_repos",
                    {"return_value": 0},
                ),
                "get_cursor": ("shiori.ingest.db.get_cursor", {"side_effect": _docs_cursor_side}),
                "upsert": ("shiori.ingest.db.upsert_indexed_head", {}),
                "upsert_clone": ("shiori.ingest.db.upsert_clone_head", {}),
            })
            run_ingest(settings=settings)

        return {"upsert": m["upsert"], "upsert_clone": m["upsert_clone"]}

    def test_completed_repo_advances_indexed_head_to_docs_cursor(self):
        """Zero pending items after the sequential index loop ->
        indexed_head == docs cursor for each completed repo."""
        mocks = self._run(pending_counts={"o/a": 0, "o/b": 0})

        upsert = mocks["upsert"]
        assert upsert.call_count == 2
        advanced = {call.args[1]: call.args[2] for call in upsert.call_args_list}
        assert advanced == {"o/a": DOCS_HEAD, "o/b": DOCS_HEAD}

    def test_truncated_repo_does_not_advance_indexed_head(self):
        """pending > 0 -> indexed_head not advanced for that repo."""
        mocks = self._run(pending_counts={"o/a": 500, "o/b": 0})

        upsert = mocks["upsert"]
        assert upsert.call_count == 1
        assert upsert.call_args.args[1] == "o/b"
        assert upsert.call_args.args[2] == DOCS_HEAD

    def test_fetch_phase_records_clone_head(self):
        """The combined run's fetch phase records clone_head for every
        fetched repo (issue #409 residual: the CLI fetch advances the
        on-disk clone, so clone_head must move with it or status compares
        indexed_head against the pre-fetch SHA)."""
        mocks = self._run(pending_counts={"o/a": 0, "o/b": 0})

        upsert_clone = mocks["upsert_clone"]
        assert upsert_clone.call_count == 2
        recorded = {
            call.args[1]: call.args[2] for call in upsert_clone.call_args_list
        }
        assert recorded == {"o/a": DOCS_HEAD, "o/b": DOCS_HEAD}


# ===================================================================
# run_fetch: clone_head recorded on successful fetch (issue #409)
# ===================================================================


class TestRunFetchRecordsCloneHead:
    """``run_fetch`` (fetch-only CLI lane) must record the fetched clone
    HEAD as ``clone_head`` -- the same Phase-1 write the pull-sync path
    makes -- so a CLI-fetched repo does not look stale in ``shiori_status``
    until some other lane happens to refresh ``clone_head``."""

    def _run(self, fetch_docs_result):
        mock_conn = MagicMock()
        settings = MagicMock()
        settings.repos = ["o/a", "o/b"]
        settings.dev_repos = set()
        settings.fetch_concurrency = 4

        with (
            patch("shiori.ingest.db.connect", return_value=mock_conn),
            patch("shiori.ingest.schema.migrate_light"),
            patch("shiori.ingest._acquire_repo_lock", return_value=True),
            patch("shiori.ingest._release_repo_lock"),
            patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
            patch("shiori.ingest.fetch_docs", return_value=fetch_docs_result),
            patch("shiori.ingest.fetch_issues", return_value=5),
            patch("shiori.ingest.db.upsert_clone_head") as mock_upsert_clone,
        ):
            run_fetch(settings=settings)

        return {"upsert_clone": mock_upsert_clone}

    def test_successful_fetch_records_clone_head(self):
        """fetch_docs returning a HEAD -> clone_head recorded per repo."""
        mocks = self._run("abc123")

        upsert_clone = mocks["upsert_clone"]
        assert upsert_clone.call_count == 2
        recorded = {
            call.args[1]: call.args[2] for call in upsert_clone.call_args_list
        }
        assert recorded == {"o/a": "abc123", "o/b": "abc123"}

    def test_failed_refresh_does_not_record_clone_head(self):
        """fetch_docs returning None (clone refresh failed) -> no
        clone_head write; the old value stays authoritative."""
        mocks = self._run(None)

        mocks["upsert_clone"].assert_not_called()
