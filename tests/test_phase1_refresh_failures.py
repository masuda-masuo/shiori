"""Phase 1 refresh-failure acceptance tests (shiori #457, #458, #460).

Step 0 characterization baseline: with an intact clone on disk and token
acquisition failing (a DNS outage simulated inside the token provider, not by
mocking ``refresh_clone``), the public ``shiori_read_file`` and
``shiori_grep`` tools keep serving the on-disk clone.  That baseline is
independent of the new ``stale`` / ``refresh_error`` metadata requested by the
acceptance criteria below; it is expected to pass at the current base.

The acceptance tests pin this public contract:

* ``read_file`` on a failed refresh with an intact clone returns the content
  plus ``stale=True`` and a short ``refresh_error`` (criterion 1).
* ``grep`` on a failed refresh with an intact clone still returns matches,
  reports the repo in ``stale_repos`` as ``{"repo": ..., "error": ...}`` and
  sets ``stale=True``; a zero-match stale search stays distinguishable from a
  clean zero-match search (criterion 2).
* ``grep`` with no clone reports the repo in ``skipped_repos`` as
  ``{"repo": ..., "reason": ...}`` and sets ``partial=True`` (criterion 3).
* Debounced and successful tool responses omit ``stale`` / ``refresh_error``
  (and ``grep`` omits ``stale_repos``) (criterion 4).
* Repeated failed tool-path refreshes surface a per-repo
  ``tool_refresh_consecutive_failures`` count in ``status()``, make the repo
  unhealthy, expose the count in warnings, and are cleared by one successful
  tool-path refresh while remaining independent of sync failures (criterion 5).
* ``report`` exposes the same stale metadata on a failed refresh (criterion 6,
  report half only -- see the notes at the bottom of this module).

Schema assumptions adopted by these tests (the parent/implementer must adopt
them, since the brief leaves the internal state/design open):

* ``stale_repos`` entries are mappings with ``repo`` and ``error`` keys;
  ``skipped_repos`` entries are mappings with ``repo`` and ``reason`` keys.
* The new public status counter is named
  ``tool_refresh_consecutive_failures`` on both the per-repo record and the
  aggregate (no-repo) record.  The tests assume the tool-path failure state is
  observable in-process by ``status()`` without a new DB round-trip; the
  storage shape itself is not asserted.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from shiori.mcp_server import (
    grep_search,
    list_tree,
    read_file,
    report,
    status,
)

REPO = "owner/phase1-fixture"
KNOWN_TOKEN = "SHIORI_PHASE1_KNOWN_TOKEN_42"
KNOWN_FILE = "docs/guide.md"

_CURSORS = {
    "docs": "h1",
    "issues": "h2",
    "issue_comments": "h3",
    "pr_review_comments": "h4",
}


class RaisingTokenProvider:
    """Token acquisition fails as it would during a DNS outage."""

    name = "raising"

    def __init__(self) -> None:
        self.calls = 0

    def get_token(self):
        self.calls += 1
        raise RuntimeError(
            "token acquisition failed: DNS resolution error "
            "(Name or service not known)"
        )


def _make_clone(root: Path, repo: str = REPO) -> Path:
    """Create a minimal but realistic on-disk clone with a known file."""
    clone = root / "repos" / repo.replace("/", "__")
    (clone / ".git").mkdir(parents=True)
    (clone / "docs").mkdir()
    (clone / KNOWN_FILE).write_text(
        f"# Phase 1 fixture\n\n{KNOWN_TOKEN}\nsecond line\n",
        encoding="utf-8",
    )
    return clone


def _settings_for(clone: Path, *, repo: str = REPO, interval: int = 3600) -> MagicMock:
    settings = MagicMock()
    settings.repos = [repo]
    settings.dev_repos = set()
    settings.docs_only_repos = set()
    settings.dev_sync_interval_seconds = 900
    settings.ref_sync_interval_seconds = 86400
    settings.sync_interval_seconds = interval
    settings.fetch_concurrency = 4
    settings.repo_dir.side_effect = lambda name: str(clone)
    return settings


def _fake_conn() -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    return conn


@contextlib.contextmanager
def _advancing_time(start: float = 1000.0, step: float = 1000.0):
    """Advance ``pipeline.time.monotonic`` past the Phase 1 debounce window.

    ``start`` lets a later success run share a clock that is safely ahead of
    failure timestamps recorded by the implementation.
    """
    clock = MagicMock()
    now = {"value": start - step}

    def monotonic() -> float:
        now["value"] += step
        return now["value"]

    clock.monotonic.side_effect = monotonic
    with patch("shiori.pipeline.time", clock):
        yield


@contextlib.contextmanager
def _clone_env(
    clone: Path,
    *,
    repo: str = REPO,
    provider=None,
    refresh_return: str | None = None,
    interval: int = 3600,
):
    """Serve *clone* for *repo* through the real public tool call paths.

    ``_ensure_phase1`` and (unless ``refresh_return`` is given) the real
    ``refresh_clone`` run; only the token provider is replaced.  This is
    deliberate: tests proving token-failure propagation must not patch
    ``_ensure_phase1`` away.
    """
    settings = _settings_for(clone, repo=repo, interval=interval)
    patches = [
        patch("shiori.tools.read.settings", settings),
        patch("shiori.tools.grep.settings", settings),
        patch("shiori.tools.report.settings", settings),
        patch("shiori.tools.list_tree.settings", settings),
        patch("shiori.tools.common.settings", settings),
        patch("shiori.pipeline.settings", settings),
        patch("shiori.pipeline._conn", _fake_conn),
        patch("shiori.pipeline.db.upsert_clone_head"),
    ]
    if provider is not None:
        patches.append(
            patch("shiori.pipeline.build_token_provider", return_value=provider)
        )
    if refresh_return is not None:
        patches.append(
            patch("shiori.refresh.refresh_clone", return_value=refresh_return)
        )
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield settings


def _repo_entry(entries, repo: str) -> dict:
    """Return the entry for *repo* from a list of structured entries."""
    assert entries, f"expected a non-empty collection for {repo}, got {entries!r}"
    for entry in entries:
        if isinstance(entry, dict) and entry.get("repo") == repo:
            return entry
    raise AssertionError(f"no entry for {repo} in {entries!r}")


def _assert_short_refresh_error(error, *, context: str) -> None:
    """The refresh error is a short, cause-bearing message, not a traceback."""
    assert isinstance(error, str) and error, f"{context}: refresh_error must be non-empty"
    assert len(error) <= 500, f"{context}: refresh_error should be short: {error!r}"
    lowered = error.lower()
    assert any(
        marker in lowered for marker in ("token", "dns", "name resolution")
    ), f"{context}: refresh_error should carry the token/DNS cause: {error!r}"


def _sync_run(
    *,
    consecutive_failures: int = 0,
    last_error: str | None = None,
    age_seconds: int = 10,
) -> dict:
    return {
        "last_synced_at": "2026-01-01T00:00:00+00:00",
        "age_seconds": age_seconds,
        "route": "mcp",
        "docs_updated": 0,
        "issues_indexed": 0,
        "code_added": 0,
        "last_attempt_at": "2026-01-01T00:00:00+00:00",
        "last_error": last_error,
        "consecutive_failures": consecutive_failures,
        "pending_count": None,
        "last_progress_at": None,
    }


def _index_state() -> dict:
    return {"clone_head": "abc123", "indexed_head": "abc123", "last_sync_error": None}


@contextlib.contextmanager
def _status_env(
    repo: str,
    *,
    sync_run: dict | None = None,
    index_state: dict | None = None,
    repos: list[str] | None = None,
    sync_runs: dict | None = None,
):
    """Call the real ``status()`` with its DB reads faked."""
    target_repos = list(repos) if repos is not None else [repo]
    settings = _settings_for(Path("/nonexistent"), repo=repo, interval=10)
    settings.repos = target_repos
    default_sync_run = _sync_run() if sync_run is None else sync_run
    runs_map = {r: dict(default_sync_run) for r in target_repos}
    if sync_runs is not None:
        for r, sr in sync_runs.items():
            runs_map[r] = dict(sr)
    base_index_state = _index_state() if index_state is None else index_state
    index_state_map = {r: dict(base_index_state) for r in target_repos}
    provider = MagicMock()
    provider.name = "fake"
    with (
        patch("shiori.tools.status._conn", _fake_conn),
        patch("shiori.tools.status.settings", settings),
        patch("shiori.tools.common.settings", settings),
        patch("shiori.tools.status.build_token_provider", return_value=provider),
        patch("shiori.tools.status.db.get_sync_run", side_effect=lambda _c, r: runs_map.get(r, default_sync_run)),
        patch("shiori.tools.status.db.get_repo_index_state", side_effect=lambda _c, r: index_state_map.get(r, base_index_state)),
        patch("shiori.tools.status.db.get_chunk_counts", return_value={}),
        patch("shiori.tools.status.db.get_issue_item_count", return_value=0),
        patch("shiori.tools.status.db.get_cursors", return_value=dict(_CURSORS)),
        patch("shiori.tools.status.db.get_sync_runs", return_value=runs_map),
        patch(
            "shiori.tools.status.db.get_all_repo_index_state",
            return_value=index_state_map,
        ),
        patch("shiori.tools.status.db.get_all_chunk_counts", return_value={r: {} for r in target_repos}),
        patch("shiori.tools.status.db.get_all_issue_item_counts", return_value={r: 0 for r in target_repos}),
        patch("shiori.tools.status.db.refresh_chunk_counts"),
    ):
        yield


def _fail_tool_refreshes(clone: Path, repo: str, times: int) -> RaisingTokenProvider:
    """Drive *times* real failed tool-path refresh attempts via ``read_file``."""
    provider = RaisingTokenProvider()
    with _advancing_time(), _clone_env(clone, repo=repo, provider=provider, interval=0):
        for _ in range(times):
            read_file(path=KNOWN_FILE, repo=repo)
    return provider


def _run_successful_background_sync(repo: str) -> dict:
    """Run ``_do_sync`` for *repo* with the network/DB work mocked out."""
    from shiori.pipeline import _do_sync

    conn = _fake_conn()
    settings = _settings_for(Path("/nonexistent"), repo=repo, interval=0)
    with (
        patch("shiori.pipeline.settings", settings),
        patch("shiori.pipeline._sync_lock") as lock,
        patch("shiori.pipeline.build_token_provider", return_value=MagicMock()),
        patch("shiori.pipeline._get_embedder", return_value=MagicMock()),
        patch("shiori.pipeline._conn", return_value=conn),
        patch("shiori.pipeline._is_bulk_path", return_value=False),
        patch("shiori.pipeline.schema.migrate"),
        patch("shiori.pipeline._acquire_repo_lock", return_value=True),
        patch("shiori.pipeline._release_repo_lock"),
        patch("shiori.pipeline.sync_docs", return_value=1),
        patch("shiori.pipeline.sync_issues", return_value=1),
        patch("shiori.pipeline.sync_code", return_value=1),
        patch(
            "shiori.pipeline.db.record_sync_run",
            return_value=MagicMock(isoformat=lambda: "2026-01-01T00:00:00+00:00"),
        ),
        patch("shiori.pipeline.db.record_sync_attempt"),
        patch("shiori.pipeline.db.advance_indexed_head"),
        patch("shiori.pipeline.db.refresh_chunk_counts"),
    ):
        lock.acquire.return_value = True
        return _do_sync(repos=[repo])


# ---------------------------------------------------------------------------
# Step 0: characterization baseline (expected to PASS at the current base)
# ---------------------------------------------------------------------------


class TestBaselineCloneKeepsServingOnTokenFailure:
    """Reads and grep must keep using the intact clone when the token fails."""

    def test_read_file_and_grep_still_use_intact_clone(self, tmp_path):
        clone = _make_clone(tmp_path)
        provider = RaisingTokenProvider()

        with _clone_env(clone, provider=provider):
            read_result = read_file(path=KNOWN_FILE, repo=REPO)
            grep_result = grep_search(
                pattern=KNOWN_TOKEN,
                repo=REPO,
                regex=False,
                ignore_case=False,
            )

        # The token provider really was asked for a token (this is the
        # failure being injected -- refresh_clone itself was not mocked).
        assert provider.calls >= 1

        # Reads continue from the clone on disk.
        assert read_result["repo"] == REPO
        assert read_result["path"] == KNOWN_FILE
        assert KNOWN_TOKEN in read_result["content"]
        assert read_result["total_lines"] >= 2

        # Grep continues from the clone on disk.
        assert grep_result["total_matches"] >= 1
        assert grep_result["matches"]
        assert any(KNOWN_TOKEN in match["text"] for match in grep_result["matches"])
        assert all(match["repo"] == REPO for match in grep_result["matches"])


# ---------------------------------------------------------------------------
# Criterion 1: read_file serves the intact clone and marks itself stale
# ---------------------------------------------------------------------------


class TestReadFileStaleMetadata:
    def test_token_failure_with_intact_clone_returns_content_and_stale(self, tmp_path):
        repo = "owner/phase1-read-stale"
        clone = _make_clone(tmp_path, repo)
        provider = RaisingTokenProvider()

        with _clone_env(clone, repo=repo, provider=provider):
            result = read_file(path=KNOWN_FILE, repo=repo)

        assert provider.calls == 1
        assert result["repo"] == repo
        assert result["path"] == KNOWN_FILE
        assert KNOWN_TOKEN in result["content"]
        assert result["stale"] is True

        _assert_short_refresh_error(result["refresh_error"], context="read_file")


# ---------------------------------------------------------------------------
# Criterion 2: grep still matches, reports stale repos, and stays distinguishable
# ---------------------------------------------------------------------------


class TestGrepStaleMetadata:
    def test_grep_finds_match_and_reports_stale_repo(self, tmp_path):
        repo = "owner/phase1-grep-stale"
        clone = _make_clone(tmp_path, repo)

        with _clone_env(clone, repo=repo, provider=RaisingTokenProvider()):
            result = grep_search(
                pattern=KNOWN_TOKEN,
                repo=repo,
                regex=False,
                ignore_case=False,
            )

        assert result["total_matches"] >= 1
        assert any(KNOWN_TOKEN in match["text"] for match in result["matches"])
        assert result["stale"] is True
        entry = _repo_entry(result["stale_repos"], repo)
        _assert_short_refresh_error(entry["error"], context="grep stale_repos")

    def test_zero_match_stale_search_is_distinguishable_from_clean_search(self, tmp_path):
        clone = _make_clone(tmp_path)
        missing = "SHIORI_TOKEN_THAT_IS_NOT_PRESENT"

        stale_repo = "owner/phase1-grep-zero-stale"
        with _clone_env(
            clone, repo=stale_repo, provider=RaisingTokenProvider()
        ):
            stale = grep_search(
                pattern=missing,
                repo=stale_repo,
                regex=False,
                ignore_case=False,
            )

        clean_repo = "owner/phase1-grep-zero-clean"
        ok_provider = MagicMock()
        ok_provider.get_token.return_value = "token"
        with _clone_env(
            clone,
            repo=clean_repo,
            provider=ok_provider,
            refresh_return="head",
            interval=0,
        ):
            clean = grep_search(
                pattern=missing,
                repo=clean_repo,
                regex=False,
                ignore_case=False,
            )

        assert stale["total_matches"] == 0
        assert clean["total_matches"] == 0
        # The stale zero-match carries degradation metadata; the clean one does not.
        assert stale["stale"] is True
        assert bool(stale["stale_repos"]) is True
        assert "stale" not in clean
        assert "refresh_error" not in clean
        assert not clean.get("stale_repos")


# ---------------------------------------------------------------------------
# Criterion 3: a missing clone is skipped with a reason and partial=True
# ---------------------------------------------------------------------------


class TestGrepMissingClone:
    def test_missing_clone_reports_reason_and_partial(self, tmp_path):
        repo = "owner/phase1-grep-missing"
        missing_clone = tmp_path / "does-not-exist"

        with _clone_env(
            missing_clone, repo=repo, provider=RaisingTokenProvider()
        ):
            result = grep_search(
                pattern=KNOWN_TOKEN,
                repo=repo,
                regex=False,
                ignore_case=False,
            )

        assert result["total_matches"] == 0
        assert result["matches"] == []
        assert result["partial"] is True
        entry = _repo_entry(result["skipped_repos"], repo)
        assert isinstance(entry["reason"], str) and entry["reason"]


# ---------------------------------------------------------------------------
# Criterion 4: debounced / successful responses carry no stale metadata
# ---------------------------------------------------------------------------


class TestNoStaleMetadataOnDebounceOrSuccess:
    def test_debounced_read_file_is_not_reported_as_stale(self, tmp_path):
        clone = _make_clone(tmp_path)

        # Control: an actual failed refresh IS marked stale, so the negative
        # assertion below distinguishes debounce from failure rather than
        # passing vacuously because the field does not exist yet.
        fail_repo = "owner/phase1-debounce-fail"
        with _clone_env(
            clone, repo=fail_repo, provider=RaisingTokenProvider(), interval=3600
        ):
            failed = read_file(path=KNOWN_FILE, repo=fail_repo)
        assert failed["stale"] is True

        repo = "owner/phase1-debounce-ok"
        ok_provider = MagicMock()
        ok_provider.get_token.return_value = "token"
        with patch("shiori.refresh.refresh_clone", return_value="head") as mock_refresh:
            with _clone_env(clone, repo=repo, provider=ok_provider, interval=3600):
                first = read_file(path=KNOWN_FILE, repo=repo)
                second = read_file(path=KNOWN_FILE, repo=repo)

        assert mock_refresh.call_count == 1, "second call should have been debounced"
        for result in (first, second):
            assert KNOWN_TOKEN in result["content"]
            assert "stale" not in result
            assert "refresh_error" not in result

    def test_successful_read_file_omits_stale_metadata(self, tmp_path):
        clone = _make_clone(tmp_path)

        fail_repo = "owner/phase1-success-fail"
        with _clone_env(
            clone, repo=fail_repo, provider=RaisingTokenProvider(), interval=3600
        ):
            failed = read_file(path=KNOWN_FILE, repo=fail_repo)
        assert failed["stale"] is True

        repo = "owner/phase1-success-ok"
        ok_provider = MagicMock()
        ok_provider.get_token.return_value = "token"
        with _clone_env(
            clone, repo=repo, provider=ok_provider, refresh_return="head", interval=0
        ):
            result = read_file(path=KNOWN_FILE, repo=repo)

        assert KNOWN_TOKEN in result["content"]
        assert "stale" not in result
        assert "refresh_error" not in result

    def test_successful_grep_omits_stale_metadata(self, tmp_path):
        clone = _make_clone(tmp_path)

        fail_repo = "owner/phase1-grep-success-fail"
        with _clone_env(
            clone, repo=fail_repo, provider=RaisingTokenProvider(), interval=3600
        ):
            failed = grep_search(
                pattern=KNOWN_TOKEN,
                repo=fail_repo,
                regex=False,
                ignore_case=False,
            )
        assert failed["stale"] is True

        repo = "owner/phase1-grep-success-ok"
        ok_provider = MagicMock()
        ok_provider.get_token.return_value = "token"
        with _clone_env(
            clone, repo=repo, provider=ok_provider, refresh_return="head", interval=0
        ):
            result = grep_search(
                pattern=KNOWN_TOKEN,
                repo=repo,
                regex=False,
                ignore_case=False,
            )

        assert result["total_matches"] >= 1
        assert "stale" not in result
        assert "refresh_error" not in result
        assert not result.get("stale_repos")


# ---------------------------------------------------------------------------
# Criterion 5: status exposes tool-path refresh failures independently of sync
# ---------------------------------------------------------------------------


class TestStatusToolRefreshCounter:
    def test_failed_tool_refreshes_surface_count_unhealthy_and_warning(self, tmp_path):
        repo = "owner/phase1-status-a"
        clone = _make_clone(tmp_path, repo)
        provider = _fail_tool_refreshes(clone, repo, 3)
        assert provider.calls == 3, "each call should be an actual refresh attempt"

        with _status_env(repo):
            per_repo = status(repo=repo)
            aggregate = status()

        record = per_repo["repos"][repo]
        assert record["tool_refresh_consecutive_failures"] == 3
        assert any("tool" in warning.lower() and "3" in warning for warning in record["warnings"])

        agg_record = aggregate["repos"][repo]
        assert agg_record["tool_refresh_consecutive_failures"] == 3
        assert aggregate["summary"]["unhealthy_repos"] >= 1

    def test_successful_tool_refresh_clears_count(self, tmp_path):
        repo = "owner/phase1-status-b"
        clone = _make_clone(tmp_path, repo)
        provider = _fail_tool_refreshes(clone, repo, 3)
        assert provider.calls == 3

        ok_provider = MagicMock()
        ok_provider.get_token.return_value = "token"
        # Start well ahead of the failure timestamps so a debounce window
        # recorded by the implementation cannot swallow the recovery refresh.
        with _advancing_time(start=1_000_000):
            with _clone_env(
                clone,
                repo=repo,
                provider=ok_provider,
                refresh_return="deadbeef",
                interval=0,
            ):
                result = read_file(path=KNOWN_FILE, repo=repo)
        assert KNOWN_TOKEN in result["content"]

        with _status_env(repo):
            record = status(repo=repo)["repos"][repo]

        assert record["tool_refresh_consecutive_failures"] == 0
        assert not any(
            "tool" in warning.lower() and "refresh" in warning.lower()
            for warning in record["warnings"]
        )

    def test_background_sync_success_does_not_clear_tool_failures(self, tmp_path):
        repo = "owner/phase1-status-c"
        clone = _make_clone(tmp_path, repo)
        provider = _fail_tool_refreshes(clone, repo, 2)
        assert provider.calls == 2

        sync_result = _run_successful_background_sync(repo)
        assert sync_result["status"] == "ok"

        with _status_env(repo):
            record = status(repo=repo)["repos"][repo]

        assert record["tool_refresh_consecutive_failures"] == 2

    def test_tool_refresh_failures_do_not_change_sync_consecutive_failures(self, tmp_path):
        repo = "owner/phase1-status-d"
        clone = _make_clone(tmp_path, repo)
        _fail_tool_refreshes(clone, repo, 2)

        failing_sync = _sync_run(consecutive_failures=4, last_error="sync boom")
        with _status_env(repo, sync_run=failing_sync):
            record = status(repo=repo)["repos"][repo]

        assert record["consecutive_failures"] == 4
        assert any("4 consecutive sync failures" in warning for warning in record["warnings"])
        assert record["tool_refresh_consecutive_failures"] == 2

    def test_aggregate_tool_refresh_failing_count_and_recovery(self, tmp_path):
        """Spec 2: unhealthy_counts.tool_refresh_failing counts repos with failures > 0.

        N repos with tool failures => tool_refresh_failing == N, failing unchanged.
        One actual successful tool refresh clears that repo => N - 1.
        Sync failures provide a nonzero independent baseline.
        """
        repo_t1 = "owner/phase1-tool-fail-1"
        repo_t2 = "owner/phase1-tool-fail-2"
        repo_sync = "owner/phase1-sync-fail"

        clone1 = _make_clone(tmp_path, repo_t1)
        clone2 = _make_clone(tmp_path, repo_t2)
        _fail_tool_refreshes(clone1, repo_t1, 2)
        _fail_tool_refreshes(clone2, repo_t2, 1)

        failing_sync = _sync_run(consecutive_failures=3, last_error="sync failed")
        repos = [repo_t1, repo_t2, repo_sync]
        sync_runs = {repo_sync: failing_sync}

        with _status_env(repo_sync, sync_runs=sync_runs, repos=repos):
            agg = status()

        summary = agg["summary"]
        counts = summary["unhealthy_counts"]
        # N=2 repos have tool refresh failures
        assert counts["tool_refresh_failing"] == 2
        # Sync failing count is strictly sync consecutive_failures > 0, unaffected by tool failures
        assert counts["failing"] == 1

        # Recover one repo via real tool refresh
        ok_provider = MagicMock()
        ok_provider.get_token.return_value = "token"
        with _advancing_time(start=2_000_000):
            with _clone_env(
                clone1,
                repo=repo_t1,
                provider=ok_provider,
                refresh_return="recovered1",
                interval=0,
            ):
                res = read_file(path=KNOWN_FILE, repo=repo_t1)
        assert KNOWN_TOKEN in res["content"]

        with _status_env(repo_sync, sync_runs=sync_runs, repos=repos):
            agg_after = status()

        counts_after = agg_after["summary"]["unhealthy_counts"]
        # One repo cleared, now N - 1 = 1
        assert counts_after["tool_refresh_failing"] == 1
        # Sync failing count remains 1
        assert counts_after["failing"] == 1


# ---------------------------------------------------------------------------
# Criterion 6: report and list_tree expose stale metadata on failed refresh
# ---------------------------------------------------------------------------


class TestReportStaleMetadata:
    def test_report_exposes_stale_metadata_on_failed_refresh(self, tmp_path):
        repo = "owner/phase1-report-stale"
        clone = _make_clone(tmp_path, repo)

        # module_tree uses ctags (present in the test image); the stats
        # template shells out to tokei, which is not installed here.
        with _clone_env(clone, repo=repo, provider=RaisingTokenProvider()):
            result = report(template="module_tree", repo=repo)

        assert result["repo"] == repo
        assert result["template"] == "module_tree"
        assert result["markdown"]
        assert result["stale"] is True
        _assert_short_refresh_error(result["refresh_error"], context="report")


class TestListTreeStaleMetadata:
    def test_list_tree_exposes_stale_metadata_on_failed_refresh(self, tmp_path):
        repo = "owner/phase1-list-tree-stale"
        clone = _make_clone(tmp_path, repo)
        (clone / "code.py").write_text("def hello(): pass\n", encoding="utf-8")

        with _clone_env(clone, repo=repo, provider=RaisingTokenProvider()):
            result = list_tree(repo=repo, source_type="code")

        assert isinstance(result, dict)
        assert result["stale"] is True
        _assert_short_refresh_error(result["refresh_error"], context="list_tree")
        assert isinstance(result["entries"], list)
        paths = [e["path"] for e in result["entries"]]
        assert "code.py" in paths

    def test_list_tree_omits_stale_metadata_on_successful_refresh(self, tmp_path):
        repo = "owner/phase1-list-tree-ok"
        clone = _make_clone(tmp_path, repo)
        (clone / "code.py").write_text("def hello(): pass\n", encoding="utf-8")
        ok_provider = MagicMock()
        ok_provider.get_token.return_value = "token"

        with _clone_env(clone, repo=repo, provider=ok_provider, refresh_return="abc", interval=0):
            result = list_tree(repo=repo, source_type="code")

        assert isinstance(result, dict)
        assert "stale" not in result
        assert "refresh_error" not in result
        assert isinstance(result["entries"], list)
        paths = [e["path"] for e in result["entries"]]
        assert "code.py" in paths
