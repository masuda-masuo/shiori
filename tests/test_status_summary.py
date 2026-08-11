"""shiori_status: aggregate summary as the no-repo default (issue #423).

The all-repos response used to carry a full record per repo -- 53,836 chars
on the live 62-repo deployment, which does not fit an MCP client context
window, while 91% of real calls omit ``repo``.  The no-repo call now returns
a ``summary`` plus full records for unhealthy repos only.

No Postgres here: every DB accessor is patched, following the technique in
tests/test_build_warnings.py and the existing status() tests.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from shiori.tools.status import (
    _NO_REPO_REPOS_CHAR_BUDGET,
    _is_healthy,
    _no_repo_response,
    _summarize,
    status,
)

# A repo with all four cursors present, fresh, no failures and heads in sync
# is healthy -- anything less trips one of the _build_warnings rules.
_ALL_CURSORS = {
    "docs": "abc",
    "issues": "2026-01-01",
    "issue_comments": "2026-01-01",
    "pr_review_comments": "2026-01-01",
}


@contextmanager
def _patched_status(
    repos,
    *,
    dev_repos=frozenset(),
    sync_runs=None,
    index_state=None,
    cursors=None,
    chunk_counts=None,
    items_in_db=0,
):
    """Run status() against fully mocked DB accessors.

    ``sync_runs`` / ``index_state`` are per-repo dicts; ``cursors`` may be a
    single dict shared by every repo or a per-repo mapping.
    """
    provider = MagicMock()
    provider.name = "static"

    def _cursors_for(_conn_arg, target_repo):
        if cursors is None:
            return dict(_ALL_CURSORS)
        return cursors.get(target_repo, dict(_ALL_CURSORS))

    with (
        patch("shiori.tools.status._conn"),
        patch("shiori.tools.status.settings") as mock_settings,
        patch("shiori.tools.status.build_token_provider", return_value=provider),
        patch("shiori.tools.status.db.get_sync_runs", return_value=sync_runs or {}),
        patch(
            "shiori.tools.status.db.get_all_repo_index_state",
            return_value=index_state or {},
        ),
        patch("shiori.tools.status.db.get_sync_run", side_effect=
              lambda _c, r: (sync_runs or {}).get(r)),
        patch("shiori.tools.status.db.get_repo_index_state", side_effect=
              lambda _c, r: (index_state or {}).get(r, {})),
        patch(
            "shiori.tools.status.db.get_chunk_counts",
            return_value=chunk_counts if chunk_counts is not None else {},
        ),
        patch("shiori.tools.status.db.get_issue_item_count", return_value=items_in_db),
        patch("shiori.tools.status.db.get_cursors", side_effect=_cursors_for),
        patch("shiori.tools.status._validate_repo_name", side_effect=lambda r: r),
    ):
        mock_settings.repos = list(repos)
        mock_settings.dev_repos = set(dev_repos)
        mock_settings.sync_interval_seconds = 300
        mock_settings.dev_sync_interval_seconds = 900
        mock_settings.ref_sync_interval_seconds = 86400
        yield


def _sixty_two_repos():
    return [f"owner/repo{i:02d}" for i in range(62)]


def _healthy_run(age_seconds=100):
    return {
        "last_synced_at": "2026-08-12T00:00:00+00:00",
        "age_seconds": age_seconds,
        "route": "cli",
        "docs_updated": 3,
        "issues_indexed": 4,
        "code_added": 5,
        "last_attempt_at": "2026-08-12T00:00:00+00:00",
        "last_error": None,
        "consecutive_failures": 0,
        "pending_count": 0,
        "last_progress_at": None,
    }


# ── All healthy: small response, empty repos (criterion 1) ──


class TestAllHealthy:
    """62 healthy repos: summary only, and it fits in a context window."""

    def _run(self):
        names = _sixty_two_repos()
        runs = {name: _healthy_run(age_seconds=100 + i) for i, name in enumerate(names)}
        with _patched_status(names, sync_runs=runs):
            return status()

    def test_repos_is_empty(self):
        assert self._run()["repos"] == {}

    def test_serialized_response_is_small(self):
        serialized = json.dumps(self._run())
        assert len(serialized) < 2000, f"{len(serialized)} chars: {serialized[:400]}"

    def test_top_level_keys_are_preserved(self):
        result = self._run()
        for key in (
            "repos",
            "sync_intervals",
            "clone_refresh_debounce_seconds",
            "token_provider",
        ):
            assert key in result
        assert result["sync_intervals"] == {"dev": 900, "ref": 86400}
        assert result["clone_refresh_debounce_seconds"] == 300

    def test_summary_counts_all_healthy(self):
        summary = self._run()["summary"]
        assert summary["total_repos"] == 62
        assert summary["healthy_repos"] == 62
        assert summary["unhealthy_repos"] == 0
        assert summary["pending_total"] == 0

    def test_summary_reports_the_single_oldest_repo(self):
        # ages run 100..161, so the last repo is the oldest.
        oldest = self._run()["summary"]["oldest_repo"]
        assert oldest["repo"] == "owner/repo61"
        assert oldest["age_seconds"] == 161
        assert oldest["role"] == "ref"

    def test_oldest_reports_dev_role_when_the_oldest_is_a_dev_repo(self):
        names = ["o/ref", "o/dev"]
        runs = {"o/ref": _healthy_run(100), "o/dev": _healthy_run(500)}
        with _patched_status(names, dev_repos={"o/dev"}, sync_runs=runs):
            oldest = status()["summary"]["oldest_repo"]
        assert oldest == {"repo": "o/dev", "role": "dev", "age_seconds": 500}

    def test_oldest_is_none_when_no_repo_has_ever_synced(self):
        names = ["o/a", "o/b"]
        with _patched_status(names):
            summary = status()["summary"]
        # No sync_run rows -> age_seconds is None everywhere.  Still healthy:
        # a missing run row is not one of the unhealthy conditions.
        assert summary["oldest_repo"] is None
        assert summary["healthy_repos"] == 2


# ── Mixed: unhealthy repos come through in full (criterion 2) ──


class TestMixedFleet:
    """Unhealthy repos keep every field they have today; healthy ones vanish."""

    def _run_mixed(self):
        names = ["o/healthy", "o/stale", "o/failing", "o/pending", "o/warned"]
        runs = {
            "o/healthy": _healthy_run(),
            "o/stale": _healthy_run(),
            "o/failing": {**_healthy_run(), "consecutive_failures": 3,
                          "last_error": "git fetch failed"},
            "o/pending": {**_healthy_run(), "pending_count": 7},
            "o/warned": _healthy_run(),
        }
        index_state = {"o/stale": {"clone_head": "def456", "indexed_head": "abc123"}}
        cursors = {
            "o/healthy": dict(_ALL_CURSORS),
            "o/stale": dict(_ALL_CURSORS),
            "o/failing": dict(_ALL_CURSORS),
            "o/pending": dict(_ALL_CURSORS),
            # missing categories -> "Unsynced categories" warning
            "o/warned": {"docs": "abc"},
        }
        with _patched_status(
            names, sync_runs=runs, index_state=index_state, cursors=cursors
        ):
            return status()

    def test_only_unhealthy_repos_are_listed(self):
        assert sorted(self._run_mixed()["repos"]) == [
            "o/failing",
            "o/pending",
            "o/stale",
            "o/warned",
        ]

    def test_healthy_repo_is_not_listed(self):
        assert "o/healthy" not in self._run_mixed()["repos"]

    def test_unhealthy_record_keeps_every_field(self):
        """The listed record is byte-for-byte what the repo-specified call returns."""
        listed = self._run_mixed()["repos"]["o/stale"]

        names = ["o/healthy", "o/stale", "o/failing", "o/pending", "o/warned"]
        runs = {"o/stale": _healthy_run()}
        index_state = {"o/stale": {"clone_head": "def456", "indexed_head": "abc123"}}
        with _patched_status(names, sync_runs=runs, index_state=index_state):
            single = status(repo="o/stale")["repos"]["o/stale"]

        assert listed == single
        # Spot-check the contract rather than trusting the comparison alone.
        for key in (
            "last_synced_at", "age_seconds", "route", "docs_updated",
            "issues_indexed", "code_added", "last_attempt_at", "last_error",
            "consecutive_failures", "pending_count", "last_progress_at",
            "clone_head", "indexed_head", "last_sync_error", "index_stale",
            "never_indexed", "chunks", "code_chunks", "items_in_db", "cursors",
            "role", "code_indexed", "expected_sync_interval_seconds", "warnings",
        ):
            assert key in listed, key
        assert "token_provider_error" not in listed

    def test_summary_counts_match_the_per_repo_data(self):
        result = self._run_mixed()
        summary = result["summary"]
        assert summary["total_repos"] == 5
        assert summary["unhealthy_repos"] == 4
        assert summary["healthy_repos"] == 1
        assert summary["unhealthy_repos"] == len(result["repos"])

    def test_condition_counts_match_the_repos_that_carry_them(self):
        result = self._run_mixed()
        counts = result["summary"]["unhealthy_counts"]
        repos = result["repos"]
        assert counts["index_stale"] == sum(
            1 for i in repos.values() if i["index_stale"]
        )
        assert counts["index_stale"] == 1
        assert counts["never_indexed"] == 0
        assert counts["failing"] == 1
        assert repos["o/failing"]["consecutive_failures"] == 3
        assert counts["with_pending"] == 1
        assert repos["o/pending"]["pending_count"] == 7
        assert counts["with_warnings"] == 4

    def test_pending_total_sums_across_all_repos(self):
        assert self._run_mixed()["summary"]["pending_total"] == 7

    def test_pending_total_includes_healthy_repos_and_ignores_none(self):
        names = ["o/a", "o/b", "o/c"]
        runs = {
            "o/a": {**_healthy_run(), "pending_count": 4},
            "o/b": {**_healthy_run(), "pending_count": None},
            # o/c has no sync_run row at all -> pending_count defaults to None
        }
        with _patched_status(names, sync_runs=runs):
            summary = status()["summary"]
        assert summary["pending_total"] == 4
        assert summary["unhealthy_counts"]["with_pending"] == 1

    def test_never_indexed_repo_is_listed_and_counted(self):
        names = ["o/new"]
        index_state = {"o/new": {"clone_head": "abc1234", "indexed_head": None}}
        with _patched_status(names, index_state=index_state):
            result = status()
        assert "o/new" in result["repos"]
        assert result["repos"]["o/new"]["never_indexed"] is True
        assert result["summary"]["unhealthy_counts"]["never_indexed"] == 1
        assert result["summary"]["unhealthy_counts"]["index_stale"] == 1


# ── repo-specified path is untouched (criterion 3) ──


class TestRepoSpecifiedUnchanged:
    """status(repo=...) keeps today's exact object -- no summary, no filtering."""

    def test_no_summary_key(self):
        with _patched_status(["o/r"], sync_runs={"o/r": _healthy_run()}):
            result = status(repo="o/r")
        assert "summary" not in result

    def test_top_level_keys_and_order_unchanged(self):
        with _patched_status(["o/r"], sync_runs={"o/r": _healthy_run()}):
            result = status(repo="o/r")
        assert list(result) == [
            "repos",
            "clone_refresh_debounce_seconds",
            "sync_intervals",
            "token_provider",
        ]

    def test_healthy_repo_is_still_returned_in_full(self):
        """A healthy repo is filtered out of the no-repo call but NOT here."""
        with _patched_status(["o/r"], sync_runs={"o/r": _healthy_run()}):
            result = status(repo="o/r")
        record = result["repos"]["o/r"]
        assert list(result["repos"]) == ["o/r"]
        assert record["warnings"] == []
        assert record["age_seconds"] == 100
        assert record["role"] == "ref"

    def test_unhealthy_repo_is_returned_in_full(self):
        index_state = {"o/r": {"clone_head": "def456", "indexed_head": "abc123"}}
        with _patched_status(
            ["o/r"], sync_runs={"o/r": _healthy_run()}, index_state=index_state
        ):
            result = status(repo="o/r")
        assert result["repos"]["o/r"]["index_stale"] is True
        assert "summary" not in result

    def test_repo_with_no_sync_run_row_keeps_the_default_record(self):
        with _patched_status(["o/r"]):
            result = status(repo="o/r")
        record = result["repos"]["o/r"]
        assert "summary" not in result
        assert list(result["repos"]) == ["o/r"]
        assert record["last_synced_at"] is None
        assert record["age_seconds"] is None
        assert record["route"] is None
        assert record["docs_updated"] is None
        assert record["issues_indexed"] is None
        assert record["code_added"] is None
        assert record["last_attempt_at"] is None
        assert record["last_error"] is None
        assert record["consecutive_failures"] == 0
        assert record["pending_count"] is None
        assert record["last_progress_at"] is None
        assert record["index_stale"] is False
        assert record["never_indexed"] is False


# ── status() still never raises (criterion 5, issue #193) ──


class TestTokenProviderErrorPathSurvivesSummarization:
    def test_no_repo_call_reports_error_and_lists_the_repo(self):
        with (
            patch("shiori.tools.status._conn"),
            patch("shiori.tools.status.settings") as mock_settings,
            patch("shiori.tools.status.db.get_sync_runs",
                  return_value={"o/r": _healthy_run()}),
            patch("shiori.tools.status.db.get_all_repo_index_state", return_value={}),
            patch("shiori.tools.status.db.get_chunk_counts", return_value={}),
            patch("shiori.tools.status.db.get_issue_item_count", return_value=0),
            patch("shiori.tools.status.db.get_cursors", return_value=dict(_ALL_CURSORS)),
            patch("shiori.tools.status.build_token_provider",
                  side_effect=RuntimeError("boom")),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.dev_repos = set()
            mock_settings.sync_interval_seconds = 300
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 86400
            result = status()

        assert result["token_provider"] == "error"
        # The token-provider warning makes the repo unhealthy, so its full
        # record (carrying that warning) survives the summarization.
        assert any("boom" in w for w in result["repos"]["o/r"]["warnings"])
        assert result["summary"]["unhealthy_repos"] == 1


# ── The pure no-repo view: budgeted, testable without DB mocks ──


class TestNoRepoResponseView:
    """_no_repo_response: the complete no-repo payload built under a hard
    char budget.  Pure (records in, payload out), so the degraded-fleet
    cases that killed the size guarantee are directly testable here
    without standing up 62 mocked repos."""

    @staticmethod
    def _view(records, **kwargs):
        return _no_repo_response(
            records,
            sync_intervals={"dev": 900, "ref": 86400},
            clone_refresh_debounce_seconds=300,
            token_provider="static",  # noqa: S106 - provider name literal, not a secret
            **kwargs,
        )

    def test_all_healthy_empty_repos_small_response(self):
        records = {f"o/r{i:02d}": _healthy_run(100) for i in range(62)}
        result = self._view(records)
        assert result["repos"] == {}
        assert len(json.dumps(result)) < 2000

    def test_mixed_emits_unhealthy_records_verbatim(self):
        records = {
            "o/ok": _healthy_run(),
            "o/bad": {**_healthy_run(), "consecutive_failures": 2},
        }
        result = self._view(records)
        assert list(result["repos"]) == ["o/bad"]
        assert result["repos"]["o/bad"] == records["o/bad"]

    def test_summary_counts_every_repo_not_just_emitted_ones(self):
        records = {
            "o/a": {**_healthy_run(), "consecutive_failures": 1},
            "o/b": {**_healthy_run(), "warnings": ["w"]},
            "o/c": _healthy_run(),
        }
        result = self._view(records)
        assert result["summary"]["unhealthy_repos"] == 2
        assert result["summary"]["healthy_repos"] == 1
        assert result["summary"]["omitted_repos"] == 0
        assert result["summary"]["omitted_repo_names"] == []

    def test_top_level_keys_and_scalars_preserved(self):
        result = self._view({})
        assert list(result) == [
            "repos",
            "clone_refresh_debounce_seconds",
            "sync_intervals",
            "token_provider",
            "summary",
        ]
        assert result["clone_refresh_debounce_seconds"] == 300
        assert result["sync_intervals"] == {"dev": 900, "ref": 86400}
        assert result["token_provider"] == "static"


class TestDegradedFleetBudget:
    """The size guarantee must hold when the fleet is worst-case degraded:
    every repo unhealthy.  Round-1 gap: the all-healthy test was green
    while the degraded response re-inflated to ~45k chars."""

    @staticmethod
    def _degraded_records(n=62):
        # Every repo unhealthy via the token-provider warning alone, with
        # fresh sync runs -- the exact #193 scenario, previously ~45k
        # chars of response.
        return {
            f"o/r{i:02d}": {
                **_healthy_run(100 + i),
                "warnings": [
                    f"token_provider could not be determined: boom-{i}"
                ],
            }
            for i in range(n)
        }

    def test_repos_payload_respects_the_char_budget(self):
        result = TestNoRepoResponseView._view(self._degraded_records())
        payload = json.dumps(result["repos"], ensure_ascii=False)
        assert len(payload) <= _NO_REPO_REPOS_CHAR_BUDGET

    def test_serialized_response_stays_bounded(self):
        result = TestNoRepoResponseView._view(self._degraded_records())
        total = len(json.dumps(result))
        # Budget + summary base + up to 62 names (~31 chars each) + the
        # four top-level scalars -- an order of magnitude under the old
        # 53,836-char response.
        assert total < 15000, f"{total} chars"

    def test_not_every_repo_fits_so_some_are_omitted(self):
        result = TestNoRepoResponseView._view(self._degraded_records())
        assert len(result["repos"]) < 62

    def test_omitted_repos_are_named_and_counted(self):
        result = TestNoRepoResponseView._view(self._degraded_records())
        summary = result["summary"]
        all_names = {f"o/r{i:02d}" for i in range(62)}
        emitted = set(result["repos"])
        omitted = set(summary["omitted_repo_names"])
        assert summary["omitted_repos"] == len(omitted)
        assert emitted | omitted == all_names
        assert emitted.isdisjoint(omitted)
        assert summary["unhealthy_repos"] == 62
        assert summary["healthy_repos"] == 0

    def test_severity_order_failing_repo_wins_the_budget(self):
        # 25 warned-only repos plus one failing repo: the failing one must
        # be emitted and the cut must fall inside the warned class.
        records = {
            f"o/w{i:02d}": {**_healthy_run(100), "warnings": ["w"]}
            for i in range(25)
        }
        records["o/failing"] = {
            **_healthy_run(50),
            "consecutive_failures": 4,
            "warnings": [],
        }
        result = TestNoRepoResponseView._view(records)
        assert "o/failing" in result["repos"]
        assert len(result["repos"]) < 26
        assert all(
            name.startswith("o/w")
            for name in result["summary"]["omitted_repo_names"]
        )

    def test_every_emitted_record_still_carries_its_full_fields(self):
        records = self._degraded_records()
        result = TestNoRepoResponseView._view(records)
        for name, info in result["repos"].items():
            assert info == records[name]


class TestTokenProviderDegradedFleetStaysBounded:
    """Criterion 5 and the size guarantee together: build_token_provider
    raising marks EVERY repo unhealthy; the no-repo response must stay
    bounded (round-1 finding: this exact path re-inflated the response
    to ~45k chars with all 62 records)."""

    def test_no_repo_response_bounded_when_all_repos_unhealthy(self):
        names = _sixty_two_repos()
        runs = {name: _healthy_run(100 + i) for i, name in enumerate(names)}
        with (
            patch("shiori.tools.status._conn"),
            patch("shiori.tools.status.settings") as mock_settings,
            patch("shiori.tools.status.db.get_sync_runs", return_value=runs),
            patch("shiori.tools.status.db.get_all_repo_index_state", return_value={}),
            patch("shiori.tools.status.db.get_chunk_counts", return_value={}),
            patch("shiori.tools.status.db.get_issue_item_count", return_value=0),
            patch("shiori.tools.status.db.get_cursors",
                  return_value=dict(_ALL_CURSORS)),
            patch("shiori.tools.status.build_token_provider",
                  side_effect=RuntimeError("boom")),
        ):
            mock_settings.repos = names
            mock_settings.dev_repos = set()
            mock_settings.sync_interval_seconds = 300
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 86400
            result = status()

        assert result["token_provider"] == "error"
        payload = json.dumps(result["repos"], ensure_ascii=False)
        assert len(payload) <= _NO_REPO_REPOS_CHAR_BUDGET
        assert len(json.dumps(result)) < 15000
        summary = result["summary"]
        assert summary["unhealthy_repos"] == 62
        assert summary["omitted_repos"] + len(result["repos"]) == 62
        assert len(summary["omitted_repo_names"]) == summary["omitted_repos"]
        # Every emitted record still carries the token-provider warning.
        assert all(
            any("boom" in w for w in rec["warnings"])
            for rec in result["repos"].values()
        )


# ── Helper units ──


class TestIsHealthy:
    """_is_healthy: exactly the five unhealthy conditions, nothing else."""

    def test_empty_record_is_healthy(self):
        assert _is_healthy({}) is True

    def test_warnings_make_it_unhealthy(self):
        assert _is_healthy({"warnings": ["something"]}) is False

    def test_empty_warnings_list_is_healthy(self):
        assert _is_healthy({"warnings": []}) is True

    def test_index_stale_makes_it_unhealthy(self):
        assert _is_healthy({"index_stale": True}) is False

    def test_never_indexed_makes_it_unhealthy(self):
        assert _is_healthy({"never_indexed": True}) is False

    def test_positive_consecutive_failures_make_it_unhealthy(self):
        assert _is_healthy({"consecutive_failures": 1}) is False

    def test_zero_consecutive_failures_is_healthy(self):
        assert _is_healthy({"consecutive_failures": 0}) is True

    def test_none_consecutive_failures_is_healthy(self):
        assert _is_healthy({"consecutive_failures": None}) is True

    def test_positive_pending_makes_it_unhealthy(self):
        assert _is_healthy({"pending_count": 1}) is False

    def test_zero_pending_is_healthy(self):
        assert _is_healthy({"pending_count": 0}) is True

    def test_none_pending_is_healthy(self):
        assert _is_healthy({"pending_count": None}) is True

    def test_old_age_alone_is_healthy(self):
        """age drives a warning via _build_warnings; age by itself does not."""
        assert _is_healthy({"age_seconds": 999999}) is True


class TestSummarize:
    def test_empty_fleet(self):
        summary = _summarize({})
        assert summary["total_repos"] == 0
        assert summary["healthy_repos"] == 0
        assert summary["unhealthy_repos"] == 0
        assert summary["pending_total"] == 0
        assert summary["oldest_repo"] is None

    def test_oldest_ignores_none_ages(self):
        summary = _summarize({
            "o/a": {"age_seconds": None, "role": "ref"},
            "o/b": {"age_seconds": 10, "role": "ref"},
        })
        assert summary["oldest_repo"]["repo"] == "o/b"

    def test_counts_are_independent_per_condition(self):
        summary = _summarize({
            "o/a": {"warnings": ["w"], "index_stale": True, "never_indexed": True,
                    "consecutive_failures": 2, "pending_count": 5, "role": "dev"},
            "o/b": {"warnings": [], "role": "ref"},
        })
        assert summary["unhealthy_repos"] == 1
        assert summary["healthy_repos"] == 1
        assert summary["unhealthy_counts"] == {
            "with_warnings": 1,
            "index_stale": 1,
            "never_indexed": 1,
            "failing": 1,
            "with_pending": 1,
        }
        assert summary["pending_total"] == 5
