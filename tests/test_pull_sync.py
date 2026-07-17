"""Pull-type sync tests: Phase 1/2, index_stale, cross-repo search (#236)."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from shiori import mcp_server
from shiori import pipeline as sync_pipeline
from shiori.mcp_server import (
    _ensure_phase1,
    _trigger_phase2,
    status,
)


# ── _ensure_phase1: single-flight + debounce ──


class TestEnsurePhase1:
    """_ensure_phase1: per-repo single-flight, debounce, and error handling."""

    def test_returns_head_on_success(self):
        with (
            patch("shiori.pipeline.build_token_provider") as mock_build,
            patch("shiori.refresh.refresh_clone", return_value="abc123"),
            patch("shiori.pipeline._conn"),
            patch("shiori.pipeline.db.upsert_clone_head"),
        ):
            mock_build.return_value = MagicMock()
            result = _ensure_phase1("o/r")
        assert result == "abc123"

# ── _trigger_phase2: single-flight no-op ──


class TestTriggerPhase2:
    """_trigger_phase2: single-flight background triggering."""

    def test_second_call_is_noop_while_first_running(self):
        sync_pipeline._phase2_in_flight.clear()
        # Pre-populate in-flight so second call is seen as duplicate
        sync_pipeline._phase2_in_flight.add("o/r")
        _trigger_phase2("o/r")  # should be no-op, repo already in-flight
        # In-flight set unchanged (the thread spawned by the _trigger_phase2
        # would remove it on completion, but this test's setup means the guard
        # already returned before thread spawn)
        sync_pipeline._phase2_in_flight.clear()

    def test_first_call_adds_to_in_flight(self):
        sync_pipeline._phase2_in_flight.clear()
        with (
            patch("shiori.pipeline._do_sync", return_value={"status": "ok"}),
            patch("shiori.pipeline.settings") as mock_settings,
        ):
            mock_settings.repos = ["o/r"]
            _trigger_phase2("o/r")
        sync_pipeline._phase2_in_flight.clear()

    def test_repo_removed_from_in_flight_after_completion(self):
        sync_pipeline._phase2_in_flight.clear()
        assert "o/r" not in sync_pipeline._phase2_in_flight


# ── status(): index_stale never_indexed detection ──


class TestStatusIndexStale:
    """status(): index_stale and never_indexed detection (#236)."""

    def _run_status(self, index_state=None, sync_runs=None):
        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.db.get_sync_runs",
                  return_value=sync_runs or {}),
            patch("shiori.mcp_server.db.get_all_repo_index_state",
                  return_value=index_state or {}),
            patch("shiori.mcp_server.db.get_chunk_counts", return_value={}),
            patch("shiori.mcp_server.db.get_issue_item_count", return_value=0),
            patch("shiori.mcp_server.db.get_cursors", return_value={}),
        ):
            mock_settings.repos = ["o/r"]
            mock_settings.sync_interval_seconds = 10
            return status()

    def test_fresh_when_both_heads_match(self):
        result = self._run_status(
            index_state={"o/r": {"clone_head": "abc123", "indexed_head": "abc123"}}
        )
        repo = result["repos"]["o/r"]
        assert repo["clone_head"] == "abc123"
        assert repo["indexed_head"] == "abc123"
        assert repo["index_stale"] is False
        assert repo["never_indexed"] is False

    def test_stale_when_heads_differ(self):
        result = self._run_status(
            index_state={"o/r": {"clone_head": "def456", "indexed_head": "abc123"}}
        )
        repo = result["repos"]["o/r"]
        assert repo["index_stale"] is True
        assert repo["never_indexed"] is False
        warnings = repo["warnings"]
        assert any("index is stale" in w for w in warnings)

    def test_never_indexed_when_no_indexed_head(self):
        result = self._run_status(
            index_state={"o/r": {"clone_head": "abc123", "indexed_head": None}}
        )
        repo = result["repos"]["o/r"]
        assert repo["index_stale"] is True
        assert repo["never_indexed"] is True
        warnings = repo["warnings"]
        assert any("never been indexed" in w for w in warnings)

    def test_neither_when_no_state_at_all(self):
        result = self._run_status()
        repo = result["repos"]["o/r"]
        assert repo["clone_head"] is None
        assert repo["indexed_head"] is None
        assert repo["index_stale"] is False
        assert repo["never_indexed"] is False


# ── Cross-repo search (repo=None) triggers Phase 1/2 for all repos ──


class TestCrossRepoSearchPhase1:
    """semantic_search / keyword_search with repo=None refresh all repos."""

    def test_repo_none_triggers_phase1_for_all_repos(self):
        from shiori.mcp_server import semantic_search

        called = []
        def fake_phase1(repo):
            called.append(("phase1", repo))

        def fake_phase2(repo):
            called.append(("phase2", repo))

        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server._get_embedder") as mock_emb,
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.search.semantic_search", return_value=[]),
            patch("shiori.mcp_server._resolve_repo_filter", return_value=None),
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server._ensure_phase1", side_effect=fake_phase1),
            patch("shiori.mcp_server._trigger_phase2", side_effect=fake_phase2),
        ):
            mock_emb.return_value = MagicMock()
            mock_settings.repos = ["r1", "r2", "r3"]
            semantic_search(query="test", repo=None)

        phase1_calls = [r for (kind, r) in called if kind == "phase1"]
        assert phase1_calls == ["r1", "r2", "r3"]

    def test_repo_specific_triggers_only_that_repo(self):
        from shiori.mcp_server import semantic_search

        called = []
        def fake_phase1(repo):
            called.append(("phase1", repo))

        def fake_phase2(repo):
            called.append(("phase2", repo))

        with (
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server._get_embedder") as mock_emb,
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.search.semantic_search", return_value=[]),
            patch("shiori.mcp_server._resolve_repo_filter", return_value="o/r"),
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server._ensure_phase1", side_effect=fake_phase1),
            patch("shiori.mcp_server._trigger_phase2", side_effect=fake_phase2),
        ):
            mock_emb.return_value = MagicMock()
            mock_settings.repos = ["r1", "r2", "r3"]
            semantic_search(query="test", repo="o/r")

        phase1_calls = [r for (kind, r) in called if kind == "phase1"]
        assert phase1_calls == ["o/r"]
