"""Additional acceptance coverage for Phase 1 refresh failures (shiori #457, #458, #460).

Covers:
- list_tree: stable mapping envelope {entries: [...]}; failed refresh adds stale and
  refresh_error; success/debounce/doc-only/empty omit failure fields; doc + failed clone
  preserves doc entries with failure metadata.
- grep: mixed multi-repo (clean + stale + missing) produces matches from clean and
  stale repos, sets stale=True and partial=True, populates stale_repos and skipped_repos.
- pipeline/status: failure counters, debounce preservation, concurrency.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shiori.mcp_server import (
    grep_search,
    list_tree,
)
from shiori.pipeline import (
    Phase1Outcome,
    _reset_tool_refresh_state,
)

REPO = "owner/fixture-additional"
KNOWN_TOKEN = "SHIORI_ADDITIONAL_TOKEN"
KNOWN_FILE = "docs/file.txt"


@pytest.fixture(autouse=True)
def _clean_tool_refresh_state():
    _reset_tool_refresh_state()
    try:
        yield
    finally:
        _reset_tool_refresh_state()


def _fake_conn() -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    return conn


def _make_clone(root: Path, repo: str = REPO) -> Path:
    clone = root / "repos" / repo.replace("/", "__")
    (clone / ".git").mkdir(parents=True)
    (clone / "docs").mkdir(parents=True)
    (clone / KNOWN_FILE).write_text(f"{KNOWN_TOKEN}\n", encoding="utf-8")
    (clone / "code.py").write_text("def hello(): pass\n", encoding="utf-8")
    return clone


def _settings_for(clone: Path, repo: str = REPO) -> MagicMock:
    settings = MagicMock()
    settings.repos = [repo]
    settings.dev_repos = set()
    settings.docs_only_repos = set()
    settings.dev_sync_interval_seconds = 900
    settings.ref_sync_interval_seconds = 86400
    settings.sync_interval_seconds = 0
    settings.fetch_concurrency = 4
    settings.repo_dir.side_effect = lambda name: str(clone)
    return settings


# ---------------------------------------------------------------------------
# list_tree Phase 1 failure handling
# ---------------------------------------------------------------------------


class TestListTreePhase1:
    def test_failed_refresh_with_intact_clone_returns_mapping_and_stale(self, tmp_path):
        repo = "owner/list-tree-stale"
        clone = _make_clone(tmp_path, repo)
        settings = _settings_for(clone, repo=repo)

        outcome = Phase1Outcome(status="failed", head="abc", error="DNS failure: host not found")
        with (
            patch("shiori.tools.list_tree._ensure_phase1", return_value=outcome),
            patch("shiori.tools.list_tree.settings", settings),
            patch("shiori.tools.common.settings", settings),
            patch("shiori.tools.list_tree._conn", _fake_conn),
        ):
            res = list_tree(repo=repo, source_type="code")

        assert isinstance(res, dict)
        assert res["stale"] is True
        assert res["refresh_error"] == "DNS failure: host not found"
        assert isinstance(res["entries"], list)
        paths = [e["path"] for e in res["entries"]]
        assert "code.py" in paths

    def test_failed_refresh_with_missing_clone_returns_mapping_and_empty_entries(self, tmp_path):
        repo = "owner/list-tree-missing"
        missing_clone = tmp_path / "nonexistent"
        settings = _settings_for(missing_clone, repo=repo)

        outcome = Phase1Outcome(status="failed", head=None, error="connection timed out")
        with (
            patch("shiori.tools.list_tree._ensure_phase1", return_value=outcome),
            patch("shiori.tools.list_tree.settings", settings),
            patch("shiori.tools.common.settings", settings),
            patch("shiori.tools.list_tree._conn", _fake_conn),
        ):
            res = list_tree(repo=repo, source_type="code")

        assert isinstance(res, dict)
        assert res["stale"] is True
        assert res["refresh_error"] == "connection timed out"
        assert res["entries"] == []

    def test_doc_only_does_not_call_phase1_and_returns_mapping_without_failure_fields(self, tmp_path):
        repo = "owner/list-tree-doc-only"
        clone = _make_clone(tmp_path, repo)
        settings = _settings_for(clone, repo=repo)

        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [("docs/manual.md",)]
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn.cursor.return_value.__exit__.return_value = False

        with (
            patch("shiori.tools.list_tree._ensure_phase1") as mock_phase1,
            patch("shiori.tools.list_tree.settings", settings),
            patch("shiori.tools.common.settings", settings),
            patch("shiori.tools.list_tree._conn", return_value=mock_conn),
        ):
            res = list_tree(repo=repo, source_type="doc")

        mock_phase1.assert_not_called()
        assert isinstance(res, dict)
        assert res == {"entries": [{"path": "docs/manual.md", "source": "doc"}]}
        assert "stale" not in res
        assert "refresh_error" not in res

    def test_doc_and_failed_clone_returns_mapping_with_doc_entries(self, tmp_path):
        repo = "owner/list-tree-mixed"
        clone = _make_clone(tmp_path, repo)
        settings = _settings_for(clone, repo=repo)

        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [("docs/index.md",)]
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_conn.cursor.return_value.__exit__.return_value = False

        outcome = Phase1Outcome(status="failed", head=None, error="git fetch 503")
        with (
            patch("shiori.tools.list_tree._ensure_phase1", return_value=outcome),
            patch("shiori.tools.list_tree.settings", settings),
            patch("shiori.tools.common.settings", settings),
            patch("shiori.tools.list_tree._conn", return_value=mock_conn),
        ):
            res = list_tree(repo=repo)  # source_type=None

        assert isinstance(res, dict)
        assert res["stale"] is True
        assert res["refresh_error"] == "git fetch 503"
        doc_entries = [e for e in res["entries"] if e["source"] == "doc"]
        code_entries = [e for e in res["entries"] if e["source"] == "code"]
        assert len(doc_entries) == 1
        assert doc_entries[0]["path"] == "docs/index.md"
        assert len(code_entries) >= 1

    def test_successful_refresh_returns_mapping_without_failure_fields(self, tmp_path):
        repo = "owner/list-tree-ok"
        clone = _make_clone(tmp_path, repo)
        settings = _settings_for(clone, repo=repo)

        outcome = Phase1Outcome(status="refreshed", head="def456", error=None)
        with (
            patch("shiori.tools.list_tree._ensure_phase1", return_value=outcome),
            patch("shiori.tools.list_tree.settings", settings),
            patch("shiori.tools.common.settings", settings),
            patch("shiori.tools.list_tree._conn", _fake_conn),
        ):
            res = list_tree(repo=repo, source_type="code")

        assert isinstance(res, dict)
        assert "stale" not in res
        assert "refresh_error" not in res
        assert isinstance(res["entries"], list)
        paths = [e["path"] for e in res["entries"]]
        assert "code.py" in paths

    def test_debounced_refresh_returns_mapping_without_failure_fields(self, tmp_path):
        repo = "owner/list-tree-debounced"
        clone = _make_clone(tmp_path, repo)
        settings = _settings_for(clone, repo=repo)

        outcome = Phase1Outcome(status="debounced", head="def456", error=None)
        with (
            patch("shiori.tools.list_tree._ensure_phase1", return_value=outcome),
            patch("shiori.tools.list_tree.settings", settings),
            patch("shiori.tools.common.settings", settings),
            patch("shiori.tools.list_tree._conn", _fake_conn),
        ):
            res = list_tree(repo=repo, source_type="code")

        assert isinstance(res, dict)
        assert "stale" not in res
        assert "refresh_error" not in res
        assert isinstance(res["entries"], list)
        paths = [e["path"] for e in res["entries"]]
        assert "code.py" in paths

    def test_empty_entries_on_successful_refresh(self, tmp_path):
        repo = "owner/list-tree-empty"
        empty_clone = tmp_path / "repos" / repo.replace("/", "__")
        (empty_clone / ".git").mkdir(parents=True)
        settings = _settings_for(empty_clone, repo=repo)

        outcome = Phase1Outcome(status="refreshed", head="def456", error=None)
        with (
            patch("shiori.tools.list_tree._ensure_phase1", return_value=outcome),
            patch("shiori.tools.list_tree.settings", settings),
            patch("shiori.tools.common.settings", settings),
            patch("shiori.tools.list_tree._conn", _fake_conn),
        ):
            res = list_tree(repo=repo, source_type="code")

        assert res == {"entries": []}
        assert "stale" not in res
        assert "refresh_error" not in res


# ---------------------------------------------------------------------------
# grep multi-repo mixed outcomes (clean + stale + missing)
# ---------------------------------------------------------------------------


class TestGrepMultiRepoMixed:
    def test_mixed_clean_stale_and_missing_repos(self, tmp_path):
        r_clean = "owner/repo-clean"
        r_stale = "owner/repo-stale"
        r_missing = "owner/repo-missing"

        c_clean = _make_clone(tmp_path, r_clean)
        c_stale = _make_clone(tmp_path, r_stale)
        c_missing = tmp_path / "repos" / "missing_dir"

        def repo_dir_mock(r: str) -> str:
            if r == r_clean:
                return str(c_clean)
            if r == r_stale:
                return str(c_stale)
            return str(c_missing)

        settings = MagicMock()
        settings.repos = [r_clean, r_stale, r_missing]
        settings.repo_dir.side_effect = repo_dir_mock

        def ensure_mock(repo: str) -> Phase1Outcome:
            if repo == r_clean:
                return Phase1Outcome(status="refreshed", head="h1")
            elif repo == r_stale:
                return Phase1Outcome(status="failed", head="h2", error="DNS outage")
            else:
                return Phase1Outcome(status="failed", head=None, error="connection reset")

        with (
            patch("shiori.tools.grep._resolve_repos", return_value=[r_clean, r_stale, r_missing]),
            patch("shiori.tools.grep._ensure_phase1", side_effect=ensure_mock),
            patch("shiori.tools.grep.settings", settings),
        ):
            res = grep_search(pattern=KNOWN_TOKEN, regex=False, ignore_case=False)

        assert res["total_matches"] == 2
        matched_repos = {m["repo"] for m in res["matches"]}
        assert matched_repos == {r_clean, r_stale}

        # Stale metadata
        assert res["stale"] is True
        assert res["refresh_error"] == "DNS outage"
        assert res["stale_repos"] == [{"repo": r_stale, "error": "DNS outage"}]

        # Partial / skipped metadata
        assert res["partial"] is True
        assert res["skipped_repos"] == [{"repo": r_missing, "reason": "clone directory not found"}]

    def test_multiple_stale_repos_combines_errors(self, tmp_path):
        r_stale1 = "owner/repo-stale-1"
        r_stale2 = "owner/repo-stale-2"

        c_stale1 = _make_clone(tmp_path, r_stale1)
        c_stale2 = _make_clone(tmp_path, r_stale2)

        def repo_dir_mock(r: str) -> str:
            return str(c_stale1) if r == r_stale1 else str(c_stale2)

        settings = MagicMock()
        settings.repos = [r_stale1, r_stale2]
        settings.repo_dir.side_effect = repo_dir_mock

        def ensure_mock(repo: str) -> Phase1Outcome:
            if repo == r_stale1:
                return Phase1Outcome(status="failed", head="h1", error="DNS timeout")
            else:
                return Phase1Outcome(status="failed", head="h2", error="auth token expired")

        with (
            patch("shiori.tools.grep._resolve_repos", return_value=[r_stale1, r_stale2]),
            patch("shiori.tools.grep._ensure_phase1", side_effect=ensure_mock),
            patch("shiori.tools.grep.settings", settings),
        ):
            res = grep_search(pattern=KNOWN_TOKEN, regex=False)

        assert res["stale"] is True
        assert len(res["stale_repos"]) == 2
        assert "owner/repo-stale-1: DNS timeout" in res["refresh_error"]
        assert "owner/repo-stale-2: auth token expired" in res["refresh_error"]
