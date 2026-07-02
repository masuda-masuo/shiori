"""Unit tests for _do_sync allowlist validation and ingest rebuild guard (issue #63)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from shiori.mcp_server import _do_sync, ingest


# ===================================================================
# _do_sync: allowlist 検証
# ===================================================================


class TestDoSyncAllowlist:
    """_do_sync repo argument allowlist validation.

    Validation runs before lock acquisition, so tests need only mock settings.
    """

    def test_valid_repo_passes_validation(self):
        """Valid repo in settings.repos passes validation (lock failure later does not raise ValueError)."""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
        ):
            mock_settings.repos = ["owner/repo", "owner2/repo2"]
            mock_lock.acquire.return_value = False  # ロック取得失敗 → skipped

            result = _do_sync(repos=["owner/repo"])
            assert result["status"] == "skipped"
            assert result["reason"] == "同期が既に実行中です"

    def test_invalid_repo_raises_value_error(self):
        """Invalid repo not in settings.repos raises ValueError."""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.repos = ["owner/repo"]

            with pytest.raises(ValueError, match="SHIORI_REPOS"):
                _do_sync(repos=["evil/repo"])

    def test_partially_invalid_raises(self):
        """ValueError even when only some repos are invalid."""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.repos = ["owner/repo"]

            with pytest.raises(ValueError, match="SHIORI_REPOS"):
                _do_sync(repos=["owner/repo", "evil/repo"])

    def test_repos_none_skips_validation(self):
        """repos=None skips validation (uses settings.repos)."""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
        ):
            mock_settings.repos = ["owner/repo"]
            mock_lock.acquire.return_value = False

            result = _do_sync(repos=None)
            assert result["status"] == "skipped"  # 検証通過、ロックで skip

    def test_error_message_includes_invalid_repo(self):
        """Error message includes invalid repo names."""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.repos = ["owner/repo"]

            with pytest.raises(ValueError, match="evil/repo"):
                _do_sync(repos=["evil/repo"])

    def test_multiple_invalid_in_error_message(self):
        """Multiple invalid repo names appear in error message."""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.repos = ["owner/repo"]

            with pytest.raises(ValueError) as exc_info:
                _do_sync(repos=["evil1/repo", "evil2/repo"])
            msg = str(exc_info.value)
            assert "evil1/repo" in msg
            assert "evil2/repo" in msg

    def test_empty_repos_list_is_valid(self):
        """Empty list is a subset of settings.repos so validation passes."""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
        ):
            mock_settings.repos = ["owner/repo"]
            mock_lock.acquire.return_value = False

            result = _do_sync(repos=[])
            assert result["status"] == "skipped"


# ===================================================================
# ingest: rebuild ガード
# ===================================================================


class TestIngestRebuildGuard:
    """shiori_ingest rebuild=True guard.

    Guard runs before _do_sync call, so mock settings and _do_sync suffice.
    """

    def test_rebuild_true_blocked_by_default(self):
        """rebuild=True raises ValueError when allow_rebuild=False (default)."""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._do_sync") as mock_do_sync,
        ):
            mock_settings.allow_rebuild = False
            mock_settings.repos = ["test/repo"]
            mock_do_sync.return_value = {"status": "ok", "repos": {}}

            with pytest.raises(ValueError, match="rebuild=True"):
                ingest(rebuild=True, repo="test/repo")

            mock_do_sync.assert_not_called()

    def test_rebuild_true_allowed_when_env_set(self):
        """rebuild=True is allowed when allow_rebuild=True."""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._do_sync") as mock_do_sync,
        ):
            mock_settings.allow_rebuild = True
            mock_settings.repos = ["test/repo"]
            mock_do_sync.return_value = {"status": "ok", "repos": {}}

            result = ingest(rebuild=True, repo="test/repo")
            assert result == {"status": "ok", "repos": {}}
            mock_do_sync.assert_called_once_with(
                repos=["test/repo"], rebuild=True, route="mcp"
            )

    def test_rebuild_false_always_allowed(self):
        """rebuild=False is always allowed regardless of allow_rebuild."""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._do_sync") as mock_do_sync,
        ):
            mock_settings.allow_rebuild = False
            mock_settings.repos = ["test/repo"]
            mock_do_sync.return_value = {"status": "ok", "repos": {}}

            result = ingest(rebuild=False, repo="test/repo")
            assert result == {"status": "ok", "repos": {}}
            mock_do_sync.assert_called_once_with(
                repos=["test/repo"], rebuild=False, route="mcp"
            )

    def test_rebuild_false_with_repo_none(self):
        """rebuild=False with repo=None is also allowed."""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._do_sync") as mock_do_sync,
        ):
            mock_settings.allow_rebuild = False
            mock_settings.repos = ["test/repo"]
            mock_do_sync.return_value = {"status": "ok", "repos": {}}

            result = ingest(rebuild=False)
            assert result == {"status": "ok", "repos": {}}
            mock_do_sync.assert_called_once_with(
                repos=None, rebuild=False, route="mcp"
            )

    def test_error_message_mentions_env_var(self):
        """Error message mentions env var name and CLI alternative."""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._do_sync"),
        ):
            mock_settings.allow_rebuild = False
            mock_settings.repos = ["test/repo"]

            with pytest.raises(ValueError) as exc_info:
                ingest(rebuild=True)
            msg = str(exc_info.value)
            assert "SHIORI_ALLOW_REBUILD" in msg
            assert "python -m shiori ingest --rebuild" in msg
