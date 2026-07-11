"""Unit tests for _resolve_repo (issue #93)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from shiori.mcp_server import (
    _infer_repo_from_cwd,
    _resolve_repo,
    _resolve_repo_filter,
    _resolve_repos,
    _validate_repo_name,
)


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
        """Explicit configured repo string is returned as a single-element list.

        (Was ``settings.repos = ["o/r"]`` / ``_resolve_repos("owner/repo")``
        pre-#189, which relied on the since-fixed passthrough-without-
        validation bug; updated to use a repo that's actually configured.)
        """
        monkeypatch.setattr("shiori.mcp_server.settings.repos", ["owner/repo"])
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


class TestValidateRepoName:
    """Behavior of _validate_repo_name (issue #189)."""

    def test_full_name_exact_match(self, monkeypatch):
        """A full "owner/name" already in settings.repos is returned as-is."""
        monkeypatch.setattr(
            "shiori.mcp_server.settings.repos",
            ["masuda-masuo/shiori", "masuda-masuo/code-sandbox-mcp"],
        )
        assert _validate_repo_name("masuda-masuo/shiori") == "masuda-masuo/shiori"

    def test_unique_short_name_resolves(self, monkeypatch):
        """A short name that uniquely matches one configured repo resolves to it."""
        monkeypatch.setattr(
            "shiori.mcp_server.settings.repos",
            ["masuda-masuo/shiori", "masuda-masuo/code-sandbox-mcp"],
        )
        assert (
            _validate_repo_name("code-sandbox-mcp")
            == "masuda-masuo/code-sandbox-mcp"
        )

    def test_ambiguous_short_name_raises(self, monkeypatch):
        """A short name matching more than one configured repo is rejected
        with the ambiguous candidates listed."""
        monkeypatch.setattr(
            "shiori.mcp_server.settings.repos", ["org1/tools", "org2/tools"]
        )
        with pytest.raises(ValueError, match="ambiguous repo") as exc:
            _validate_repo_name("tools")
        assert "org1/tools" in str(exc.value)
        assert "org2/tools" in str(exc.value)

    def test_unknown_repo_lists_indexed_repos(self, monkeypatch):
        """An unresolvable repo name raises with the full indexed-repo list,
        distinct from a "not indexed" data error."""
        monkeypatch.setattr(
            "shiori.mcp_server.settings.repos",
            ["masuda-masuo/shiori", "masuda-masuo/code-sandbox-mcp"],
        )
        with pytest.raises(ValueError, match="unknown repo") as exc:
            _validate_repo_name("totally-bogus-repo-xyz")
        msg = str(exc.value)
        assert "totally-bogus-repo-xyz" in msg
        assert "masuda-masuo/shiori" in msg
        assert "masuda-masuo/code-sandbox-mcp" in msg

    def test_unknown_full_name_also_lists_indexed_repos(self, monkeypatch):
        """A well-formed "owner/name" not in settings.repos is unknown too."""
        monkeypatch.setattr("shiori.mcp_server.settings.repos", ["o/r1"])
        with pytest.raises(ValueError, match="unknown repo"):
            _validate_repo_name("o/other")

    def test_no_repos_configured_passthrough(self, monkeypatch):
        """With SHIORI_REPOS unset there's nothing to validate against, so
        the repo is returned unchanged (legacy behavior preserved)."""
        monkeypatch.setattr("shiori.mcp_server.settings.repos", [])
        assert _validate_repo_name("anything/goes") == "anything/goes"


class TestResolveRepoWithValidation:
    """_resolve_repo delegates explicit repo validation to _validate_repo_name."""

    def test_unique_short_name_resolves(self, monkeypatch):
        monkeypatch.setattr(
            "shiori.mcp_server.settings.repos",
            ["masuda-masuo/shiori", "masuda-masuo/code-sandbox-mcp"],
        )
        assert _resolve_repo("code-sandbox-mcp") == "masuda-masuo/code-sandbox-mcp"

    def test_unknown_repo_raises_with_indexed_list(self, monkeypatch):
        monkeypatch.setattr(
            "shiori.mcp_server.settings.repos",
            ["masuda-masuo/shiori", "masuda-masuo/code-sandbox-mcp"],
        )
        with pytest.raises(ValueError, match="unknown repo"):
            _resolve_repo("totally-bogus-repo-xyz")


class TestResolveRepoFilter:
    """Behavior of _resolve_repo_filter (issue #189, search-tool repo filter)."""

    def test_none_passes_through(self, monkeypatch):
        """None means "no filter, search all repos", unlike _resolve_repo."""
        monkeypatch.setattr("shiori.mcp_server.settings.repos", ["o/r1", "o/r2"])
        assert _resolve_repo_filter(None) is None

    def test_short_name_resolves(self, monkeypatch):
        monkeypatch.setattr(
            "shiori.mcp_server.settings.repos",
            ["masuda-masuo/shiori", "masuda-masuo/code-sandbox-mcp"],
        )
        assert (
            _resolve_repo_filter("code-sandbox-mcp")
            == "masuda-masuo/code-sandbox-mcp"
        )

    def test_unknown_repo_raises(self, monkeypatch):
        monkeypatch.setattr("shiori.mcp_server.settings.repos", ["o/r1"])
        with pytest.raises(ValueError, match="unknown repo"):
            _resolve_repo_filter("bogus")
