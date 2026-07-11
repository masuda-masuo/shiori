"""_build_warnings unit tests (issue #35, #187)."""

from __future__ import annotations

from unittest.mock import patch

from shiori.mcp_server import _build_warnings, _stale_threshold_seconds


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


# ── Stale threshold derivation (issue #187) ──


class TestStaleThresholdSeconds:
    """_stale_threshold_seconds: auto sync 有効時は interval から閾値を導出する。"""

    def test_disabled_uses_fixed_24h(self):
        """auto sync 無効（sync_interval_seconds<=0）では固定24時間のまま。"""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.sync_interval_seconds = 0
            assert _stale_threshold_seconds() == 86400

    def test_enabled_scales_with_interval(self):
        """interval=10（issueの実例）では threshold=300（floor）になる。"""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.sync_interval_seconds = 10
            assert _stale_threshold_seconds() == 300

    def test_enabled_floor_applies_for_tiny_interval(self):
        """interval=1 のような極端に短い値でも floor 未満にはならない。"""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.sync_interval_seconds = 1
            assert _stale_threshold_seconds() == 300

    def test_enabled_scales_above_floor_for_larger_interval(self):
        """interval が大きい場合は floor でなく interval*倍率が使われる。"""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.sync_interval_seconds = 600
            assert _stale_threshold_seconds() == 18000  # 600 * 30

    def test_negative_interval_uses_fixed_24h(self):
        """負の値も無効扱いで固定24時間になる。"""
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.sync_interval_seconds = -1
            assert _stale_threshold_seconds() == 86400


class TestBuildWarningsIntervalDerivedThreshold:
    """_build_warnings が動的閾値を使うこと（issue #187）。"""

    def test_warns_earlier_when_auto_sync_enabled_with_short_interval(self):
        """interval=10 なら 20 時間経過（issueの実例）でも stale 警告が出る。"""
        info = {"age_seconds": 72715}  # 20.2 hours, from the issue report
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.sync_interval_seconds = 10
            result = _build_warnings(info, {}, 0, {"docs": "x"})
        assert any("hours since last sync" in w for w in result)

    def test_no_warning_within_derived_threshold(self):
        """導出した閾値の内側では警告が出ない。"""
        info = {"age_seconds": 100}
        with patch("shiori.mcp_server.settings") as mock_settings:
            mock_settings.sync_interval_seconds = 10  # threshold = 300
            result = _build_warnings(info, {}, 0, {"docs": "x"})
        assert not any("hours since last sync" in w for w in result)


# ── Consecutive failure warning (issue #187) ──


class TestConsecutiveFailuresWarning:
    """_build_warnings: 連続失敗カウンタからの警告（issue #187）。"""

    def test_warns_when_consecutive_failures_positive(self):
        """consecutive_failures > 0 のとき last_error を含む警告を出す。"""
        info = {
            "age_seconds": 100,
            "consecutive_failures": 5,
            "last_error": "git fetch failed (exit 128): Invalid username or token",
        }
        result = _build_warnings(info, {}, 0, {"docs": "x"})
        assert any("5 consecutive sync failures" in w for w in result)
        assert any("Invalid username or token" in w for w in result)

    def test_no_warning_when_consecutive_failures_zero(self):
        """consecutive_failures=0 では警告なし。"""
        info = {"age_seconds": 100, "consecutive_failures": 0, "last_error": None}
        result = _build_warnings(info, {}, 0, {"docs": "x"})
        assert not any("consecutive sync failures" in w for w in result)

    def test_no_warning_when_consecutive_failures_absent(self):
        """consecutive_failures キー自体が無い（未同期リポジトリ）場合も警告なし。"""
        info = {"age_seconds": 100}
        result = _build_warnings(info, {}, 0, {"docs": "x"})
        assert not any("consecutive sync failures" in w for w in result)
