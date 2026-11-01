"""Global pytest fixtures for test isolation."""

from __future__ import annotations

import pytest

from shiori import pipeline


@pytest.fixture(autouse=True)
def _isolate_tool_refresh_and_debounce_state():
    """Reset process-local Phase 1 debounce and tool refresh failure state before and after each test."""
    pipeline._phase1_last_fetch.clear()
    pipeline._reset_tool_refresh_state()
    try:
        yield
    finally:
        pipeline._phase1_last_fetch.clear()
        pipeline._reset_tool_refresh_state()
