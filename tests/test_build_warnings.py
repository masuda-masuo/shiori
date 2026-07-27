"""_build_warnings unit tests (issue #35, #187, #347)."""

from __future__ import annotations

from unittest.mock import patch

from shiori.tools.status import _build_warnings, _stale_threshold_seconds

# _build_warnings now takes an explicit stale_threshold_seconds (issue #347:
# the threshold is derived per-repo/role by the caller, status()). Tests
# below that are not exercising the threshold derivation itself just pass a
# fixed, arbitrary value that matches the old default (24h) so their
# existing age_seconds fixtures keep meaning what they said.
_THRESHOLD = 86400


# ── No warnings (normal)──


def test_no_warnings_when_everything_is_fine():
    """Returns an empty list when everything is fine."""
    info = {"age_seconds": 3600}  # 1 hour → fresh
    chunk_counts = {"issue": 10, "pr_review": 2}
    items_in_db = 10
    cursors = {"docs": "abc", "issues": "2026-01-01", "issue_comments": "2026-01-01", "pr_review_comments": "2026-01-01"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors, _THRESHOLD)
    assert result == []


# ── Freshness warning ──


def test_staleness_warning_when_age_exceeds_threshold():
    """Warns when age_seconds > threshold."""
    info = {"age_seconds": 90000}  # 25 hours
    chunk_counts = {}
    items_in_db = 0
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors, _THRESHOLD)
    assert any("hours since last sync" in w for w in result)


def test_no_staleness_warning_at_boundary():
    """No warning at age_seconds == threshold exactly."""
    info = {"age_seconds": 86400}
    chunk_counts = {}
    items_in_db = 0
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors, _THRESHOLD)
    assert not any("hours since last sync" in w for w in result)


def test_no_staleness_warning_when_age_is_none():
    """age_seconds=None (never synced) does not produce a freshness warning."""
    info = {"age_seconds": None}
    chunk_counts = {}
    items_in_db = 0
    cursors = {}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors, _THRESHOLD)
    assert not any("hours since last sync" in w for w in result)


# ── Missing chunks warning ──


def test_missing_chunks_warning_when_chunks_few():
    """Warns when chunks are few."""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 1, "pr_review": 0}
    items_in_db = 10
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors, _THRESHOLD)
    # issue(1) + pr_review(0) = 1 < 10//2 = 5 → warning
    assert any("Bot exclusion" in w for w in result)


def test_no_missing_warning_at_boundary():
    """Boundary: items_in_db=6, chunks=3, no warning."""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 3, "pr_review": 0}
    items_in_db = 6
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors, _THRESHOLD)
    # 3 >= 6//2 = 3 → no warning
    assert not any("Bot exclusion" in w for w in result)


def test_missing_warning_with_pr_review():
    """pr_review チャンクがMissing chunks warningの抑止に寄与する。"""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 1, "pr_review": 4}
    items_in_db = 10
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors, _THRESHOLD)
    # issue(1) + pr_review(4) = 5 >= 10//2 = 5 → no warning
    assert not any("Bot exclusion" in w for w in result)


def test_missing_warning_with_zero_items():
    """No missing chunks warning when items_in_db=0."""
    info = {"age_seconds": 3600}
    chunk_counts = {"issue": 0, "pr_review": 0}
    items_in_db = 0
    cursors = {"docs": "abc"}

    result = _build_warnings(info, chunk_counts, items_in_db, cursors, _THRESHOLD)
    assert not any("Bot exclusion" in w for w in result)


# ── Unsynced warning ──


def test_unsynced_warning_for_missing_cursors():
    """Warns when some categories are missing cursors."""
    info = {"age_seconds": 3600}
    chunk_counts = {}
    items_in_db = 0
    cursors = {"docs": "abc"}  # issues, issue_comments, pr_review_comments are missing

    result = _build_warnings(info, chunk_counts, items_in_db, cursors, _THRESHOLD)
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

    result = _build_warnings(info, chunk_counts, items_in_db, cursors, _THRESHOLD)
    assert not any("Unsynced categories" in w for w in result)


def test_multiple_warnings_can_coexist():
    """Multiple warnings appear simultaneously."""
    info = {"age_seconds": 90000}  # Freshness warning
    chunk_counts = {"issue": 1, "pr_review": 0}
    items_in_db = 10  # Missing chunks warning
    cursors = {}  # unsynced warning

    result = _build_warnings(info, chunk_counts, items_in_db, cursors, _THRESHOLD)
    assert len(result) == 3
    assert any("hours since last sync" in w for w in result)
    assert any("Bot exclusion" in w for w in result)
    assert any("Unsynced categories" in w for w in result)


# ── Stale threshold derivation (issue #187, superseded by #347) ──


class TestStaleThresholdSeconds:
    """_stale_threshold_seconds: role-aware (dev vs ref), 2x the expected
    timer cadence, floored at 300s (issue #347).

    Supersedes the old single sync_interval_seconds-derived formula (issue
    #187): sync_interval_seconds is the Phase-1 clone refresh debounce, not
    an index sync cadence, so deriving a staleness threshold from it alone
    reported a fictional cadence.
    """

    def test_dev_repo_uses_dev_interval_default(self):
        with patch("shiori.tools.status.settings") as mock_settings:
            mock_settings.dev_repos = {"o/dev"}
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 86400
            assert _stale_threshold_seconds("o/dev") == 1800  # 2 * 900

    def test_ref_repo_uses_ref_interval_default(self):
        with patch("shiori.tools.status.settings") as mock_settings:
            mock_settings.dev_repos = {"o/dev"}
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 86400
            assert _stale_threshold_seconds("o/ref") == 172800  # 2 * 86400

    def test_dev_repo_respects_env_override(self):
        """A short dev interval floors at 300s rather than going below it."""
        with patch("shiori.tools.status.settings") as mock_settings:
            mock_settings.dev_repos = {"o/dev"}
            mock_settings.dev_sync_interval_seconds = 60
            mock_settings.ref_sync_interval_seconds = 86400
            assert _stale_threshold_seconds("o/dev") == 300  # 2*60=120, floored

    def test_ref_repo_respects_env_override(self):
        with patch("shiori.tools.status.settings") as mock_settings:
            mock_settings.dev_repos = set()
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 100
            assert _stale_threshold_seconds("o/ref") == 300  # 2*100=200, floored

    def test_floor_applies_for_tiny_interval(self):
        with patch("shiori.tools.status.settings") as mock_settings:
            mock_settings.dev_repos = {"o/dev"}
            mock_settings.dev_sync_interval_seconds = 1
            mock_settings.ref_sync_interval_seconds = 86400
            assert _stale_threshold_seconds("o/dev") == 300

    def test_scales_above_floor_for_larger_interval(self):
        with patch("shiori.tools.status.settings") as mock_settings:
            mock_settings.dev_repos = set()
            mock_settings.dev_sync_interval_seconds = 900
            mock_settings.ref_sync_interval_seconds = 20000
            assert _stale_threshold_seconds("o/ref") == 40000  # 2 * 20000


class TestBuildWarningsRespectsGivenThreshold:
    """_build_warnings uses whatever stale_threshold_seconds the caller
    passes in (status() derives it per-repo/role via
    _stale_threshold_seconds; issue #347 superseded the old single-interval
    derivation that used to live inside _build_warnings itself, issue #187).
    """

    def test_warns_when_age_exceeds_given_threshold(self):
        info = {"age_seconds": 400}
        result = _build_warnings(info, {}, 0, {"docs": "x"}, 300)
        assert any("hours since last sync" in w for w in result)

    def test_no_warning_within_given_threshold(self):
        info = {"age_seconds": 100}
        result = _build_warnings(info, {}, 0, {"docs": "x"}, 300)
        assert not any("hours since last sync" in w for w in result)


# ── Consecutive failure warning (issue #187) ──


class TestConsecutiveFailuresWarning:
    """_build_warnings: 連続失敗カウンタからの警告(issue #187)。"""

    def test_warns_when_consecutive_failures_positive(self):
        """consecutive_failures > 0 のとき last_error を含む警告を出す。"""
        info = {
            "age_seconds": 100,
            "consecutive_failures": 5,
            "last_error": "git fetch failed (exit 128): Invalid username or token",
        }
        result = _build_warnings(info, {}, 0, {"docs": "x"}, _THRESHOLD)
        assert any("5 consecutive sync failures" in w for w in result)
        assert any("Invalid username or token" in w for w in result)

    def test_no_warning_when_consecutive_failures_zero(self):
        """consecutive_failures=0 では警告なし。"""
        info = {"age_seconds": 100, "consecutive_failures": 0, "last_error": None}
        result = _build_warnings(info, {}, 0, {"docs": "x"}, _THRESHOLD)
        assert not any("consecutive sync failures" in w for w in result)

    def test_no_warning_when_consecutive_failures_absent(self):
        """consecutive_failures キー自体が無い(未同期リポジトリ)場合も警告なし。"""
        info = {"age_seconds": 100}
        result = _build_warnings(info, {}, 0, {"docs": "x"}, _THRESHOLD)
        assert not any("consecutive sync failures" in w for w in result)


# ── Token provider construction error (issue #193) ──


class TestTokenProviderErrorWarning:
    """_build_warnings: build_token_provider() 自体が例外を投げた場合の警告(issue #193)。"""

    def test_warns_when_error_present(self):
        """token_provider_error があるとき警告を出す。"""
        info = {
            "age_seconds": 100,
            "token_provider_error": "GitHub App configuration is incomplete...",
        }
        result = _build_warnings(info, {}, 0, {"docs": "x"}, _THRESHOLD)
        assert any("token_provider could not be determined" in w for w in result)
        assert any("GitHub App configuration is incomplete" in w for w in result)

    def test_no_warning_when_error_absent(self):
        """token_provider_error が None のときは警告なし。"""
        info = {"age_seconds": 100, "token_provider_error": None}
        result = _build_warnings(info, {}, 0, {"docs": "x"}, _THRESHOLD)
        assert not any("token_provider could not be determined" in w for w in result)

    def test_no_warning_when_error_key_absent(self):
        """token_provider_error キー自体が無い場合も警告なし。"""
        info = {"age_seconds": 100}
        result = _build_warnings(info, {}, 0, {"docs": "x"}, _THRESHOLD)
        assert not any("token_provider could not be determined" in w for w in result)
