"""Unit tests for _build_warnings (issue #35)."""

from __future__ import annotations

import pytest

from shiori.mcp_server import _build_warnings


# ── 警告なし（正常時）──


def test_no_warnings_when_everything_is_fine():
    """Return empty list when everything is fine."""
    info = {"age_seconds": 3600}  # 1 時間 → 新鮮
    chunk_counts = {"issue": 10, "pr_review": 2}
    items_in_db = 10
    cursors = {"docs": "abc", "issues": "2026-01-01", "issue_comments": "2026-01-01", "pr_review_comments": "2026-01-01"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert result == []


# ── 鮮度警告 ──


def test_staleness_warning_when_age_exceeds_threshold():
    """Warning when age_seconds > 86400."""
    info = {"age_seconds": 90000}  # 25 時間
    chunk_counts = {}
    items_in_db = 0
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert any("最終同期から" in w for w in result)


def test_no_staleness_warning_at_boundary():
    """No warning at boundary (age_seconds == 86400)."""
    info = {"age_seconds": 86400}
    chunk_counts = {}
    items_in_db = 0
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert not any("最終同期から" in w for w in result)


def test_no_staleness_warning_when_age_is_none():
    """No staleness warning when age_seconds=None (unsynced)."""
    info = {"age_seconds": None}
    chunk_counts = {}
    items_in_db = 0
    cursors = {}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert not any("最終同期から" in w for w in result)


# ── 欠落警告 ──


def test_missing_chunks_warning_when_chunks_few():
    """Warning when chunks are few."""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 1, "pr_review": 0}
    items_in_db = 10
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    # issue(1) + pr_review(0) = 1 < 10//2 = 5 → 警告
    assert any("bot 除外" in w for w in result)


def test_no_missing_warning_at_boundary():
    """No warning at boundary: items_in_db=6, chunks=3."""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 3, "pr_review": 0}
    items_in_db = 6
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    # 3 >= 6//2 = 3 → 警告なし
    assert not any("bot 除外" in w for w in result)


def test_missing_warning_with_pr_review():
    """pr_review chunks contribute to suppressing missing-chunks warning."""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 1, "pr_review": 4}
    items_in_db = 10
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    # issue(1) + pr_review(4) = 5 >= 10//2 = 5 → 警告なし
    assert not any("bot 除外" in w for w in result)


def test_missing_warning_with_zero_items():
    """No missing-chunks warning when items_in_db=0."""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 0, "pr_review": 0}
    items_in_db = 0
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert not any("bot 除外" in w for w in result)


# ── 未同期警告 ──


def test_unsynced_warning_for_missing_cursors():
    """Warning when categories have missing cursors."""
    info = {"age_seconds": 3600}
    chunk_counts = {}
    items_in_db = 0
    cursors = {"docs": "abc"}  # issues, issue_comments, pr_review_comments が欠落

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert any("未同期の種類があります" in w for w in result)


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
    assert not any("未同期の種類があります" in w for w in result)


def test_multiple_warnings_can_coexist():
    """Multiple warnings can coexist."""
    info = {"age_seconds": 90000}  # 鮮度警告
    chunk_counts = {"issue": 1, "pr_review": 0}
    items_in_db = 10  # 欠落警告
    cursors = {}  # 未同期警告

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert len(result) == 3
    assert any("最終同期から" in w for w in result)
    assert any("bot 除外" in w for w in result)
    assert any("未同期の種類があります" in w for w in result)
