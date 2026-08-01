"""Circuit breaker: per-lane backoff cap (issue #371) and defensive reads.

Direct unit tests of ``ingest._should_skip_repo``: the backoff cap is
chosen per lane from ``settings.dev_repos``, so a reference repo (daily
cadence, elapsed is always ~86400s) can finally be skipped, while a dev
repo keeps today's one-hour cap and exactly today's skip decisions.

Also pins that every circuit-breaker setting falls back to its built-in
default on an empty, unparseable, or non-positive environment value, and
that MagicMock settings (non-numeric fields) disable the breaker rather
than raising.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from shiori.config import Settings
from shiori.ingest import _should_skip_repo

DAY = 86400.0
DEV_CAP = 3600.0
REF_CAP = 604800.0


def _settings(**overrides) -> Settings:
    """Real Settings with the built-in defaults, plus per-test overrides."""
    s = Settings()
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def _attempt_at(seconds_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds_ago)


def _should_skip(
    repo: str,
    settings: Settings,
    failures: int,
    last_attempt_at: datetime | None,
    explicit: bool = False,
) -> bool:
    with patch(
        "shiori.ingest.db.get_sync_attempt",
        return_value=(failures, last_attempt_at),
    ):
        return _should_skip_repo(None, repo, settings, explicit)


class TestReferenceLaneDailyCadence:
    """A repo failing every day: elapsed between attempts is always 86400s.

    With the defaults (threshold 5, base 60, ref cap 604800) the backoff
    first exceeds 86400 at 12 consecutive failures (60 * 2**11 = 122880),
    so the breaker fires from the 12th failure onward -- and never before
    the threshold regardless of timing.
    """

    def test_ref_repo_not_skipped_at_11_consecutive_failures(self):
        settings = _settings(dev_repos={"dev/one"})
        # Backoff at 11 failures is 60 * 2**10 = 61440 < 86400: not skipped.
        assert (
            _should_skip("ref/one", settings, 11, _attempt_at(DAY)) is False
        )

    def test_ref_repo_skipped_at_12_consecutive_failures(self):
        settings = _settings(dev_repos={"dev/one"})
        # Backoff at 12 failures is 60 * 2**11 = 122880 > 86400: skipped.
        assert _should_skip("ref/one", settings, 12, _attempt_at(DAY)) is True

    def test_ref_skip_lengthens_until_the_new_cap(self):
        """The ref cap (604800) bounds the backoff, not the dev cap (3600)."""
        settings = _settings(dev_repos=set())
        # At 15 failures the exponential term (60 * 2**14 = 983040) first
        # clears the cap, so the backoff is exactly 604800: a repo that
        # failed for ~7 days is still inside the window...
        assert (
            _should_skip(
                "ref/one", settings, 15, _attempt_at(REF_CAP - 60.0)
            )
            is True
        )
        # ...and is retried once the cap has passed.
        assert (
            _should_skip(
                "ref/one", settings, 15, _attempt_at(REF_CAP + 60.0)
            )
            is False
        )

    def test_ref_repo_explicit_skip_raises(self):
        """The explicit-repo path raises with the reference cap in play."""
        settings = _settings(dev_repos=set())
        with pytest.raises(ValueError, match="circuit-broken"):
            _should_skip(
                "ref/one", settings, 12, _attempt_at(DAY), explicit=True
            )

    def test_ref_repo_below_threshold_never_skips(self):
        settings = _settings(dev_repos=set())
        # 4 < threshold 5, even with a huge backoff candidate.
        assert _should_skip("ref/one", settings, 4, _attempt_at(0.0)) is False


class TestDevLaneUnchanged:
    """Dev repos keep threshold 5 / base 60 / cap 3600 exactly."""

    def test_dev_defaults_are_unchanged(self):
        s = Settings()
        assert s.cb_threshold == 5
        assert s.cb_base_backoff == 60.0
        assert s.cb_max_backoff == DEV_CAP
        assert s.cb_ref_max_backoff == REF_CAP

    def test_dev_repo_skips_inside_one_hour_window(self):
        """5 failures, last attempt moments ago: skipped (backoff 960s)."""
        settings = _settings(dev_repos={"dev/one"})
        assert (
            _should_skip("dev/one", settings, 5, _attempt_at(0.0)) is True
        )

    def test_dev_repo_cap_is_still_exactly_one_hour(self):
        """The dev cap still bites at 3600: 3599 skips, 3601 does not."""
        settings = _settings(dev_repos={"dev/one"})
        assert (
            _should_skip(
                "dev/one", settings, 14, _attempt_at(DEV_CAP - 1.0)
            )
            is True
        )
        assert (
            _should_skip(
                "dev/one", settings, 14, _attempt_at(DEV_CAP + 1.0)
            )
            is False
        )

    def test_dev_repo_failing_daily_is_retried_daily(self):
        """A dev repo failing every day is retried every day: the ref cap
        must NOT leak onto the dev lane (elapsed 86400 > dev cap 3600)."""
        settings = _settings(dev_repos={"dev/one"})
        assert (
            _should_skip("dev/one", settings, 14, _attempt_at(DAY)) is False
        )

    def test_same_threshold_and_base_on_both_lanes(self):
        """Dev and ref repos share threshold/base; only the cap differs."""
        settings = _settings(dev_repos={"dev/one"})
        # Below the shared threshold: neither lane skips.
        assert (
            _should_skip("dev/one", settings, 4, _attempt_at(0.0)) is False
        )
        assert (
            _should_skip("ref/one", settings, 4, _attempt_at(0.0)) is False
        )


class TestMagicMockSettings:
    """Non-numeric settings fields must disable the breaker, not raise."""

    def test_magicmock_settings_return_false(self):
        mock_settings = MagicMock()
        with patch(
            "shiori.ingest.db.get_sync_attempt",
            return_value=(5, _attempt_at(0.0)),
        ):
            assert (
                _should_skip_repo(None, "owner/repo", mock_settings, False)
                is False
            )


CB_ENV_DEFAULTS = (
    ("SHIORI_CB_THRESHOLD", "cb_threshold", 5),
    ("SHIORI_CB_BASE_BACKOFF", "cb_base_backoff", 60.0),
    ("SHIORI_CB_MAX_BACKOFF", "cb_max_backoff", DEV_CAP),
    ("SHIORI_CB_REF_MAX_BACKOFF", "cb_ref_max_backoff", REF_CAP),
)


#: Rejected by every setting. "0" is deliberately absent: it is a bad
#: value for the three float settings but the documented off-switch for
#: SHIORI_CB_THRESHOLD, so the two are pinned separately below.
BAD_VALUES = ["", "abc", "-1", "0.0", "1e", "12.5.6", "  "]


class TestDefensiveEnvReads:
    """All four settings fall back to defaults on bad env values (#371)."""

    @pytest.mark.parametrize("env_var,field,default", CB_ENV_DEFAULTS)
    @pytest.mark.parametrize("bad", BAD_VALUES)
    def test_falls_back_to_default_on_bad_value(
        self, monkeypatch, env_var, field, default, bad
    ):
        monkeypatch.setenv(env_var, bad)
        assert getattr(Settings(), field) == default

    @pytest.mark.parametrize("env_var,field,default", [
        row for row in CB_ENV_DEFAULTS if row[0] != "SHIORI_CB_THRESHOLD"
    ])
    def test_zero_is_a_bad_value_for_the_backoff_settings(
        self, monkeypatch, env_var, field, default
    ):
        """A zero base or a zero cap is not a configuration, it is a typo."""
        monkeypatch.setenv(env_var, "0")
        assert getattr(Settings(), field) == default

    def test_threshold_zero_from_env_disables_the_breaker(self, monkeypatch):
        """SHIORI_CB_THRESHOLD=0 is the documented off-switch.

        It worked before this change (the read was a bare ``int()``), and
        the setting only now reaches the container -- so folding 0 into
        the default would ship a knob that silently does nothing.
        """
        monkeypatch.setenv("SHIORI_CB_THRESHOLD", "0")
        settings = _settings(dev_repos=set())
        assert settings.cb_threshold == 0
        # 20 consecutive failures, attempted a moment ago: without the
        # off-switch this is the deepest possible skip.
        assert (
            _should_skip("ref/one", settings, 20, _attempt_at(0.0)) is False
        )
        assert (
            _should_skip("dev/one", settings, 20, _attempt_at(0.0)) is False
        )

    @pytest.mark.parametrize("env_var,field", [
        (env_var, field) for env_var, field, _ in CB_ENV_DEFAULTS
    ])
    def test_valid_value_is_read(self, monkeypatch, env_var, field):
        monkeypatch.setenv(env_var, "123")
        value = getattr(Settings(), field)
        if env_var == "SHIORI_CB_THRESHOLD":
            assert value == 123
        else:
            assert value == 123.0

    def test_settings_construction_never_raises_on_empty_env(self, monkeypatch):
        """The crash this change removes: int('')/float('') on the ${VAR:-}
        compose expansion. Every setting empty at once must still work."""
        for env_var, _, _ in CB_ENV_DEFAULTS:
            monkeypatch.setenv(env_var, "")
        s = Settings()
        assert s.cb_threshold == 5
        assert s.cb_base_backoff == 60.0
        assert s.cb_max_backoff == DEV_CAP
        assert s.cb_ref_max_backoff == REF_CAP
