"""_build_warnings の単体テスト（issue #35）。"""

from __future__ import annotations

import pytest

from shiori.mcp_server import _build_warnings


# ── 警告なし（正常時）──


def test_no_warnings_when_everything_is_fine():
    """全て正常な場合は空リストを返す。"""
    info = {"age_seconds": 3600}  # 1 時間 → 新鮮
    chunk_counts = {"issue": 10, "pr_review": 2}
    items_in_db = 10
    cursors = {"docs": "abc", "issues": "2026-01-01", "issue_comments": "2026-01-01", "pr_review_comments": "2026-01-01"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert result == []


# ── 鮮度警告 ──


def test_staleness_warning_when_age_exceeds_threshold():
    """age_seconds > 86400 で警告を出す。"""
    info = {"age_seconds": 90000}  # 25 時間
    chunk_counts = {}
    items_in_db = 0
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert any("最終同期から" in w for w in result)


def test_no_staleness_warning_at_boundary():
    """age_seconds == 86400 ちょうどは警告なし。"""
    info = {"age_seconds": 86400}
    chunk_counts = {}
    items_in_db = 0
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert not any("最終同期から" in w for w in result)


def test_no_staleness_warning_when_age_is_none():
    """age_seconds=None（未同期）では鮮度警告を出さない。"""
    info = {"age_seconds": None}
    chunk_counts = {}
    items_in_db = 0
    cursors = {}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert not any("最終同期から" in w for w in result)


# ── 欠落警告 ──


def test_missing_chunks_warning_when_chunks_few():
    """chunks が少ない場合に警告を出す。"""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 1, "pr_review": 0}
    items_in_db = 10
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    # issue(1) + pr_review(0) = 1 < 10//2 = 5 → 警告
    assert any("bot 除外" in w for w in result)


def test_no_missing_warning_at_boundary():
    """境界値: items_in_db=6, chunks=3 で警告なし。"""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 3, "pr_review": 0}
    items_in_db = 6
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    # 3 >= 6//2 = 3 → 警告なし
    assert not any("bot 除外" in w for w in result)


def test_missing_warning_with_pr_review():
    """pr_review チャンクが欠落警告の抑止に寄与する。"""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 1, "pr_review": 4}
    items_in_db = 10
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    # issue(1) + pr_review(4) = 5 >= 10//2 = 5 → 警告なし
    assert not any("bot 除外" in w for w in result)


def test_missing_warning_with_zero_items():
    """items_in_db=0 のときは欠落警告を出さない。"""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 0, "pr_review": 0}
    items_in_db = 0
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert not any("bot 除外" in w for w in result)


# ── 未同期警告 ──


def test_unsynced_warning_for_missing_cursors():
    """カーソルが欠落しているカテゴリがある場合に警告を出す。"""
    info = {"age_seconds": 3600}
    chunk_counts = {}
    items_in_db = 0
    cursors = {"docs": "abc"}  # issues, issue_comments, pr_review_comments が欠落

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert any("未同期の種類があります" in w for w in result)


def test_no_unsynced_warning_when_all_cursors_present():
    """全てのカーソルが揃っている場合は警告なし。"""
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
    """複数の警告が同時に出力される。"""
    info = {"age_seconds": 90000}  # 鮮度警告
    chunk_counts = {"issue": 1, "pr_review": 0}
    items_in_db = 10  # 欠落警告
    cursors = {}  # 未同期警告

    result = _build_warnings(info, chunk_counts, items_in_db, cursors)
    assert len(result) == 3
    assert any("最終同期から" in w for w in result)
    assert any("bot 除外" in w for w in result)
    assert any("未同期の種類があります" in w for w in result)
