"""Unit tests for _do_sync allowlist validation and ingest rebuild guard (issue #63)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from shiori.mcp_server import _do_sync, ingest


# ===================================================================
# _do_sync: allowlist validation
# ===================================================================


class TestDoSyncAllowlist:
    """_do_sync repo argument allowlist validation.

    Allowlist validation runs before lock acquisition, so only settings mock is needed.
    """

    def test_valid_repo_passes_validation(self):
        """repo in settings.repos passes validation (failure after lock does not raise ValueError)."""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._sync_lock") as mock_lock,
        ):
            mock_settings.repos = ["owner/repo", "owner2/repo2"]
            mock_lock.acquire.return_value = False  # lock acquisition failed → skipped

            result = _do_sync(repos=["owner/repo"])
            assert result["status"] == "skipped"
            assert result["reason"] == "sync already running"

    def test_invalid_repo_raises_value_error(self):
        """repo not in settings.repos raises ValueError."""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.repos = ["owner/repo"]

            with pytest.raises(ValueError, match="SHIORI_REPOS"):
                _do_sync(repos=["evil/repo"])

    def test_partially_invalid_raises(self):
        """Raises ValueError even if only some repos are invalid."""
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
            assert result["status"] == "skipped"  # validation passed, skipped by lock

    def test_error_message_includes_invalid_repo(self):
        """エラーメッセージに無効な repo 名が含まれる。"""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.repos = ["owner/repo"]

            with pytest.raises(ValueError, match="evil/repo"):
                _do_sync(repos=["evil/repo"])

    def test_multiple_invalid_in_error_message(self):
        """複数の無効な repo がエラーメッセージに含まれる。"""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.repos = ["owner/repo"]

            with pytest.raises(ValueError) as exc_info:
                _do_sync(repos=["evil1/repo", "evil2/repo"])
            msg = str(exc_info.value)
            assert "evil1/repo" in msg
            assert "evil2/repo" in msg

    def test_empty_repos_list_is_valid(self):
        """空リストは settings.repos の部分集合なので検証通過。"""
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
    """shiori_ingest の rebuild=True ガード。

    ガードは _do_sync 呼び出し前に行われるため、settings と _do_sync のモックでテスト可能。
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
        """allow_rebuild=True では rebuild=True が許可される。"""
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
        """rebuild=False は allow_rebuild の値に関わらず許可される。"""
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
        """rebuild=False, repo=None も許可される。"""
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
        """エラーメッセージが環境変数名と CLI 代替手段を示している。"""
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
