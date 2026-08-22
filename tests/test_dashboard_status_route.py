"""Tests for the /api/status dashboard endpoint."""

from __future__ import annotations

from unittest.mock import patch

from starlette.testclient import TestClient

import shiori.mcp_server  # noqa: F401 - ensures dashboard routes are registered
from shiori.tools.registry import mcp


def test_api_status_success_no_repo():
    app = mcp.streamable_http_app()
    client = TestClient(app)
    fake_status = {
        "repos": {},
        "clone_refresh_debounce_seconds": 300,
        "sync_intervals": {"dev": 900, "ref": 86400},
        "token_provider": "static",
        "summary": {
            "total_repos": 10,
            "healthy_repos": 10,
            "unhealthy_repos": 0,
            "unhealthy_counts": {
                "with_warnings": 0,
                "index_stale": 0,
                "never_indexed": 0,
                "failing": 0,
                "with_pending": 0,
            },
            "pending_total": 0,
            "oldest_repo": None,
            "omitted_repos": 0,
            "omitted_repo_names": [],
            "chunk_counts_source": "cached",
        },
    }
    with patch("shiori.tools.status.status", return_value=fake_status) as mock_status:
        res = client.get("/api/status")
        assert res.status_code == 200
        assert res.json() == fake_status
        mock_status.assert_called_once_with(repo=None)


def test_api_status_success_with_repo():
    app = mcp.streamable_http_app()
    client = TestClient(app)
    fake_status = {
        "repos": {"owner/repo": {"consecutive_failures": 0}},
        "clone_refresh_debounce_seconds": 300,
        "sync_intervals": {"dev": 900, "ref": 86400},
        "token_provider": "static",
    }
    with patch("shiori.tools.status.status", return_value=fake_status) as mock_status:
        res = client.get("/api/status?repo=owner/repo")
        assert res.status_code == 200
        assert res.json() == fake_status
        mock_status.assert_called_once_with(repo="owner/repo")


def test_api_status_value_error_returns_400():
    app = mcp.streamable_http_app()
    client = TestClient(app)
    with patch("shiori.tools.status.status", side_effect=ValueError("Unknown repository 'unknown/repo'")):
        res = client.get("/api/status?repo=unknown/repo")
        assert res.status_code == 400
        assert res.json() == {"detail": "Unknown repository 'unknown/repo'"}


def test_api_status_exception_returns_500_with_error_key():
    app = mcp.streamable_http_app()
    client = TestClient(app)
    with patch("shiori.tools.status.status", side_effect=RuntimeError("Database failure")):
        res = client.get("/api/status")
        assert res.status_code == 500
        assert res.json() == {"error": "Database failure"}
