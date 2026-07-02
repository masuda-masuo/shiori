"""_build_warnings unit tests (issue #35)."""

from __future__ import annotations

import pytest

from shiori.mcp_server import _build_warnings


# ── No warnings (normal)──


def test_no_warnings_when_everything_is_fine():
    """Returns an empty list when everything is fine."""
    info = {"age_seconds": 3600}  # 1 hour → fresh
    chunk_counts = {"issue": 10, "pr_review": 2}
    items_in_db = 10
    cursors = {"docs": "abc", "issues": "2026-01-01", "issue_comments": "2026-01-01", "pr_review_comments": "2026-01-01"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert result == []


# ── Freshness warning ──


def test_staleness_warning_when_age_exceeds_threshold():
    """Warns when age_seconds > 86400."""
    info = {"age_seconds": 90000}  # 25 hours
    chunk_counts = {}
    items_in_db = 0
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert any("hours since last sync" in w for w in result)


def test_no_staleness_warning_at_boundary():
    """No warning at age_seconds == 86400 exactly."""
    info = {"age_seconds": 86400}
    chunk_counts = {}
    items_in_db = 0
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert not any("hours since last sync" in w for w in result)


def test_no_staleness_warning_when_age_is_none():
    """age_seconds=None (never synced) does not produce a freshness warning."""
    info = {"age_seconds": None}
    chunk_counts = {}
    items_in_db = 0
    cursors = {}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert not any("hours since last sync" in w for w in result)


# ── Missing chunks warning ──


def test_missing_chunks_warning_when_chunks_few():
    """Warns when chunks are few."""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 1, "pr_review": 0}
    items_in_db = 10
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    # issue(1) + pr_review(0) = 1 < 10//2 = 5 → warning
    assert any("Bot exclusion" in w for w in result)


def test_no_missing_warning_at_boundary():
    """Boundary: items_in_db=6, chunks=3, no warning."""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 3, "pr_review": 0}
    items_in_db = 6
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    # 3 >= 6//2 = 3 → no warning
    assert not any("Bot exclusion" in w for w in result)


def test_missing_warning_with_pr_review():
    """pr_review チャンクがMissing chunks warningの抑止に寄与する。"""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 1, "pr_review": 4}
    items_in_db = 10
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    # issue(1) + pr_review(4) = 5 >= 10//2 = 5 → no warning
    assert not any("Bot exclusion" in w for w in result)


def test_missing_warning_with_zero_items():
    """No missing chunks warning when items_in_db=0."""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 0, "pr_review": 0}
    items_in_db = 0
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert not any("Bot exclusion" in w for w in result)


# ── Unsynced warning ──


def test_unsynced_warning_for_missing_cursors():
    """Warns when some categories are missing cursors."""
    info = {"age_seconds": 3600}
    chunk_counts = {}
    items_in_db = 0
    cursors = {"docs": "abc"}  # issues, issue_comments, pr_review_comments are missing

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert any("Unsynced categories" in w for w in result)


def test_no_unsynced_warning_when_all_cursors_present():
    """No warning when all cursors are present."""
    info = {"age_seconds": 3600}
    chunk_counts = {}
    items_in_db = 0
    cursors = {
        "docs": "abc",
        "issues": "2026-01-01",
        "issue_comments": "2026-01-01",
        "pr_review_comments": "2026-01-01",
    }

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert not any("Unsynced categories" in w for w in result)


def test_multiple_warnings_can_coexist():
    """Multiple warnings appear simultaneously."""
    info = {"age_seconds": 90000}  # Freshness warning
    chunk_counts = {"issue": 1, "pr_review": 0}
    items_in_db = 10  # Missing chunks warning
    cursors = {}  # unsynced warning

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert len(result) == 3
    assert any("hours since last sync" in w for w in result)
    assert any("Bot exclusion" in w for w in result)
    assert any("Unsynced categories" in w for w in result)
