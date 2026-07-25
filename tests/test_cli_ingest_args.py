"""Unit tests for ingest CLI argument parsing (issue #348).

Covers every row of the required dispatch table and every exit-2 case.
Does not touch Postgres, the network, or the embedding model.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from shiori.__main__ import main


# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture
def mock_ingest_funcs():
    """Patch the three ingest entry points so they never hit the DB."""
    with (
        patch("shiori.ingest.run_fetch") as mf,
        patch("shiori.ingest.run_index") as mi,
        patch("shiori.ingest.run_ingest") as mr,
    ):
        yield mf, mi, mr


def _run(argv: list[str]) -> None:
    """Invoke main() with a patched sys.argv."""
    with patch.object(sys, "argv", ["shiori"] + argv):
        main()


# ===================================================================
# Exit-2 (no --repo anywhere)
# ===================================================================


class TestMissingRepo:
    """Invocation that names no repository must exit 2."""

    def test_ingest_no_subcommand(self):
        """shiori ingest → exit 2, mentions --repo."""
        with pytest.raises(SystemExit) as exc:
            _run(["ingest"])
        assert exc.value.code == 2

    def test_ingest_fetch(self):
        """shiori ingest fetch → exit 2."""
        with pytest.raises(SystemExit) as exc:
            _run(["ingest", "fetch"])
        assert exc.value.code == 2

    def test_ingest_index(self):
        """shiori ingest index → exit 2."""
        with pytest.raises(SystemExit) as exc:
            _run(["ingest", "index"])
        assert exc.value.code == 2

    def test_ingest_run(self):
        """shiori ingest run → exit 2."""
        with pytest.raises(SystemExit) as exc:
            _run(["ingest", "run"])
        assert exc.value.code == 2


# ===================================================================
# Dispatch table — every row of the required behaviour table
# ===================================================================


class TestDispatchTable:
    """Every required dispatch pattern."""

    # ── shiori ingest --repo a/b → run_ingest ──────────────────────────

    def test_ingest_repo_runs_ingest(self, mock_ingest_funcs):
        """shiori ingest --repo a/b → run_ingest(repos=["a/b"])"""
        _, _, run_ingest = mock_ingest_funcs
        _run(["ingest", "--repo", "a/b"])
        run_ingest.assert_called_once_with(
            repos=["a/b"], rebuild=False, backfill_since=None
        )

    # ── shiori ingest fetch --repo a/b → run_fetch ─────────────────────

    def test_fetch_repo_runs_fetch(self, mock_ingest_funcs):
        """shiori ingest fetch --repo a/b → run_fetch(repos=["a/b"])"""
        run_fetch, _, _ = mock_ingest_funcs
        _run(["ingest", "fetch", "--repo", "a/b"])
        run_fetch.assert_called_once_with(repos=["a/b"], backfill_since=None)

    # ── shiori ingest index --repo a/b → run_index ─────────────────────

    def test_index_repo_runs_index(self, mock_ingest_funcs):
        """shiori ingest index --repo a/b → run_index(repos=["a/b"])"""
        _, run_index, _ = mock_ingest_funcs
        _run(["ingest", "index", "--repo", "a/b"])
        run_index.assert_called_once_with(repos=["a/b"], rebuild=False)

    # ── shiori ingest run --repo a/b → run_ingest ──────────────────────

    def test_run_repo_runs_ingest(self, mock_ingest_funcs):
        """shiori ingest run --repo a/b → run_ingest(repos=["a/b"])"""
        _, _, run_ingest = mock_ingest_funcs
        _run(["ingest", "run", "--repo", "a/b"])
        run_ingest.assert_called_once_with(
            repos=["a/b"], rebuild=False, backfill_since=None
        )

    # ── shiori ingest --repo a/b fetch → run_fetch (parent --repo) ────

    def test_parent_repo_fetch(self, mock_ingest_funcs):
        """shiori ingest --repo a/b fetch → run_fetch(repos=["a/b"])"""
        run_fetch, _, _ = mock_ingest_funcs
        _run(["ingest", "--repo", "a/b", "fetch"])
        run_fetch.assert_called_once_with(repos=["a/b"], backfill_since=None)

    # ── shiori ingest --repo a/b --repo c/d fetch → run_fetch (multiple parent) ──

    def test_parent_multirepo_fetch(self, mock_ingest_funcs):
        """shiori ingest --repo a/b --repo c/d fetch → run_fetch(repos=["a/b","c/d"])"""
        run_fetch, _, _ = mock_ingest_funcs
        _run(["ingest", "--repo", "a/b", "--repo", "c/d", "fetch"])
        run_fetch.assert_called_once_with(
            repos=["a/b", "c/d"], backfill_since=None
        )

    # ── shiori ingest fetch --repo a/b --repo c/d → run_fetch (multiple on sub) ──

    def test_sub_multirepo_fetch(self, mock_ingest_funcs):
        """shiori ingest fetch --repo a/b --repo c/d → run_fetch(repos=["a/b","c/d"])"""
        run_fetch, _, _ = mock_ingest_funcs
        _run(["ingest", "fetch", "--repo", "a/b", "--repo", "c/d"])
        run_fetch.assert_called_once_with(
            repos=["a/b", "c/d"], backfill_since=None
        )


# ===================================================================
# Flag survival
# ===================================================================


class TestFlagSurvival:
    """Flags must survive across parent/subcommand boundary."""

    # ── --backfill-since with fetch ──────────────────────────────────

    def test_backfill_since_on_parent_fetch(self, mock_ingest_funcs):
        """--backfill-since on parent + --repo on sub → run_fetch gets backfill_since"""
        run_fetch, _, _ = mock_ingest_funcs
        _run(
            [
                "ingest",
                "--backfill-since",
                "2024-01-01",
                "fetch",
                "--repo",
                "a/b",
            ]
        )
        run_fetch.assert_called_once_with(
            repos=["a/b"], backfill_since="2024-01-01"
        )

    def test_backfill_since_on_sub_fetch(self, mock_ingest_funcs):
        """--backfill-since on sub (fetch) → run_fetch gets backfill_since"""
        run_fetch, _, _ = mock_ingest_funcs
        _run(
            [
                "ingest",
                "fetch",
                "--backfill-since",
                "2024-01-01",
                "--repo",
                "a/b",
            ]
        )
        run_fetch.assert_called_once_with(
            repos=["a/b"], backfill_since="2024-01-01"
        )

    # ── --rebuild with run / no-subcommand ───────────────────────────

    def test_rebuild_on_parent_no_sub(self, mock_ingest_funcs):
        """shiori ingest --rebuild --repo a/b → run_ingest(rebuild=True)"""
        _, _, run_ingest = mock_ingest_funcs
        _run(["ingest", "--rebuild", "--repo", "a/b"])
        run_ingest.assert_called_once_with(
            repos=["a/b"], rebuild=True, backfill_since=None
        )

    def test_rebuild_on_run_subcommand(self, mock_ingest_funcs):
        """shiori ingest run --rebuild --repo a/b → run_ingest(rebuild=True)"""
        _, _, run_ingest = mock_ingest_funcs
        _run(["ingest", "run", "--rebuild", "--repo", "a/b"])
        run_ingest.assert_called_once_with(
            repos=["a/b"], rebuild=True, backfill_since=None
        )

    # ── Defaults when flags omitted ─────────────────────────────────

    def test_backfill_since_default(self, mock_ingest_funcs):
        """fetch without --backfill-since → backfill_since=None"""
        run_fetch, _, _ = mock_ingest_funcs
        _run(["ingest", "fetch", "--repo", "x/y"])
        run_fetch.assert_called_once_with(repos=["x/y"], backfill_since=None)

    def test_rebuild_default(self, mock_ingest_funcs):
        """index without --rebuild → rebuild=False"""
        _, run_index, _ = mock_ingest_funcs
        _run(["ingest", "index", "--repo", "x/y"])
        run_index.assert_called_once_with(repos=["x/y"], rebuild=False)

    def test_all_defaults_no_sub(self, mock_ingest_funcs):
        """ingest --repo x/y (no flags) → all defaults are falsy/None"""
        _, _, run_ingest = mock_ingest_funcs
        _run(["ingest", "--repo", "x/y"])
        run_ingest.assert_called_once_with(
            repos=["x/y"], rebuild=False, backfill_since=None
        )


# ===================================================================
# Safety guard: issue #338 intent preserved
# ===================================================================


class TestNoRepoGuard:
    """issue #338: no --repo anywhere must exit 2, not run over all configured repos."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["ingest"], id="just-ingest"),
            pytest.param(["ingest", "fetch"], id="ingest-fetch"),
            pytest.param(["ingest", "index"], id="ingest-index"),
            pytest.param(["ingest", "run"], id="ingest-run"),
            pytest.param(["ingest", "--rebuild"], id="ingest-rebuild-only"),
            pytest.param(
                ["ingest", "--backfill-since", "2024-01-01"],
                id="ingest-backfill-only",
            ),
            pytest.param(
                ["ingest", "run", "--rebuild"],
                id="ingest-run-rebuild-no-repo",
            ),
        ],
    )
    def test_exit_2(self, argv):
        with pytest.raises(SystemExit) as exc:
            _run(argv)
        assert exc.value.code == 2
