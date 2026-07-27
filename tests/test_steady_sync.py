"""Tests for shiori#347 (steady sync): CLI role selectors (--only-dev /
--only-ref) and role-aware shiori_status staleness thresholds.

No PostgreSQL: CLI dispatch tests mock shiori.ingest.run_* (same pattern as
tests/test_cli_ingest_args.py and tests/test_reindex.py
TestCliReindexDispatch); status tests mock at the connection/cursor
boundary (same pattern as tests/test_pull_sync.py).
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from shiori.__main__ import main
from shiori.config import Settings
from shiori.tools.status import _stale_threshold_seconds, status


def _run(argv: list[str]) -> None:
    """Invoke main() with a patched sys.argv."""
    with patch.object(sys, "argv", ["shiori"] + argv):
        main()


def _settings(repos, dev_repos=()):
    s = Settings()
    s.repos = list(repos)
    s.dev_repos = set(dev_repos)
    return s


# ===================================================================
# CLI: --only-dev / --only-ref resolution
# ===================================================================


class TestOnlyDevOnlyRefResolution:
    """--only-dev / --only-ref resolve to the configured dev/ref repos, in
    SHIORI_REPOS order (issue #347)."""

    def test_only_dev_resolves_to_dev_repos_in_order(self):
        settings = _settings(
            ["o/ref1", "o/dev1", "o/dev2", "o/ref2"], dev_repos=["o/dev1", "o/dev2"]
        )
        with (
            patch("shiori.config.load_settings", return_value=settings),
            patch("shiori.ingest.run_ingest") as mock_run_ingest,
        ):
            _run(["ingest", "run", "--only-dev"])
        mock_run_ingest.assert_called_once_with(
            repos=["o/dev1", "o/dev2"], rebuild=False, backfill_since=None
        )

    def test_only_ref_resolves_to_complement_in_order(self):
        settings = _settings(
            ["o/ref1", "o/dev1", "o/dev2", "o/ref2"], dev_repos=["o/dev1", "o/dev2"]
        )
        with (
            patch("shiori.config.load_settings", return_value=settings),
            patch("shiori.ingest.run_ingest") as mock_run_ingest,
        ):
            _run(["ingest", "run", "--only-ref"])
        mock_run_ingest.assert_called_once_with(
            repos=["o/ref1", "o/ref2"], rebuild=False, backfill_since=None
        )

    def test_only_dev_on_fetch(self):
        settings = _settings(["o/dev1", "o/ref1"], dev_repos=["o/dev1"])
        with (
            patch("shiori.config.load_settings", return_value=settings),
            patch("shiori.ingest.run_fetch") as mock_run_fetch,
        ):
            _run(["ingest", "fetch", "--only-dev"])
        mock_run_fetch.assert_called_once_with(repos=["o/dev1"], backfill_since=None)

    def test_only_ref_on_index(self):
        settings = _settings(["o/dev1", "o/ref1"], dev_repos=["o/dev1"])
        with (
            patch("shiori.config.load_settings", return_value=settings),
            patch("shiori.ingest.run_index") as mock_run_index,
        ):
            _run(["ingest", "index", "--only-ref"])
        mock_run_index.assert_called_once_with(repos=["o/ref1"], rebuild=False)

    def test_only_dev_empty_selection_exits_2(self):
        """No dev repos configured -> exit 2, not a silent empty run."""
        settings = _settings(["o/ref1", "o/ref2"], dev_repos=[])
        with patch("shiori.config.load_settings", return_value=settings):
            with pytest.raises(SystemExit) as exc:
                _run(["ingest", "run", "--only-dev"])
        assert exc.value.code == 2

    def test_only_ref_empty_selection_exits_2(self):
        """All configured repos are dev repos -> --only-ref has nothing to
        select -> exit 2."""
        settings = _settings(["o/dev1", "o/dev2"], dev_repos=["o/dev1", "o/dev2"])
        with patch("shiori.config.load_settings", return_value=settings):
            with pytest.raises(SystemExit) as exc:
                _run(["ingest", "run", "--only-ref"])
        assert exc.value.code == 2


# ===================================================================
# CLI: mutual exclusivity (exit 2)
# ===================================================================


class TestOnlyDevOnlyRefMutualExclusion:
    def test_only_dev_and_repo_exit_2(self):
        with pytest.raises(SystemExit) as exc:
            _run(["ingest", "run", "--only-dev", "--repo", "a/b"])
        assert exc.value.code == 2

    def test_only_ref_and_repo_exit_2(self):
        with pytest.raises(SystemExit) as exc:
            _run(["ingest", "fetch", "--only-ref", "--repo", "a/b"])
        assert exc.value.code == 2

    def test_only_dev_and_only_ref_exit_2(self):
        with pytest.raises(SystemExit) as exc:
            _run(["ingest", "run", "--only-dev", "--only-ref"])
        assert exc.value.code == 2

    def test_index_all_and_only_dev_exit_2(self):
        with pytest.raises(SystemExit) as exc:
            _run(["ingest", "index", "--all", "--only-dev"])
        assert exc.value.code == 2

    def test_index_all_and_only_ref_exit_2(self):
        with pytest.raises(SystemExit) as exc:
            _run(["ingest", "index", "--all", "--only-ref"])
        assert exc.value.code == 2


# ===================================================================
# shiori_status: role-aware staleness threshold (issue #347)
# ===================================================================


class TestStaleThresholdSecondsRoles:
    def test_dev_role_uses_dev_interval(self):
        with patch("shiori.tools.status.settings") as mock_settings:
            mock_settings.dev_repos = {"o/dev"}
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 86400
            assert _stale_threshold_seconds("o/dev") == 1800

    def test_ref_role_uses_ref_interval(self):
        with patch("shiori.tools.status.settings") as mock_settings:
            mock_settings.dev_repos = {"o/dev"}
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 86400
            assert _stale_threshold_seconds("o/ref") == 172800

    def test_env_override_dev(self):
        with patch("shiori.tools.status.settings") as mock_settings:
            mock_settings.dev_repos = {"o/dev"}
            mock_settings.dev_sync_interval_seconds = 120
            mock_settings.ref_sync_interval_seconds = 86400
            assert _stale_threshold_seconds("o/dev") == 300  # floored (2*120=240)

    def test_env_override_ref(self):
        with patch("shiori.tools.status.settings") as mock_settings:
            mock_settings.dev_repos = set()
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 50000
            assert _stale_threshold_seconds("o/ref") == 100000


class TestStatusPayloadShape:
    """shiori_status payload carries the new role-aware fields and drops the
    old fictional top-level sync_interval_seconds key (issue #347)."""

    def _run_status(self):
        with (
            patch("shiori.tools.status._conn"),
            patch("shiori.tools.status.settings") as mock_settings,
            patch("shiori.tools.status.db.get_sync_runs", return_value={}),
            patch("shiori.tools.status.db.get_all_repo_index_state", return_value={}),
            patch("shiori.tools.status.db.get_chunk_counts", return_value={}),
            patch("shiori.tools.status.db.get_issue_item_count", return_value=0),
            patch("shiori.tools.status.db.get_cursors", return_value={}),
        ):
            mock_settings.repos = ["o/dev", "o/ref"]
            mock_settings.dev_repos = {"o/dev"}
            mock_settings.sync_interval_seconds = 5
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 86400
            return status()

    def test_old_top_level_key_is_gone(self):
        result = self._run_status()
        assert "sync_interval_seconds" not in result

    def test_new_top_level_keys(self):
        result = self._run_status()
        assert result["clone_refresh_debounce_seconds"] == 5
        assert result["sync_intervals"] == {"dev": 900, "ref": 86400}

    def test_per_repo_expected_interval(self):
        result = self._run_status()
        assert result["repos"]["o/dev"]["expected_sync_interval_seconds"] == 900
        assert result["repos"]["o/ref"]["expected_sync_interval_seconds"] == 86400

    def test_per_repo_role(self):
        result = self._run_status()
        assert result["repos"]["o/dev"]["role"] == "dev"
        assert result["repos"]["o/ref"]["role"] == "ref"


class TestStatusAgeStalenessEndToEnd:
    """The full status() -> _stale_threshold_seconds(repo) -> _build_warnings
    pipeline with realistic sync_run data (reviewer finding: each piece was
    unit-tested but the wiring between them was not).

    dev interval 900 -> threshold 1800. The 1500s case guards the x2
    multiplier itself: with a x1 formula it would start warning.
    """

    def _run_status(self, age_seconds):
        with (
            patch("shiori.tools.status._conn"),
            patch("shiori.tools.status.settings") as mock_settings,
            patch("shiori.tools.status.db.get_sync_runs",
                  return_value={"o/dev": {"age_seconds": age_seconds}}),
            patch("shiori.tools.status.db.get_all_repo_index_state", return_value={}),
            patch("shiori.tools.status.db.get_chunk_counts", return_value={}),
            patch("shiori.tools.status.db.get_issue_item_count", return_value=0),
            patch("shiori.tools.status.db.get_cursors",
                  return_value={"docs": "x", "issues": "x", "issue_comments": "x",
                                "pr_review_comments": "x"}),
        ):
            mock_settings.repos = ["o/dev"]
            mock_settings.dev_repos = {"o/dev"}
            mock_settings.sync_interval_seconds = 5
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 86400
            return status()

    def test_warns_above_role_derived_threshold(self):
        result = self._run_status(age_seconds=2000)
        warnings = result["repos"]["o/dev"]["warnings"]
        assert any("hours since last sync" in w for w in warnings)

    def test_no_warning_between_interval_and_threshold(self):
        result = self._run_status(age_seconds=1500)
        warnings = result["repos"]["o/dev"]["warnings"]
        assert not any("hours since last sync" in w for w in warnings)
