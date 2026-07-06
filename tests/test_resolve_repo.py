"""Unit tests for _resolve_repo (issue #93)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from shiori.mcp_server import _infer_repo_from_cwd, _resolve_repo, _resolve_repos


class TestResolveRepo:
    """Behavior of _resolve_repo for default repo resolution."""

    def test_explicit_repo(self):
        """Explicit repo string is returned as-is."""
        assert _resolve_repo("owner/repo") == "owner/repo"

    def test_single_repo_default(self, monkeypatch):
        """Single configured repo is returned when repo=None."""
        monkeypatch.setattr("shiori.mcp_server.settings.repos", ["o/r"])
        assert _resolve_repo(None) == "o/r"

    def test_multiple_repo_first_fallback(self, monkeypatch):
        """First repo is returned when cwd inference fails."""
        monkeypatch.setattr("shiori.mcp_server.settings.repos", ["o/r1", "o/r2"])
        with patch("shiori.mcp_server._infer_repo_from_cwd", return_value=None):
            assert _resolve_repo(None) == "o/r1"

    def test_infer_from_cwd_matches(self, monkeypatch):
        """Repo inferred from cwd is returned when it matches."""
        monkeypatch.setattr("shiori.mcp_server.settings.repos", ["o/r1", "o/r2"])
        with patch("shiori.mcp_server._infer_repo_from_cwd", return_value="o/r2"):
            assert _resolve_repo(None) == "o/r2"

    def test_no_repos_raises(self, monkeypatch):
        """ValueError raised when SHIORI_REPOS is empty."""
        monkeypatch.setattr("shiori.mcp_server.settings.repos", [])
        with pytest.raises(ValueError, match="SHIORI_REPOS not set"):
            _resolve_repo(None)


class TestInferRepoFromCwd:
    """Behavior of _infer_repo_from_cwd."""

    def _fake_run(self, stdout: str):
        """Build a fake subprocess.run that returns given stdout."""
        def fake_run(*args, **kwargs):
            import subprocess
            return subprocess.CompletedProcess(
                args[0], 0, stdout=stdout, stderr=""
            )
        return fake_run

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setattr("shiori.mcp_server.settings.repos", ["owner/repo"])

    def test_ssh_url(self, monkeypatch):
        """SSH remote URL is parsed correctly."""
        with patch("shiori.mcp_server.subprocess.run", side_effect=self._fake_run(
            "git@github.com:owner/repo.git\n"
        )):
            assert _infer_repo_from_cwd() == "owner/repo"

    def test_https_url_with_git(self, monkeypatch):
        """HTTPS remote URL with .git suffix is parsed correctly."""
        with patch("shiori.mcp_server.subprocess.run", side_effect=self._fake_run(
            "https://github.com/owner/repo.git\n"
        )):
            assert _infer_repo_from_cwd() == "owner/repo"

    def test_https_url_without_git(self, monkeypatch):
        """HTTPS remote URL without .git suffix is parsed correctly."""
        with patch("shiori.mcp_server.subprocess.run", side_effect=self._fake_run(
            "https://github.com/owner/repo\n"
        )):
            assert _infer_repo_from_cwd() == "owner/repo"

    def test_not_github_remote(self, monkeypatch):
        """Non-GitHub remote returns None."""
        with patch("shiori.mcp_server.subprocess.run", side_effect=self._fake_run(
            "git@gitlab.com:owner/repo.git\n"
        )):
            assert _infer_repo_from_cwd() is None

    def test_not_in_repos(self, monkeypatch):
        """Remote that doesn't match any configured repo returns None."""
        with patch("shiori.mcp_server.subprocess.run", side_effect=self._fake_run(
            "https://github.com/other/repo.git\n"
        )):
            assert _infer_repo_from_cwd() is None

    def test_git_not_found(self, monkeypatch):
        """FileNotFoundError (git not installed) returns None gracefully."""
        with patch("shiori.mcp_server.subprocess.run", side_effect=FileNotFoundError):
            assert _infer_repo_from_cwd() is None


class TestResolveRepos:
    """Behavior of _resolve_repos for multi-repo resolution (issue #151)."""

    def test_wildcard_returns_all(self, monkeypatch):
        """repo='*' returns all configured repos."""
        monkeypatch.setattr("shiori.mcp_server.settings.repos", ["o/r1", "o/r2"])
        assert _resolve_repos("*") == ["o/r1", "o/r2"]

    def test_wildcard_empty_repos_raises(self, monkeypatch):
        """repo='*' with empty SHIORI_REPOS raises ValueError."""
        monkeypatch.setattr("shiori.mcp_server.settings.repos", [])
        with pytest.raises(ValueError, match="SHIORI_REPOS not set"):
            _resolve_repos("*")

    def test_explicit_repo(self, monkeypatch):
        """Explicit repo string is returned as a single-element list."""
        monkeypatch.setattr("shiori.mcp_server.settings.repos", ["o/r"])
        assert _resolve_repos("owner/repo") == ["owner/repo"]

    def test_none_delegates_to_resolve_repo(self, monkeypatch):
        """repo=None delegates to _resolve_repo for backward compat."""
        monkeypatch.setattr("shiori.mcp_server.settings.repos", ["o/r"])
        assert _resolve_repos(None) == ["o/r"]

    def test_wildcard_copies_list(self, monkeypatch):
        """repo='*' returns a copy, not the original list."""
        monkeypatch.setattr("shiori.mcp_server.settings.repos", ["o/r"])
        result = _resolve_repos("*")
        result.append("x/y")
        assert "x/y" not in _resolve_repos("*")
