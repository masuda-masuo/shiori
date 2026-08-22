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
from typing import Any
from unittest.mock import MagicMock, patch

from mcp.server.mcpserver.tools.base import Tool
from mcp_types import JSONRPCResponse

from shiori.tools.status import (
    _NO_REPO_REPOS_CHAR_BUDGET,
    _STATUS_LAST_ERROR_CHAR_CAP,
    _is_healthy,
    _no_repo_budget,
    _no_repo_response,
    _severity_key,
    _summarize,
    _truncate_last_error_for_status,
    _wrapped_result_dumps,
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
    all_chunk_counts=None,
    items_in_db=0,
    all_issue_item_counts=None,
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

    if all_chunk_counts is None:
        c_counts = chunk_counts if chunk_counts is not None else {}
        all_chunk_map = {r: dict(c_counts) for r in repos}
    else:
        all_chunk_map = all_chunk_counts

    if all_issue_item_counts is None:
        issue_map = {r: items_in_db for r in repos}
    else:
        issue_map = all_issue_item_counts

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
            "shiori.tools.status.db.get_all_chunk_counts",
            return_value=all_chunk_map,
        ),
        patch(
            "shiori.tools.status.db.get_chunk_counts",
            return_value=chunk_counts if chunk_counts is not None else {},
        ),
        patch("shiori.tools.status.db.refresh_chunk_counts", return_value={}),
        patch("shiori.tools.status.db.get_issue_item_count", return_value=items_in_db),
        patch(
            "shiori.tools.status.db.get_all_issue_item_counts",
            return_value=issue_map,
        ),
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
        result = self._run_mixed()
        emitted = list(result["repos"])
        severity_order = ["o/failing", "o/stale", "o/pending", "o/warned"]
        # The wrapped-result budget (#431) can cut the least-severe
        # unhealthy repos -- here o/warned tips four full records over
        # 8,000 chars -- but every emitted repo is unhealthy and emission
        # keeps severity order; the cut is named in the summary.
        assert set(emitted) <= set(severity_order)
        assert emitted == [name for name in severity_order if name in emitted]
        assert set(result["summary"]["omitted_repo_names"]) == set(severity_order) - set(emitted)

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
        # Every unhealthy repo is either emitted or named as omitted --
        # the wrapped-result budget (#431) may cut the tail, it never
        # drops one silently.
        assert summary["unhealthy_repos"] == len(result["repos"]) + summary["omitted_repos"]

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

    def test_wrapped_result_respects_the_char_budget(self):
        result = TestNoRepoResponseView._view(self._degraded_records())
        # The cap applies to what the MCP client receives: FastMCP's
        # convert_result wrapping (pretty text + structuredContent inside
        # the JSON-RPC envelope), not the inner `repos` dict (issue #431).
        payload = _wrapped_result_dumps(result)
        assert len(payload) <= _NO_REPO_REPOS_CHAR_BUDGET, f"{len(payload)} chars"

    def test_serialized_response_stays_bounded(self):
        result = TestNoRepoResponseView._view(self._degraded_records())
        # Measured with the same serializer as the budget loop: the
        # wrapped tool result must sit inside the budget that decided its
        # contents.  (The old inner-dict 15000 cap was met by a selection
        # whose client-visible wrapped form was ~22.7k chars -- the same
        # unit mismatch as the loop, issue #431.)
        total = len(_wrapped_result_dumps(result))
        assert total <= _NO_REPO_REPOS_CHAR_BUDGET, f"{total} chars"

    def test_not_every_repo_fits_so_some_are_omitted(self):
        result = TestNoRepoResponseView._view(self._degraded_records())
        # Non-vacuity: at least one full record emitted, at least one cut.
        assert len(result["repos"]) >= 1
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
        # Measured with the wrapped serializer (issue #431), all 26
        # records must NOT fit the 8000-char budget, or the cut would not
        # fall inside the warned class and this test would silently stop
        # exercising severity order.
        records = {
            f"o/w{i:02d}": {
                **_healthy_run(100),
                "warnings": ["token_provider unavailable: boom"],
            }
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


class TestCJKDegradedFleetBudget:
    """The budget must hold when the degraded fleet speaks Japanese (#425).

    The MCP streamable-HTTP transport serializes the body with
    ``ensure_ascii`` left at its default, so each CJK char costs 6 chars
    on the wire but only 1 under the old ``ensure_ascii=False``
    measurement.  ASCII-only fixtures cannot catch that: for pure ASCII
    the wire form is *smaller* than the old measurement (compact
    separators), so the wrong dumps stayed green.

    Shape implemented: rather than hand-tuning a one-record crossover,
    every record here is CJK-heavy, so the crossover falls out of the
    budget loop itself.  The pre-#431 loop admitted records until their
    compact inner `repos` length passed 8000 -- a selection whose
    FastMCP-wrapped form is ~2.6x larger (20,290 chars for a 7,741-char
    `repos` payload), i.e. well over the budget.  The wrapped assertion
    below therefore fails on the pre-#431 loop and passes on this one,
    and the non-vacuity assertions pin that the budget really binds, so
    it cannot pass by emitting everything or nothing.
    """

    @staticmethod
    def _cjk_degraded_records(n=62):
        # Same #193 fleet-wide outage as _degraded_records, with the
        # operator-facing strings in Japanese as they are on the live
        # deployment.
        return {
            f"o/r{i:02d}": {
                **_healthy_run(100 + i),
                "consecutive_failures": 3,
                "last_error": (
                    f"同期に失敗しました（{i}回目）: "
                    "トークンプロバイダに接続できません"
                ),
                "warnings": [
                    "token_provider を判定できませんでした: 認証情報が見つかりません",
                    f"インデックスが古くなっています（最終同期から {100 + i} 秒）",
                ],
            }
            for i in range(n)
        }

    def test_fixture_is_actually_non_ascii(self):
        # Guards the regression test itself: an ASCII-ified fixture would
        # silently stop exercising the escape expansion.
        record = next(iter(self._cjk_degraded_records().values()))
        assert any(ord(ch) > 0x7F for ch in record["last_error"])
        assert any(ord(ch) > 0x7F for w in record["warnings"] for ch in w)

    def test_wire_payload_respects_the_char_budget(self):
        result = TestNoRepoResponseView._view(self._cjk_degraded_records())
        # The client-visible form: convert_result wrapping duplicates the
        # payload (pretty text + structuredContent) and escapes each CJK
        # char to 6 chars in the compact envelope -- the inner `repos`
        # dumps measured 7,741 chars for a selection whose wrapped form
        # was 20,290 (issue #431).
        payload = _wrapped_result_dumps(result)
        assert len(payload) <= _NO_REPO_REPOS_CHAR_BUDGET, f"{len(payload)} chars"

    def test_budget_actually_binds_on_the_cjk_fleet(self):
        # Non-vacuity: some records fit, some are cut -- otherwise the
        # assertion above could hold trivially.
        result = TestNoRepoResponseView._view(self._cjk_degraded_records())
        assert len(result["repos"]) >= 1
        assert result["summary"]["omitted_repos"] > 0

    def test_omitted_cjk_repos_are_still_named(self):
        records = self._cjk_degraded_records()
        result = TestNoRepoResponseView._view(records)
        summary = result["summary"]
        emitted, omitted = set(result["repos"]), set(summary["omitted_repo_names"])
        assert summary["omitted_repos"] == len(omitted)
        assert emitted | omitted == set(records)
        assert emitted.isdisjoint(omitted)

    def test_emitted_cjk_records_keep_every_field(self):
        records = self._cjk_degraded_records()
        result = TestNoRepoResponseView._view(records)
        for name, info in result["repos"].items():
            assert info == records[name]

    def test_serialized_cjk_response_stays_bounded(self):
        result = TestNoRepoResponseView._view(self._cjk_degraded_records())
        # Same serializer as the budget loop: the wrapped tool result must
        # sit inside the budget that decided its contents (the old 15000
        # cap on the inner dict was met while the client-visible form
        # exceeded 20k -- issue #431).
        total = len(_wrapped_result_dumps(result))
        assert total <= _NO_REPO_REPOS_CHAR_BUDGET, f"{total} chars"


class TestOversizedRecordStillEmitted:
    """Criterion 7 as a construction (issue #431): a record whose wrapped
    form alone exceeds the nominal 8,000-char budget must still be
    emitted, or a fully degraded fleet reports zero error detail exactly
    when the operator needs it most.

    `last_error` is an unbounded production string -- record_sync_attempt
    truncates it with a char slice ((error or "")[:2000], src/shiori/db.py),
    so a 2000-char CJK error survives -- and with ensure_ascii escaping
    plus the text/structuredContent duplication a single such record
    wraps to ~22.7k chars (measured in the container; a single record
    already exceeds 8,000 at ~470 CJK chars of last_error).  The old
    fit-or-skip loop dropped it silently -- and with it every other
    record, so the fleet-wide outage emitted ZERO full records.  The
    loop now measures against a per-call effective ceiling floored at
    the one-record payload's wrapped size (_no_repo_budget), so the
    most severe record always fits.
    """

    @staticmethod
    def _oversized_records(n=62, error_chars=2000):
        # ~2000 CJK chars, the production truncation bound of
        # record_sync_attempt's char slice.
        error = (
            "同期に失敗しました（トークンプロバイダーへの接続に失敗しました）" * 100
        )[:error_chars]
        return {
            f"o/r{i:02d}": {
                **_healthy_run(100 + i),
                "consecutive_failures": 3,
                "last_error": error,
                "warnings": [f"3 consecutive sync failures. last_error: {error}"],
            }
            for i in range(n)
        }

    @staticmethod
    def _effective_budget(records):
        # The same computation the budget loop applies (issue #431): the
        # nominal constant, raised to the one-record payload's wrapped
        # size when that alone cannot fit.
        unhealthy = sorted(
            (
                (name, info)
                for name, info in records.items()
                if not _is_healthy(info)
            ),
            key=lambda pair: _severity_key(pair[0], pair[1]),
        )
        return _no_repo_budget(
            unhealthy,
            all_repos=records,
            clone_refresh_debounce_seconds=300,
            sync_intervals={"dev": 900, "ref": 86400},
            token_provider="static",  # noqa: S106 - provider name literal, not a secret
        )

    def test_fixture_one_record_wraps_past_the_nominal_budget(self):
        # Guards the regression tests: if the fixture ever shrinks below
        # the crossover, the floor never binds and they would silently
        # stop exercising the degradation rule.
        effective = self._effective_budget(self._oversized_records())
        assert effective > _NO_REPO_REPOS_CHAR_BUDGET

    def test_most_severe_oversized_record_is_still_emitted(self):
        records = self._oversized_records()
        result = TestNoRepoResponseView._view(records)
        effective = self._effective_budget(records)
        # The most severe record (all failing; o/r61 synced longest ago)
        # is emitted in full, and the emitted payload sits inside the
        # same effective ceiling the loop applied.
        assert len(result["repos"]) >= 1
        assert "o/r61" in result["repos"]
        assert result["repos"]["o/r61"] == records["o/r61"]
        assert len(_wrapped_result_dumps(result)) <= effective
        # The rest are named as omitted, not dropped.
        summary = result["summary"]
        assert summary["unhealthy_repos"] == len(records)
        assert summary["omitted_repos"] == len(records) - 1
        assert set(summary["omitted_repo_names"]) == set(records) - {"o/r61"}

    def test_mixed_fleet_emits_the_failing_oversized_record(self):
        # The finding's mixed case: six warned repos must not crowd out
        # a failing repo whose oversized error used to be skipped, so the
        # operator keeps the most actionable record (severity order).
        error = self._oversized_records(1)["o/r00"]["last_error"]
        records = {
            f"o/w{i:02d}": {
                **_healthy_run(100),
                "warnings": ["token_provider unavailable: boom"],
            }
            for i in range(6)
        }
        records["o/failing"] = {
            **_healthy_run(50),
            "consecutive_failures": 4,
            "last_error": error,
            "warnings": [f"4 consecutive sync failures. last_error: {error}"],
        }
        result = TestNoRepoResponseView._view(records)
        assert "o/failing" in result["repos"]
        assert result["repos"]["o/failing"]["consecutive_failures"] == 4
        # The oversized failing record alone already exceeds the nominal
        # budget, so the warned repos stay named as omitted.
        assert set(result["summary"]["omitted_repo_names"]) == set(records) - {
            "o/failing"
        }


class TestWrappedResultDumpsMatchesInstalledFastMCP:
    """The wrap helper must serialize exactly what the installed FastMCP
    convert_result wrapping plus the streamable-HTTP envelope put on the
    wire for a shiori_status return (issue #431: budget the wrapped tool
    result, not the inner dict).

    The ground truth is computed from the installed SDK pieces -- real
    convert_result metadata for a ``dict[str, Any]`` tool plus the
    transport's JSON-RPC envelope dumps -- not from a hard-coded length,
    so an SDK upgrade that changes the wrapping fails this test and
    forces a re-measure instead of silently drifting.
    """

    @staticmethod
    def _sdk_wire(payload):
        def _probe(repo: str | None = None) -> dict[str, Any]:
            ...

        tool = Tool.from_function(_probe, name="shiori_status", description="probe")
        call_tool_result = tool.fn_metadata.convert_result(payload)
        result = call_tool_result.model_dump(
            by_alias=True, mode="json", exclude_none=True
        )
        envelope = JSONRPCResponse(jsonrpc="2.0", id=1, result=result)
        body = envelope.model_dump(mode="json", by_alias=True, exclude_none=True)
        return json.dumps(body, separators=(",", ":"))

    def test_helper_matches_the_installed_wire_serialization(self):
        # A CJK degraded payload: the escape expansion and the text /
        # structuredContent duplication both have to match the SDK.
        records = TestCJKDegradedFleetBudget._cjk_degraded_records(2)
        payload = TestNoRepoResponseView._view(records)
        assert _wrapped_result_dumps(payload) == self._sdk_wire(payload)


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
        # The budget is on the wrapped tool result the client receives
        # (issue #431); the inner `repos` dumps is not the cap.
        payload = _wrapped_result_dumps(result)
        assert len(payload) <= _NO_REPO_REPOS_CHAR_BUDGET, f"{len(payload)} chars"
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



# ── last_error truncation at the status surface (issue #433) ──


def _cjk_error(chars: int = 2000) -> str:
    # Same CJK-heavy production error shape as TestCJKDegradedFleetBudget /
    # TestOversizedRecordStillEmitted: every char is non-ASCII, so it costs
    # 6 wire chars under the transport's ensure_ascii default.
    return ("同期に失敗しました（トークンプロバイダーへの接続に失敗しました）" * 100)[:chars]


class TestLastErrorStatusSurfaceTruncation:
    """Criterion 1 & 2 (issue #433): last_error is truncated to the SAME
    form in the record field and the consecutive_failures warning, and a
    single worst-case record stays inside the 12,000-char bound."""

    @staticmethod
    def _single_failing_repo_payload(last_error, consecutive_failures=3):
        # Drive the real status() surface (no-repo path) so the truncation
        # point in status() is exercised exactly as in production.
        runs = {
            "o/r": {
                **_healthy_run(100),
                "consecutive_failures": consecutive_failures,
                "last_error": last_error,
            }
        }
        with _patched_status(["o/r"], sync_runs=runs):
            return status()

    def test_oversized_cjk_last_error_is_truncated_in_record_and_warning(self):
        raw = _cjk_error(2000)
        result = self._single_failing_repo_payload(raw)
        record = result["repos"]["o/r"]
        expected = _truncate_last_error_for_status(raw)
        # record field carries the shared truncated form
        assert record["last_error"] == expected
        assert record["last_error"].startswith(raw[:_STATUS_LAST_ERROR_CHAR_CAP])
        # marker names the original length
        assert record["last_error"].endswith("… (truncated, 2000 chars stored)")
        # warning carries the SAME truncated form (raw is not duplicated)
        assert len(record["warnings"]) == 1
        assert record["warnings"][0].endswith(expected)
        assert raw not in record["warnings"][0]
        # repo-specified path truncates identically
        with _patched_status(
            ["o/r"],
            sync_runs={
                "o/r": {
                    **_healthy_run(100),
                    "consecutive_failures": 3,
                    "last_error": raw,
                }
            },
        ):
            repo_result = status(repo="o/r")
        assert repo_result["repos"]["o/r"]["last_error"] == expected
        assert repo_result["repos"]["o/r"]["warnings"][0].endswith(expected)

    def test_wrapped_worst_case_record_stays_under_twelve_thousand(self):
        # Criterion 2: the payload a fully degraded fleet actually emits --
        # exactly ONE worst-case record (2000-char CJK last_error,
        # consecutive_failures > 0) in `repos` PLUS every other unhealthy
        # repo named in `summary.omitted_repo_names` -- must never exceed
        # 12,000 chars.  This drives the REAL status() surface with the
        # existing 62-repo CJK oversized fixture (issue #433), so the
        # `omitted_repo_names` overhead the prior round's degenerate
        # single-repo test hid is included.  That floor -- not the
        # zero-omitted happy path -- is the criterion-2 subject; the
        # degenerate payload is smaller (~9,400 chars) and must not be the
        # only thing guarding the bound.
        records = TestOversizedRecordStillEmitted._oversized_records(62)
        with _patched_status(list(records.keys()), sync_runs=records):
            result = status()
        # Exactly one worst-case record is emitted; the rest are named.
        assert len(result["repos"]) == 1
        assert result["summary"]["omitted_repos"] == 61
        wrapped = _wrapped_result_dumps(result)
        # Criterion 2 bound, measured with the existing wrap helper.
        assert len(wrapped) <= 12_000, f"{len(wrapped)} chars"
        # The chosen cap (250) keeps a healthy margin under the bound.
        assert len(wrapped) <= 11_500, f"{len(wrapped)} chars"

    def test_short_last_error_passes_through_unmarked(self):
        raw = "git fetch failed"  # under the cap, ASCII
        result = self._single_failing_repo_payload(raw, consecutive_failures=1)
        record = result["repos"]["o/r"]
        # byte-identical, no marker
        assert record["last_error"] == raw
        assert "truncated" not in record["last_error"]
        assert len(record["warnings"]) == 1
        assert record["warnings"][0].endswith(raw)
        assert len(record["last_error"]) == len(raw)

    def test_truncate_helper_none_and_short_and_over(self):
        assert _truncate_last_error_for_status(None) is None
        short = "boom"
        assert _truncate_last_error_for_status(short) is short
        over = _cjk_error(2000)
        trunc = _truncate_last_error_for_status(over)
        assert trunc != over
        assert trunc.endswith("… (truncated, 2000 chars stored)")
        assert trunc.startswith(over[:_STATUS_LAST_ERROR_CHAR_CAP])
        assert "truncated" in trunc


class TestChunkCountsCaching:
    """Acceptance criteria 1 & 2: per-repo chunk counts caching behavior in status()."""

    def test_no_repo_chunk_counts_caching_sources(self):
        """Acceptance criterion 1: 3 repos where cache has 2 -> get_all_chunk_counts 1x,
        get_chunk_counts 1x, refresh_chunk_counts 1x, source == 'mixed';
        all 3 cached -> get_chunk_counts 0x, source == 'cached'.
        """
        repos = ["o/r1", "o/r2", "o/r3"]
        cached_map = {
            "o/r1": {"code": 10},
            "o/r2": {"issue": 5},
        }

        with (
            patch("shiori.tools.status._conn"),
            patch("shiori.tools.status.settings") as mock_settings,
            patch("shiori.tools.status.build_token_provider") as mock_tp,
            patch("shiori.tools.status.db.get_sync_runs", return_value={}),
            patch("shiori.tools.status.db.get_all_repo_index_state", return_value={}),
            patch("shiori.tools.status.db.get_cursors", return_value=dict(_ALL_CURSORS)),
            patch("shiori.tools.status.db.get_issue_item_count", return_value=0),
            patch(
                "shiori.tools.status.db.get_all_chunk_counts",
                return_value=cached_map,
            ) as mock_get_all,
            patch(
                "shiori.tools.status.db.get_chunk_counts",
                return_value={"code": 2},
            ) as mock_get_single,
            patch("shiori.tools.status.db.refresh_chunk_counts") as mock_refresh,
            patch("shiori.tools.status._validate_repo_name", side_effect=lambda r: r),
        ):
            mock_settings.repos = repos
            mock_settings.dev_repos = set()
            mock_settings.sync_interval_seconds = 300
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 86400
            mock_tp.return_value.name = "static"

            res = status()
            assert res["summary"]["chunk_counts_source"] == "mixed"
            assert mock_get_all.call_count == 1
            assert mock_get_single.call_count == 1
            assert mock_get_single.call_args[0][1] == "o/r3"
            assert mock_refresh.call_count == 1
            assert mock_refresh.call_args[0][1] == "o/r3"

        cached_map_all = {
            "o/r1": {"code": 10},
            "o/r2": {"issue": 5},
            "o/r3": {"code": 2},
        }
        with (
            patch("shiori.tools.status._conn"),
            patch("shiori.tools.status.settings") as mock_settings,
            patch("shiori.tools.status.build_token_provider") as mock_tp,
            patch("shiori.tools.status.db.get_sync_runs", return_value={}),
            patch("shiori.tools.status.db.get_all_repo_index_state", return_value={}),
            patch("shiori.tools.status.db.get_cursors", return_value=dict(_ALL_CURSORS)),
            patch("shiori.tools.status.db.get_issue_item_count", return_value=0),
            patch(
                "shiori.tools.status.db.get_all_chunk_counts",
                return_value=cached_map_all,
            ) as mock_get_all,
            patch("shiori.tools.status.db.get_chunk_counts") as mock_get_single,
            patch("shiori.tools.status.db.refresh_chunk_counts") as mock_refresh,
            patch("shiori.tools.status._validate_repo_name", side_effect=lambda r: r),
        ):
            mock_settings.repos = repos
            mock_settings.dev_repos = set()
            mock_settings.sync_interval_seconds = 300
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 86400
            mock_tp.return_value.name = "static"

            res = status()
            assert res["summary"]["chunk_counts_source"] == "cached"
            assert mock_get_single.call_count == 0
            assert mock_refresh.call_count == 0

    def test_zero_chunk_repo_cached_hit(self):
        """0-chunk repos present in get_all_chunk_counts as {} trigger a cache hit, not live query."""
        repos = ["o/empty"]
        cached_map = {"o/empty": {}}
        with (
            patch("shiori.tools.status._conn"),
            patch("shiori.tools.status.settings") as mock_settings,
            patch("shiori.tools.status.build_token_provider") as mock_tp,
            patch("shiori.tools.status.db.get_sync_runs", return_value={}),
            patch("shiori.tools.status.db.get_all_repo_index_state", return_value={}),
            patch("shiori.tools.status.db.get_cursors", return_value=dict(_ALL_CURSORS)),
            patch("shiori.tools.status.db.get_issue_item_count", return_value=0),
            patch(
                "shiori.tools.status.db.get_all_chunk_counts",
                return_value=cached_map,
            ),
            patch("shiori.tools.status.db.get_chunk_counts") as mock_get_single,
            patch("shiori.tools.status.db.refresh_chunk_counts") as mock_refresh,
            patch("shiori.tools.status._validate_repo_name", side_effect=lambda r: r),
        ):
            mock_settings.repos = repos
            mock_settings.dev_repos = set()
            mock_settings.sync_interval_seconds = 300
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 86400
            mock_tp.return_value.name = "static"

            res = status()
            assert res["summary"]["chunk_counts_source"] == "cached"
            assert mock_get_single.call_count == 0
            assert mock_refresh.call_count == 0

    def test_no_repo_fields_identical_with_cached_counts(self):
        """Acceptance criterion 2: per-repo records in no-repo response are identical
        to today's for the same counts.
        """
        counts = {"code": 12, "issue": 4, "pr_review": 1}
        with _patched_status(
            ["o/unhealthy"],
            all_chunk_counts={"o/unhealthy": counts},
            sync_runs={"o/unhealthy": {"last_error": "boom", "consecutive_failures": 1}},
        ):
            res = status()
            rec = res["repos"]["o/unhealthy"]
            assert rec["chunks"] == counts
            assert rec["code_chunks"] == 12
            assert rec["code_indexed"] is True
            assert any("1 consecutive sync failures" in w for w in rec["warnings"])


class TestAllIssueItemCounts:
    """Issue #438: get_all_issue_item_counts is called once on the no-repo path."""

    def test_no_repo_calls_get_all_issue_item_counts_once(self):
        """3 repos, no-repo call -> get_all_issue_item_counts 1x,
        get_issue_item_count 0x, and each repo's items_in_db matches
        the dict value (missing repo -> 0).
        """
        repos = ["o/r1", "o/r2", "o/r3"]
        all_issue_map = {"o/r1": 42, "o/r2": 7, "o/r3": 0}
        # Make all repos unhealthy so they appear in the no-repo repos dict
        sync_runs = {
            r: {"last_error": "boom", "consecutive_failures": 1}
            for r in repos
        }
        provider = MagicMock()
        provider.name = "static"

        with (
            patch("shiori.tools.status._conn"),
            patch("shiori.tools.status.settings") as mock_settings,
            patch("shiori.tools.status.build_token_provider", return_value=provider),
            patch("shiori.tools.status.db.get_sync_runs", return_value=sync_runs),
            patch("shiori.tools.status.db.get_all_repo_index_state", return_value={}),
            patch("shiori.tools.status.db.get_all_chunk_counts", return_value={r: {} for r in repos}),
            patch("shiori.tools.status.db.get_chunk_counts", return_value={}),
            patch("shiori.tools.status.db.refresh_chunk_counts", return_value={}),
            patch("shiori.tools.status.db.get_cursors", return_value=dict(_ALL_CURSORS)),
            patch("shiori.tools.status._validate_repo_name", side_effect=lambda r: r),
            patch(
                "shiori.tools.status.db.get_all_issue_item_counts",
                return_value=all_issue_map,
            ) as mock_get_all,
            patch(
                "shiori.tools.status.db.get_issue_item_count", return_value=0,
            ) as mock_get_single,
        ):
            mock_settings.repos = repos
            mock_settings.dev_repos = set()
            mock_settings.sync_interval_seconds = 300
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 86400

            res = status()
            assert mock_get_all.call_count == 1
            assert mock_get_single.call_count == 0
            for r in repos:
                assert res["repos"][r]["items_in_db"] == all_issue_map.get(r, 0)

    def test_no_repo_missing_repo_in_dict_yields_zero(self):
        """A repo absent from get_all_issue_item_counts result gets items_in_db == 0."""
        repos = ["o/r1", "o/missing"]
        all_issue_map = {"o/r1": 5}
        sync_runs = {
            r: {"last_error": "boom", "consecutive_failures": 1}
            for r in repos
        }

        with _patched_status(
            repos,
            all_issue_item_counts=all_issue_map,
            sync_runs=sync_runs,
        ):
            res = status()
            assert res["repos"]["o/r1"]["items_in_db"] == 5
            assert res["repos"]["o/missing"]["items_in_db"] == 0

    def test_single_repo_calls_get_issue_item_count(self):
        """Single-repo call -> get_issue_item_count 1x,
        get_all_issue_item_counts not called.
        """
        provider = MagicMock()
        provider.name = "static"

        with (
            patch("shiori.tools.status._conn"),
            patch("shiori.tools.status.settings") as mock_settings,
            patch("shiori.tools.status.build_token_provider", return_value=provider),
            patch("shiori.tools.status.db.get_sync_run", return_value=None),
            patch("shiori.tools.status.db.get_repo_index_state", return_value={}),
            patch("shiori.tools.status.db.get_chunk_counts", return_value={}),
            patch("shiori.tools.status.db.get_cursors", return_value=dict(_ALL_CURSORS)),
            patch("shiori.tools.status._validate_repo_name", side_effect=lambda r: r),
            patch(
                "shiori.tools.status.db.get_issue_item_count", return_value=3,
            ) as mock_get_single,
            patch(
                "shiori.tools.status.db.get_all_issue_item_counts",
            ) as mock_get_all,
        ):
            mock_settings.repos = ["o/only"]
            mock_settings.dev_repos = set()
            mock_settings.sync_interval_seconds = 300
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 86400

            res = status(repo="o/only")
            assert mock_get_single.call_count == 1
            assert mock_get_single.call_args[0][1] == "o/only"
            assert mock_get_all.call_count == 0
            assert res["repos"]["o/only"]["items_in_db"] == 3
