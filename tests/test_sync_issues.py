"""Unit tests for sync_issues.fetch_issues (issue #314)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from shiori.config import Settings
from shiori.sync_issues import fetch_issues


def _make_settings(*, dev_repos: set[str] | None = None) -> Settings:
    return Settings(
        repos=["owner/repo"],
        dev_repos=dev_repos or set(),
        data_dir="/tmp/data",
    )


# Shared mock helpers ---------------------------------------------------


def _mock_conn() -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = MagicMock()
    return conn


def _mock_provider() -> MagicMock:
    return MagicMock()


def _body_item(number: int, **overrides) -> dict:
    """Build a minimal item dict as returned by the /issues endpoint."""
    item: dict = {
        "number": number,
        "title": f"Item {number}",
        "body": f"body{number}",
        "user": {"login": f"user{number}", "type": "User"},
        "state": "open",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": f"2024-01-01T{number:02d}:00:00Z",
        "html_url": f"https://github.com/owner/repo/issues/{number}",
    }
    item.update(overrides)
    return item


# Tests -----------------------------------------------------------------


class TestFetchIssuesPRCollectionFix:
    """Issue #314: PR numbers are collected during the body loop, not
    re-enumerated from an already-advanced cursor."""

    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues.set_cursor")
    @patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._fetch_pr_reviews_parallel")
    def test_prs_from_body_loop_are_fetched(
        self,
        mock_fetch: MagicMock,
        mock_upsert: MagicMock,
        mock_get_cursor: MagicMock,
        mock_set_cursor: MagicMock,
        mock_api_pages: MagicMock,
    ):
        """PRs returned by the body loop get their reviews collected via
        _fetch_pr_reviews_parallel — no second enumeration of /issues."""
        settings = _make_settings(dev_repos={"owner/repo"})
        conn = _mock_conn()
        provider = _mock_provider()

        # Body pages: page1 has a plain issue (#1) and a PR (#2);
        #             page2 has a PR (#3).
        page1 = [
            _body_item(1),
            _body_item(2, pull_request={}),
        ]
        page2 = [
            _body_item(3, pull_request={}),
        ]
        # Subsequent calls for issue-comments / pr-review-comments.
        mock_api_pages.side_effect = [
            [page1, page2],  # body — /issues (2 pages)
            [],               # issue comments — /issues/comments
            [],               # PR review comments — /pulls/comments
        ]
        mock_fetch.return_value = 2  # pretend 2 PRs had reviews fetched

        result = fetch_issues(settings, conn, "owner/repo", provider, skip_pr_reviews=False)

        # _fetch_pr_reviews_parallel must receive numbers [2, 3]
        mock_fetch.assert_called_once_with(
            settings, "owner/repo", provider, [2, 3],
        )
        # n_fetched = 3 body items + 2 review results = 5
        assert result == 5

    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues.set_cursor")
    @patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._fetch_pr_reviews_parallel")
    def test_issues_endpoint_called_once(
        self,
        mock_fetch: MagicMock,
        mock_upsert: MagicMock,
        mock_get_cursor: MagicMock,
        mock_set_cursor: MagicMock,
        mock_api_pages: MagicMock,
    ):
        """/issues is enumerated exactly once — body loop only."""
        settings = _make_settings(dev_repos={"owner/repo"})
        conn = _mock_conn()
        provider = _mock_provider()

        page = [_body_item(1), _body_item(2, pull_request={})]
        mock_api_pages.side_effect = [
            [page],  # body
            [],      # issue comments
            [],      # PR review comments
        ]
        mock_fetch.return_value = 1

        fetch_issues(settings, conn, "owner/repo", provider, skip_pr_reviews=False)

        # There must be exactly 3 _api_pages_gen calls
        assert mock_api_pages.call_count == 3, (
            f"Expected 3 calls (body, issue-comments, pr-review-comments); "
            f"got {mock_api_pages.call_count}"
        )
        # The first call is for /issues (body loop)
        first_url = mock_api_pages.call_args_list[0][0][1]
        assert "repos/owner/repo/issues" in first_url and "comments" not in first_url

        # The second call is for /issues/comments
        second_url = mock_api_pages.call_args_list[1][0][1]
        assert "issues/comments" in second_url

        # The third call is for /pulls/comments
        third_url = mock_api_pages.call_args_list[2][0][1]
        assert "pulls/comments" in third_url

    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues.set_cursor")
    @patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._fetch_pr_reviews_parallel")
    def test_no_prs_in_body_skips_review_fetch(
        self,
        mock_fetch: MagicMock,
        mock_upsert: MagicMock,
        mock_get_cursor: MagicMock,
        mock_set_cursor: MagicMock,
        mock_api_pages: MagicMock,
    ):
        """When the body loop yields no PRs, _fetch_pr_reviews_parallel is not
        called even when skip_pr_reviews=False."""
        settings = _make_settings(dev_repos={"owner/repo"})
        conn = _mock_conn()
        provider = _mock_provider()

        # Only plain issues, no PRs
        page = [_body_item(1)]
        mock_api_pages.side_effect = [
            [page],  # body
            [],      # issue comments
            [],      # PR review comments
        ]

        fetch_issues(settings, conn, "owner/repo", provider, skip_pr_reviews=False)

        mock_fetch.assert_not_called()


class TestFetchIssuesSkipPRReviewsGuard:
    """The 3-value skip_pr_reviews guard must behave identically before and
    after the #314 fix."""

    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues.set_cursor")
    @patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._fetch_pr_reviews_parallel")
    def test_skip_true_skips_reviews(
        self,
        mock_fetch: MagicMock,
        mock_upsert: MagicMock,
        mock_get_cursor: MagicMock,
        mock_set_cursor: MagicMock,
        mock_api_pages: MagicMock,
    ):
        """skip_pr_reviews=True → reviews are not fetched."""
        settings = _make_settings(dev_repos={"owner/repo"})  # dev repo
        conn = _mock_conn()
        provider = _mock_provider()

        page = [_body_item(1, pull_request={})]
        mock_api_pages.side_effect = [[page], [], []]

        fetch_issues(settings, conn, "owner/repo", provider, skip_pr_reviews=True)

        mock_fetch.assert_not_called()

    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues.set_cursor")
    @patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._fetch_pr_reviews_parallel")
    def test_skip_false_fetches_reviews(
        self,
        mock_fetch: MagicMock,
        mock_upsert: MagicMock,
        mock_get_cursor: MagicMock,
        mock_set_cursor: MagicMock,
        mock_api_pages: MagicMock,
    ):
        """skip_pr_reviews=False → reviews are fetched even for non-dev repos."""
        settings = _make_settings(dev_repos=set())  # NOT a dev repo
        conn = _mock_conn()
        provider = _mock_provider()

        page = [_body_item(1, pull_request={})]
        mock_api_pages.side_effect = [[page], [], []]
        mock_fetch.return_value = 1

        fetch_issues(settings, conn, "owner/repo", provider, skip_pr_reviews=False)

        mock_fetch.assert_called_once()

    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues.set_cursor")
    @patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._fetch_pr_reviews_parallel")
    def test_skip_none_dev_repo_fetches_reviews(
        self,
        mock_fetch: MagicMock,
        mock_upsert: MagicMock,
        mock_get_cursor: MagicMock,
        mock_set_cursor: MagicMock,
        mock_api_pages: MagicMock,
    ):
        """skip_pr_reviews=None + dev repo → reviews are fetched."""
        settings = _make_settings(dev_repos={"owner/repo"})
        conn = _mock_conn()
        provider = _mock_provider()

        page = [_body_item(1, pull_request={})]
        mock_api_pages.side_effect = [[page], [], []]
        mock_fetch.return_value = 1

        fetch_issues(settings, conn, "owner/repo", provider, skip_pr_reviews=None)

        mock_fetch.assert_called_once()

    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues.set_cursor")
    @patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._fetch_pr_reviews_parallel")
    def test_skip_none_non_dev_repo_skips_reviews(
        self,
        mock_fetch: MagicMock,
        mock_upsert: MagicMock,
        mock_get_cursor: MagicMock,
        mock_set_cursor: MagicMock,
        mock_api_pages: MagicMock,
    ):
        """skip_pr_reviews=None + non-dev repo → reviews are skipped."""
        settings = _make_settings(dev_repos=set())  # NOT a dev repo
        conn = _mock_conn()
        provider = _mock_provider()

        page = [_body_item(1, pull_request={})]
        mock_api_pages.side_effect = [[page], [], []]

        fetch_issues(settings, conn, "owner/repo", provider, skip_pr_reviews=None)

        mock_fetch.assert_not_called()

# Issue #323: httpx.Client follow_redirects regression tests ------------

class TestHttpxClientFollowRedirects:
    """Verify every httpx.Client(...) construction passes follow_redirects=True."""

    # --- _fetch_pr_reviews_parallel (line 262) ---

    @patch("shiori.sync_issues.db.connect")
    @patch("shiori.sync_issues._sync_pr_reviews")
    def test_fetch_pr_reviews_parallel_client_has_follow_redirects(
        self, mock_sync: MagicMock, mock_connect: MagicMock,
    ):
        """The _worker inside _fetch_pr_reviews_parallel creates httpx.Client
        with follow_redirects=True."""
        from shiori.sync_issues import _fetch_pr_reviews_parallel

        mock_connect.return_value = MagicMock()
        captured_kwargs: dict[str, object] = {}

        class _SpyClient:
            def __init__(self, **kwargs: object) -> None:
                captured_kwargs.update(kwargs)

            def __enter__(self) -> _SpyClient:
                return self

            def __exit__(self, *args: object) -> None:
                pass

        with patch("shiori.sync_issues.httpx.Client", _SpyClient):
            _fetch_pr_reviews_parallel(
                _make_settings(), "owner/repo", _mock_provider(), [1],
            )

        assert captured_kwargs.get("follow_redirects") is True, (
            f"Expected follow_redirects=True in _fetch_pr_reviews_parallel "
            f"but got {captured_kwargs}"
        )

    # --- fetch_issues (line 377) ---

    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues.set_cursor")
    @patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._fetch_pr_reviews_parallel")
    def test_fetch_issues_client_has_follow_redirects(
        self,
        mock_fetch: MagicMock,
        mock_upsert: MagicMock,
        mock_get_cursor: MagicMock,
        mock_set_cursor: MagicMock,
        mock_api_pages: MagicMock,
    ):
        """fetch_issues creates httpx.Client with follow_redirects=True."""
        settings = _make_settings(dev_repos={"owner/repo"})
        conn = _mock_conn()
        provider = _mock_provider()

        page = [_body_item(1)]
        mock_api_pages.side_effect = [[page], [], []]
        mock_fetch.return_value = 0

        captured_kwargs: dict[str, object] = {}

        class _SpyClient:
            def __init__(self, **kwargs: object) -> None:
                captured_kwargs.update(kwargs)

            def __enter__(self) -> _SpyClient:
                return self

            def __exit__(self, *args: object) -> None:
                pass

        with patch("shiori.sync_issues.httpx.Client", _SpyClient):
            fetch_issues(settings, conn, "owner/repo", provider)

        assert captured_kwargs.get("follow_redirects") is True, (
            f"Expected follow_redirects=True in fetch_issues "
            f"but got {captured_kwargs}"
        )


# Issue #324: 404 on main issues endpoint is handled gracefully ---------

class TestFetchIssuesNotFoundOK:
    """Issue #324: 404 on main /issues is handled like comments 404s."""

    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues.set_cursor")
    @patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._fetch_pr_reviews_parallel")
    def test_404_on_issues_returns_zero_and_warns(
        self,
        mock_fetch: MagicMock,
        mock_upsert: MagicMock,
        mock_get_cursor: MagicMock,
        mock_set_cursor: MagicMock,
        mock_api_pages: MagicMock,
    ):
        """404 on /issues → fetch_issues returns 0, no exception, warning logged."""
        settings = _make_settings()
        conn = _mock_conn()
        provider = _mock_provider()

        # First call (issues) returns empty iterable = 404 with not_found_ok
        # Second/third calls (comments) also return empty
        mock_api_pages.side_effect = [[], [], []]
        mock_fetch.return_value = 0

        with patch("shiori.sync_issues.log") as mock_log:
            result = fetch_issues(settings, conn, "owner/repo", provider)

        assert result == 0
        mock_upsert.assert_not_called()
        mock_log.warning.assert_called_once_with(
            "Issues API returned 404 for %s — issues disabled, skipping",
            "owner/repo",
        )

    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues.set_cursor")
    @patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._fetch_pr_reviews_parallel")
    def test_non_404_errors_still_raise(
        self,
        mock_fetch: MagicMock,
        mock_upsert: MagicMock,
        mock_get_cursor: MagicMock,
        mock_set_cursor: MagicMock,
        mock_api_pages: MagicMock,
    ):
        """Non-404 errors (403/500) on /issues still raise."""
        settings = _make_settings()
        conn = _mock_conn()
        provider = _mock_provider()

        resp = MagicMock()
        resp.status_code = 403
        error = httpx.HTTPStatusError(
            "403 Forbidden", request=MagicMock(), response=resp,
        )
        mock_api_pages.side_effect = [error, [], []]

        with pytest.raises(httpx.HTTPStatusError):
            fetch_issues(settings, conn, "owner/repo", provider)

    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues.set_cursor")
    @patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._fetch_pr_reviews_parallel")
    def test_normal_repos_unchanged(
        self,
        mock_fetch: MagicMock,
        mock_upsert: MagicMock,
        mock_get_cursor: MagicMock,
        mock_set_cursor: MagicMock,
        mock_api_pages: MagicMock,
    ):
        """Repos with working issues endpoints behave exactly as before."""
        settings = _make_settings()
        conn = _mock_conn()
        provider = _mock_provider()

        page = [_body_item(1)]
        mock_api_pages.side_effect = [[page], [], []]
        mock_fetch.return_value = 0

        result = fetch_issues(settings, conn, "owner/repo", provider)

        # Normal path: body item is upserted
        assert result == 1
        mock_upsert.assert_called_once()


# Issue #165: labels extracted from GitHub payload ----------------------

class TestLabelExtraction:
    """Labels are extracted from the GitHub API payload and stored in issue_items."""

    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues.set_cursor")
    @patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._fetch_pr_reviews_parallel")
    def test_labels_extracted_and_stored(
        self,
        mock_fetch: MagicMock,
        mock_upsert: MagicMock,
        mock_get_cursor: MagicMock,
        mock_set_cursor: MagicMock,
        mock_api_pages: MagicMock,
    ):
        """Labels from the GitHub payload are extracted and passed to _upsert_issue_item."""
        settings = _make_settings(dev_repos=set())
        conn = _mock_conn()
        provider = _mock_provider()

        labels_payload = [
            {"name": "bug", "color": "d73a4a"},
            {"name": "enhancement", "color": "a2eeef"},
        ]
        page = [
            _body_item(1, labels=labels_payload),
            _body_item(2),  # no labels
        ]
        mock_api_pages.side_effect = [[page], [], []]
        mock_fetch.return_value = 0

        fetch_issues(settings, conn, "owner/repo", provider)

        # First call: item with labels
        call1_row = mock_upsert.call_args_list[0][0][1]
        assert call1_row["labels"] == ["bug", "enhancement"]

        # Second call: item without labels
        call2_row = mock_upsert.call_args_list[1][0][1]
        assert call2_row["labels"] == []

    @patch("shiori.sync_issues._api_pages_gen")
    @patch("shiori.sync_issues.set_cursor")
    @patch("shiori.sync_issues.get_cursor", return_value="2023-01-01T00:00:00Z")
    @patch("shiori.sync_issues._upsert_issue_item")
    @patch("shiori.sync_issues._fetch_pr_reviews_parallel")
    def test_labels_empty_list_when_no_labels_key(
        self,
        mock_fetch: MagicMock,
        mock_upsert: MagicMock,
        mock_get_cursor: MagicMock,
        mock_set_cursor: MagicMock,
        mock_api_pages: MagicMock,
    ):
        """Items without a 'labels' key in the payload get an empty list."""
        settings = _make_settings(dev_repos=set())
        conn = _mock_conn()
        provider = _mock_provider()

        # Item without labels key at all — _body_item doesn't include it by default
        page = [_body_item(1)]
        assert "labels" not in page[0]

        mock_api_pages.side_effect = [[page], [], []]
        mock_fetch.return_value = 0

        fetch_issues(settings, conn, "owner/repo", provider)

        call_row = mock_upsert.call_args_list[0][0][1]
        assert call_row["labels"] == []


class TestUpsertLabelsKey:
    """Rows without a "labels" key (comments/reviews) must not break %(labels)s."""

    def test_upsert_supplies_missing_labels_key(self):
        from shiori.sync_issues import _upsert_issue_item

        conn = _mock_conn()
        row = {
            "repo": "o/r", "issue_no": 1, "comment_id": 42,
            "kind": "comment", "title": None, "author": "u",
            "is_bot": False, "state": "open", "path": None, "line": None,
            "body": "hi", "url": None,
            "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
        }
        _upsert_issue_item(conn, row)
        executed_row = conn.cursor.return_value.__enter__.return_value.execute.call_args[0][1]
        assert executed_row["labels"] is None


class TestDocsOnlyReposIngest:
    """Issue #441: SHIORI_DOCS_ONLY_REPOS skips issue fetching and indexing."""

    @patch("shiori.ingest.build_token_provider")
    @patch("shiori.ingest.fetch_issues")
    @patch("shiori.ingest.fetch_docs", return_value="abc123456789")
    @patch("shiori.ingest.repo_lock")
    @patch("shiori.ingest._should_skip_repo", return_value=False)
    @patch("shiori.ingest.schema.migrate_light")
    @patch("shiori.ingest.db.connect")
    def test_run_fetch_skips_issues_and_syncs_docs(
        self,
        mock_connect: MagicMock,
        mock_migrate: MagicMock,
        mock_skip: MagicMock,
        mock_lock: MagicMock,
        mock_fetch_docs: MagicMock,
        mock_fetch_issues: MagicMock,
        mock_provider: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ):
        from shiori.ingest import run_fetch

        mock_lock.return_value.__enter__.return_value = True
        conn = _mock_conn()
        mock_connect.return_value = conn
        settings = Settings(
            repos=["owner/docs-only-repo"],
            docs_only_repos={"owner/docs-only-repo"},
            data_dir="/tmp/data",
        )

        with caplog.at_level("INFO"):
            run_fetch(settings, repos=["owner/docs-only-repo"])

        assert mock_fetch_docs.called
        assert not mock_fetch_issues.called
        assert "fetch issues: skipping owner/docs-only-repo (docs-only repo)" in caplog.text

    @patch("shiori.ingest._is_bulk_path", return_value=False)
    @patch("shiori.ingest.Embedder")
    @patch("shiori.ingest.index_issues")
    @patch("shiori.ingest.index_docs", return_value=1)
    @patch("shiori.ingest.index_code", return_value=0)
    @patch("shiori.ingest.repo_lock")
    @patch("shiori.ingest.schema.migrate_light")
    @patch("shiori.ingest.db.connect")
    def test_run_index_skips_issues_and_syncs_docs(
        self,
        mock_connect: MagicMock,
        mock_migrate: MagicMock,
        mock_lock: MagicMock,
        mock_index_code: MagicMock,
        mock_index_docs: MagicMock,
        mock_index_issues: MagicMock,
        mock_embedder: MagicMock,
        mock_is_bulk: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ):
        from shiori.ingest import run_index

        mock_lock.return_value.__enter__.return_value = True
        conn = _mock_conn()
        mock_connect.return_value = conn
        settings = Settings(
            repos=["owner/docs-only-repo"],
            docs_only_repos={"owner/docs-only-repo"},
            data_dir="/tmp/data",
        )

        with caplog.at_level("INFO"):
            run_index(settings, repos=["owner/docs-only-repo"])

        assert mock_index_docs.called
        assert not mock_index_issues.called
        assert "index issues: skipping owner/docs-only-repo (docs-only repo)" in caplog.text

    @patch("shiori.ingest._is_bulk_path", return_value=False)
    @patch("shiori.ingest.Embedder")
    @patch("shiori.ingest.index_issues")
    @patch("shiori.ingest.index_code", return_value=2)
    @patch("shiori.ingest.index_docs", return_value=1)
    @patch("shiori.ingest.repo_lock")
    @patch("shiori.ingest.schema.migrate_light")
    @patch("shiori.ingest.db.connect")
    def test_dev_repos_independence_both_dev_and_docs_only(
        self,
        mock_connect: MagicMock,
        mock_migrate: MagicMock,
        mock_lock: MagicMock,
        mock_index_docs: MagicMock,
        mock_index_code: MagicMock,
        mock_index_issues: MagicMock,
        mock_embedder: MagicMock,
        mock_is_bulk: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ):
        from shiori.ingest import run_index

        mock_lock.return_value.__enter__.return_value = True
        conn = _mock_conn()
        mock_connect.return_value = conn
        settings = Settings(
            repos=["owner/dev-and-docs"],
            dev_repos={"owner/dev-and-docs"},
            docs_only_repos={"owner/dev-and-docs"},
            data_dir="/tmp/data",
        )

        with caplog.at_level("INFO"):
            run_index(settings, repos=["owner/dev-and-docs"])

        assert mock_index_docs.called
        assert mock_index_code.called
        assert not mock_index_issues.called
        assert "index issues: skipping owner/dev-and-docs (docs-only repo)" in caplog.text

    @patch("shiori.ingest._is_bulk_path", return_value=False)
    @patch("shiori.ingest.Embedder")
    @patch("shiori.ingest.index_issues")
    @patch("shiori.ingest.index_code")
    @patch("shiori.ingest.index_docs", return_value=1)
    @patch("shiori.ingest.repo_lock")
    @patch("shiori.ingest.schema.migrate_light")
    @patch("shiori.ingest.db.connect")
    def test_dev_repos_independence_docs_only_not_dev(
        self,
        mock_connect: MagicMock,
        mock_migrate: MagicMock,
        mock_lock: MagicMock,
        mock_index_docs: MagicMock,
        mock_index_code: MagicMock,
        mock_index_issues: MagicMock,
        mock_embedder: MagicMock,
        mock_is_bulk: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ):
        from shiori.ingest import run_index

        mock_lock.return_value.__enter__.return_value = True
        conn = _mock_conn()
        mock_connect.return_value = conn
        settings = Settings(
            repos=["owner/docs-not-dev"],
            dev_repos=set(),
            docs_only_repos={"owner/docs-not-dev"},
            data_dir="/tmp/data",
        )

        with caplog.at_level("INFO"):
            run_index(settings, repos=["owner/docs-not-dev"])

        assert mock_index_docs.called
        assert mock_index_code.called
        assert not mock_index_issues.called
        assert "index issues: skipping owner/docs-not-dev (docs-only repo)" in caplog.text

    @patch("shiori.ingest.build_token_provider")
    @patch("shiori.ingest.fetch_issues", return_value=5)
    @patch("shiori.ingest.fetch_docs", return_value="abc123456789")
    @patch("shiori.ingest.repo_lock")
    @patch("shiori.ingest._should_skip_repo", return_value=False)
    @patch("shiori.ingest.schema.migrate_light")
    @patch("shiori.ingest.db.connect")
    def test_repo_not_in_docs_only_repos_fetches_issues(
        self,
        mock_connect: MagicMock,
        mock_migrate: MagicMock,
        mock_skip: MagicMock,
        mock_lock: MagicMock,
        mock_fetch_docs: MagicMock,
        mock_fetch_issues: MagicMock,
        mock_provider: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ):
        from shiori.ingest import run_fetch

        mock_lock.return_value.__enter__.return_value = True
        conn = _mock_conn()
        mock_connect.return_value = conn
        settings = Settings(
            repos=["owner/normal-repo"],
            docs_only_repos={"owner/other-repo"},
            data_dir="/tmp/data",
        )

        with caplog.at_level("INFO"):
            run_fetch(settings, repos=["owner/normal-repo"])

        assert mock_fetch_docs.called
        assert mock_fetch_issues.called
        assert "fetch issues: 5 items fetched" in caplog.text
