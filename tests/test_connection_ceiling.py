"""Connection ceiling for one ingest process (issue #375).

The fetch phase runs up to ``settings.fetch_concurrency`` repos in
parallel, and each repo's PR-review pass spawns up to
``MAX_PR_REVIEW_WORKERS`` workers, each on its own connection.  Without a
global bound on that inner level the worst case is a product
(``fetch_concurrency * (1 + MAX_PR_REVIEW_WORKERS) + 1`` = 45 with the
defaults); with ``PR_REVIEW_CONNECTION_LIMIT`` it is a sum
(``1 + fetch_concurrency + PR_REVIEW_CONNECTION_LIMIT`` = 15).

These tests drive the real threaded code path (``ingest.run_fetch`` with a
real ``fetch_issues`` / ``_fetch_pr_reviews_parallel`` / ``repo_lock``)
against a fake ``db.connect`` that counts live connections under a lock and
assert on the observed high-water mark: the ceiling is proven by
measurement, not by reading source or asserting on a constant.
"""

from __future__ import annotations

import threading
import time
from typing import Self
from unittest.mock import MagicMock, patch

import pytest

from shiori.config import Settings
from shiori.sync_issues import PR_REVIEW_CONNECTION_LIMIT


class _ConnectionCounter:
    """Fake ``db.connect``: counts live connections under a lock.

    ``open`` increments the live count, tracks the high-water mark, and
    then holds the "connection" open briefly so that concurrent opens
    overlap -- a fast fake would under-count the peak and the ceiling test
    would prove nothing.  The returned connection's ``close`` decrements.
    """

    def __init__(self, hold: float = 0.05) -> None:
        self._lock = threading.Lock()
        self._hold = hold
        self.live = 0
        self.high_water = 0

    def open(self, settings) -> MagicMock:
        with self._lock:
            self.live += 1
            self.high_water = max(self.high_water, self.live)
        time.sleep(self._hold)
        conn = MagicMock()
        conn.close = lambda: self.close()
        return conn

    def close(self) -> None:
        with self._lock:
            self.live -= 1


class _FakeClient:
    """Context-manager stand-in for httpx.Client (hermetic; no network)."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _ExplodingClient:
    """httpx.Client stand-in whose construction raises (worker failure path)."""

    def __init__(self, *args, **kwargs) -> None:
        raise RuntimeError("boom")


def _make_settings(repos: list[str]) -> Settings:
    return Settings(
        repos=repos,
        dev_repos=set(repos),
        data_dir="/tmp/data",
        fetch_concurrency=len(repos),
    )


def _body_page(pr_count: int) -> list[dict]:
    """One /issues page with ``pr_count`` PRs (and no plain issues)."""
    page = []
    for i in range(pr_count):
        page.append({
            "number": i + 1,
            "title": f"PR {i + 1}",
            "body": f"body{i + 1}",
            "user": {"login": "user", "type": "User"},
            "state": "open",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": f"2024-01-01T{i + 1:02d}:00:00Z",
            "html_url": f"https://github.com/owner/repo/issues/{i + 1}",
            "pull_request": {},
        })
    return page


def _run_fetch(
    settings: Settings,
    counter: _ConnectionCounter,
    *,
    client_factory=_FakeClient,
    prs_per_repo: int = 10,
) -> None:
    """Drive ``ingest.run_fetch`` through the real threaded code path.

    Real pieces kept real: ``run_fetch``'s executor and per-repo thread
    (``_fetch_one`` + ``repo_lock`` advisory lock), ``fetch_issues`` and
    ``_fetch_pr_reviews_parallel`` including the per-repo worker pools and
    their ``db.connect`` calls.  Everything that would touch the network or
    the filesystem is mocked.
    """
    from shiori.ingest import run_fetch

    with (
        patch("shiori.ingest.db.connect", side_effect=counter.open),
        patch("shiori.ingest.build_token_provider", return_value=MagicMock()),
        patch("shiori.ingest.schema.migrate_light"),
        patch("shiori.ingest.fetch_docs", return_value=None),
        patch(
            "shiori.sync_issues._api_pages_gen",
            side_effect=[[_body_page(prs_per_repo)], [], []] * len(settings.repos),
        ),
        patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z"),
        patch("shiori.sync_issues.set_cursor"),
        patch("shiori.sync_issues._upsert_issue_item"),
        patch("shiori.sync_issues._sync_pr_reviews"),
        patch("shiori.sync_issues.httpx.Client", client_factory),
    ):
        run_fetch(settings=settings)


@pytest.mark.parametrize("n_repos", [4, 8])
def test_ceiling_holds_under_worst_case_nesting(n_repos):
    """Worst-case nesting (n_repos repos x 10 PRs each): the observed peak
    never exceeds 1 + fetch_concurrency + PR_REVIEW_CONNECTION_LIMIT.

    Without the global inner cap the naive peak is
    fetch_concurrency * (1 + MAX_PR_REVIEW_WORKERS) + 1 (45 with the
    defaults), so this test fails on the unmodified code -- and raising
    fetch_concurrency cannot inflate the inner level (8-repo case).
    """
    repos = [f"owner/repo{i}" for i in range(n_repos)]
    settings = _make_settings(repos)  # fetch_concurrency = len(repos)
    counter = _ConnectionCounter()

    _run_fetch(settings, counter)

    ceiling = 1 + settings.fetch_concurrency + PR_REVIEW_CONNECTION_LIMIT
    assert counter.high_water <= ceiling, (
        f"observed {counter.high_water} live connections, ceiling is {ceiling}"
    )
    # The ceiling must actually bind: the inner level alone reaches
    # PR_REVIEW_CONNECTION_LIMIT concurrent connections on top of the
    # fetch_concurrency repo-thread connections.  With 10 PRs per repo the
    # uncapped inner level would open n_repos*10 connections at once, so a
    # low observed peak would mean the nesting never materialised and the
    # test would prove nothing.
    assert (
        counter.high_water
        >= settings.fetch_concurrency + PR_REVIEW_CONNECTION_LIMIT
    ), f"observed {counter.high_water} connections; the cap never bound"
    assert counter.live == 0


def test_steady_state_path_opens_no_inner_connections():
    """A run with no PRs (the daily steady state) never enters the inner
    level: exactly pre-flight + fetch_concurrency repo-thread connections
    are opened, never more -- the steady-state path is untouched."""
    repos = [f"owner/repo{i}" for i in range(4)]
    settings = _make_settings(repos)
    counter = _ConnectionCounter()

    _run_fetch(settings, counter, prs_per_repo=0)

    assert counter.high_water == settings.fetch_concurrency
    assert counter.live == 0


def test_permits_returned_when_worker_raises():
    """A worker that raises inside its slot still returns the permit.

    The release is structural (the slot wraps the whole worker body), so an
    exception on the work path cannot leak a permit.  Leaked permits are
    observable: after an exploding run, a follow-up happy run could reach
    at most PR_REVIEW_CONNECTION_LIMIT - 1 concurrent inner connections.
    """
    from shiori.sync_issues import _fetch_pr_reviews_parallel

    settings = _make_settings(["owner/repo0"])
    pr_numbers = list(range(1, 41))  # 40 PRs -> 10 workers, 4 PRs each

    # Every worker raises right after opening its connection.
    exploding = _ConnectionCounter()
    with (
        patch("shiori.sync_issues.db.connect", side_effect=exploding.open),
        patch("shiori.sync_issues.httpx.Client", _ExplodingClient),
    ):
        result = _fetch_pr_reviews_parallel(
            settings, "owner/repo0", MagicMock(), pr_numbers,
        )

    assert result == 0  # every worker raised before doing any work
    assert exploding.high_water == PR_REVIEW_CONNECTION_LIMIT  # slots were held
    assert exploding.live == 0

    # Happy path afterwards: the full ceiling must be reachable again, i.e.
    # all PR_REVIEW_CONNECTION_LIMIT permits are available.
    happy = _ConnectionCounter()
    with (
        patch("shiori.sync_issues.db.connect", side_effect=happy.open),
        patch("shiori.sync_issues.httpx.Client", _FakeClient),
        patch("shiori.sync_issues._sync_pr_reviews"),
    ):
        _fetch_pr_reviews_parallel(
            settings, "owner/repo0", MagicMock(), pr_numbers,
        )

    assert happy.high_water == PR_REVIEW_CONNECTION_LIMIT
    assert happy.live == 0
