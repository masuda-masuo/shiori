"""Defensive env parsing for the ten settings converted in #397.

Every one of the ten former bare ``int(os.environ.get(...))`` /
``float(os.environ.get(...))`` reads must survive the compose ``${VAR:-}``
form: unset, empty, whitespace-only, and unparseable values fall back to the
built-in default instead of raising at ``Settings()`` construction.

The zero / negative expectations are deliberately NOT uniform: what an
out-of-range value means differs per setting and each expectation below is
derived from the consuming code (cited per class). A blanket
"non-positive -> default" rule would have changed runtime behaviour for the
settings whose zero is documented (SHIORI_SYNC_INTERVAL_SECONDS,
SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS) or meaningful
(SHIORI_RATE_LIMIT_MAX_WAIT / _MAX_RETRIES).
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from shiori.config import Settings

#: Values that must fall back to the default for every one of the ten:
#: unset is exercised separately; these are the delivered-but-useless forms
#: (compose ``${VAR:-}`` expands unset to "", plus operator typos).
BAD_STRINGS = ["", "   ", "abc", "1.5x", "--3"]


def _get(field: str) -> object:
    return getattr(Settings(), field)


class TestChunkMaxChars:
    """SHIORI_CHUNK_MAX_CHARS -- positive only.

    chunking._split_long_text loops ``while len(s) > max_chars`` taking
    ``s[:cut]`` with ``cut = _find_breakpoint(s, max_chars)``; with
    max_chars <= 0 the breakpoint search range is empty, cut becomes
    max_chars (<= 0) and the remainder never shrinks -- an infinite loop,
    not a configuration.
    """

    def test_unset_defaults(self, monkeypatch):
        monkeypatch.delenv("SHIORI_CHUNK_MAX_CHARS", raising=False)
        assert _get("chunk_max_chars") == 1200

    @pytest.mark.parametrize("bad", BAD_STRINGS)
    def test_bad_string_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("SHIORI_CHUNK_MAX_CHARS", bad)
        assert _get("chunk_max_chars") == 1200

    def test_valid_value(self, monkeypatch):
        monkeypatch.setenv("SHIORI_CHUNK_MAX_CHARS", "800")
        assert _get("chunk_max_chars") == 800

    @pytest.mark.parametrize("nonpositive", ["0", "-1"])
    def test_zero_and_negative_would_hang_the_chunker(
        self, monkeypatch, nonpositive
    ):
        monkeypatch.setenv("SHIORI_CHUNK_MAX_CHARS", nonpositive)
        assert _get("chunk_max_chars") == 1200


class TestTopK:
    """SHIORI_TOP_K -- positive only.

    search.py truncates with ``ranked[:k]``: k=0 makes every search return
    nothing, a negative k slices from the tail. Neither is documented.
    """

    def test_unset_defaults(self, monkeypatch):
        monkeypatch.delenv("SHIORI_TOP_K", raising=False)
        assert _get("default_top_k") == 8

    @pytest.mark.parametrize("bad", BAD_STRINGS)
    def test_bad_string_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("SHIORI_TOP_K", bad)
        assert _get("default_top_k") == 8

    def test_valid_value(self, monkeypatch):
        monkeypatch.setenv("SHIORI_TOP_K", "20")
        assert _get("default_top_k") == 20

    @pytest.mark.parametrize("nonpositive", ["0", "-2"])
    def test_zero_and_negative_have_no_search_meaning(
        self, monkeypatch, nonpositive
    ):
        monkeypatch.setenv("SHIORI_TOP_K", nonpositive)
        assert _get("default_top_k") == 8


class TestSnippetChars:
    """SHIORI_SNIPPET_CHARS -- positive only.

    search._row_to_hit slices ``content[:snippet_chars]``: 0 turns every
    snippet into a bare ellipsis, a negative value drops the *end* of the
    content. Neither is documented.
    """

    def test_unset_defaults(self, monkeypatch):
        monkeypatch.delenv("SHIORI_SNIPPET_CHARS", raising=False)
        assert _get("snippet_chars") == 400

    @pytest.mark.parametrize("bad", BAD_STRINGS)
    def test_bad_string_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("SHIORI_SNIPPET_CHARS", bad)
        assert _get("snippet_chars") == 400

    def test_valid_value(self, monkeypatch):
        monkeypatch.setenv("SHIORI_SNIPPET_CHARS", "1000")
        assert _get("snippet_chars") == 1000

    @pytest.mark.parametrize("nonpositive", ["0", "-40"])
    def test_zero_and_negative_have_no_snippet_meaning(
        self, monkeypatch, nonpositive
    ):
        monkeypatch.setenv("SHIORI_SNIPPET_CHARS", nonpositive)
        assert _get("snippet_chars") == 400


class TestSyncIntervalSeconds:
    """SHIORI_SYNC_INTERVAL_SECONDS -- zero is real and IS the default.

    0 is the documented "always pull" value (config.py field comment;
    compose shipped a literal 0 until #397) and pipeline._ensure_phase1
    clamps the debounce with ``max(interval, _PHASE1_MIN_DEBOUNCE)``, so 0
    must pass through -- this is exactly the setting a blanket
    "non-positive -> default" rule would NOT have broken only by luck,
    because its default happens to be 0 as well. A negative interval
    behaves identically to 0 under that same ``max()``, so negative ->
    default(0) is behaviour-neutral.
    """

    def test_unset_defaults_to_zero(self, monkeypatch):
        monkeypatch.delenv("SHIORI_SYNC_INTERVAL_SECONDS", raising=False)
        assert _get("sync_interval_seconds") == 0

    @pytest.mark.parametrize("bad", BAD_STRINGS)
    def test_bad_string_falls_back_to_zero(self, monkeypatch, bad):
        monkeypatch.setenv("SHIORI_SYNC_INTERVAL_SECONDS", bad)
        assert _get("sync_interval_seconds") == 0

    def test_valid_value(self, monkeypatch):
        monkeypatch.setenv("SHIORI_SYNC_INTERVAL_SECONDS", "30")
        assert _get("sync_interval_seconds") == 30

    def test_zero_is_preserved_as_the_documented_always_pull(
        self, monkeypatch
    ):
        monkeypatch.setenv("SHIORI_SYNC_INTERVAL_SECONDS", "0")
        assert _get("sync_interval_seconds") == 0

    def test_negative_falls_back_to_zero_which_it_already_meant(
        self, monkeypatch
    ):
        monkeypatch.setenv("SHIORI_SYNC_INTERVAL_SECONDS", "-5")
        assert _get("sync_interval_seconds") == 0


class TestSteadySyncIntervals:
    """SHIORI_DEV/REF_SYNC_INTERVAL_SECONDS -- any integer passes through.

    These document the expected host-timer cadence; the only consumer
    (tools/status.py) floors the derived staleness threshold at
    _STALE_SECONDS_FLOOR (``max(interval * 2, 300)``) and otherwise reports
    the raw value in shiori_status output. Zero and negative are therefore
    already defended where they are used, and rewriting them at parse time
    would change both the reported value and the derived threshold (0 ->
    threshold 300 today; a fallback to 900 would make it 1800).
    """

    def test_unset_defaults(self, monkeypatch):
        monkeypatch.delenv("SHIORI_DEV_SYNC_INTERVAL_SECONDS", raising=False)
        monkeypatch.delenv("SHIORI_REF_SYNC_INTERVAL_SECONDS", raising=False)
        s = Settings()
        assert s.dev_sync_interval_seconds == 900
        assert s.ref_sync_interval_seconds == 86400

    @pytest.mark.parametrize("bad", BAD_STRINGS)
    def test_bad_string_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("SHIORI_DEV_SYNC_INTERVAL_SECONDS", bad)
        monkeypatch.setenv("SHIORI_REF_SYNC_INTERVAL_SECONDS", bad)
        s = Settings()
        assert s.dev_sync_interval_seconds == 900
        assert s.ref_sync_interval_seconds == 86400

    def test_valid_values(self, monkeypatch):
        monkeypatch.setenv("SHIORI_DEV_SYNC_INTERVAL_SECONDS", "600")
        monkeypatch.setenv("SHIORI_REF_SYNC_INTERVAL_SECONDS", "43200")
        s = Settings()
        assert s.dev_sync_interval_seconds == 600
        assert s.ref_sync_interval_seconds == 43200

    @pytest.mark.parametrize("raw,expected", [("0", 0), ("-60", -60)])
    def test_zero_and_negative_pass_through_status_floors_them(
        self, monkeypatch, raw, expected
    ):
        monkeypatch.setenv("SHIORI_DEV_SYNC_INTERVAL_SECONDS", raw)
        monkeypatch.setenv("SHIORI_REF_SYNC_INTERVAL_SECONDS", raw)
        s = Settings()
        assert s.dev_sync_interval_seconds == expected
        assert s.ref_sync_interval_seconds == expected


class TestMcpPort:
    """SHIORI_MCP_PORT -- positive only.

    mcp_server.run() passes it to ``mcp.run(port=...)``, a served endpoint:
    0 binds an ephemeral random port nobody can find, negative fails at bind.
    Neither is a configuration of this server.
    """

    def test_unset_defaults(self, monkeypatch):
        monkeypatch.delenv("SHIORI_MCP_PORT", raising=False)
        assert _get("mcp_port") == 8765

    @pytest.mark.parametrize("bad", BAD_STRINGS)
    def test_bad_string_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("SHIORI_MCP_PORT", bad)
        assert _get("mcp_port") == 8765

    def test_valid_value(self, monkeypatch):
        monkeypatch.setenv("SHIORI_MCP_PORT", "8080")
        assert _get("mcp_port") == 8080

    @pytest.mark.parametrize("nonpositive", ["0", "-1"])
    def test_zero_and_negative_are_not_a_served_port(
        self, monkeypatch, nonpositive
    ):
        monkeypatch.setenv("SHIORI_MCP_PORT", nonpositive)
        assert _get("mcp_port") == 8765


class TestRateLimitMaxWait:
    """SHIORI_RATE_LIMIT_MAX_WAIT -- zero is meaningful, negative crashes.

    github_errors.compute_wait_seconds clamps every wait with
    ``min(..., max_wait)``: 0 means "never sleep, retry immediately"
    (``time.sleep(0)`` is legal), so it must pass through. A negative cap
    would propagate through the same ``min()`` into api_utils'
    ``time.sleep()`` and raise ValueError mid-sync, so negative falls back.
    """

    def test_unset_defaults(self, monkeypatch):
        monkeypatch.delenv("SHIORI_RATE_LIMIT_MAX_WAIT", raising=False)
        assert _get("rate_limit_max_wait") == 60.0

    @pytest.mark.parametrize("bad", ["", "   ", "abc", "--3"])
    def test_bad_string_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("SHIORI_RATE_LIMIT_MAX_WAIT", bad)
        assert _get("rate_limit_max_wait") == 60.0

    def test_valid_float_value(self, monkeypatch):
        monkeypatch.setenv("SHIORI_RATE_LIMIT_MAX_WAIT", "0.5")
        assert _get("rate_limit_max_wait") == 0.5

    def test_zero_is_preserved_never_sleep(self, monkeypatch):
        monkeypatch.setenv("SHIORI_RATE_LIMIT_MAX_WAIT", "0")
        assert _get("rate_limit_max_wait") == 0.0

    def test_negative_would_crash_time_sleep_falls_back(self, monkeypatch):
        monkeypatch.setenv("SHIORI_RATE_LIMIT_MAX_WAIT", "-10")
        assert _get("rate_limit_max_wait") == 60.0


class TestRateLimitMaxRetries:
    """SHIORI_RATE_LIMIT_MAX_RETRIES -- zero is meaningful ("do not retry").

    api_utils._api_pages_gen raises RateLimitExhausted once ``retries >=
    max_retries`` with retries starting at 0, so 0 = fail on the first
    rate-limit hit and must pass through. A negative value would behave
    identically to 0 there but is treated as a misconfiguration and falls
    back -- the same non-negative convention as SHIORI_CB_THRESHOLD, whose
    helper also rejects only negatives.
    """

    def test_unset_defaults(self, monkeypatch):
        monkeypatch.delenv("SHIORI_RATE_LIMIT_MAX_RETRIES", raising=False)
        assert _get("rate_limit_max_retries") == 3

    @pytest.mark.parametrize("bad", BAD_STRINGS)
    def test_bad_string_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("SHIORI_RATE_LIMIT_MAX_RETRIES", bad)
        assert _get("rate_limit_max_retries") == 3

    def test_valid_value(self, monkeypatch):
        monkeypatch.setenv("SHIORI_RATE_LIMIT_MAX_RETRIES", "5")
        assert _get("rate_limit_max_retries") == 5

    def test_zero_is_preserved_as_do_not_retry(self, monkeypatch):
        monkeypatch.setenv("SHIORI_RATE_LIMIT_MAX_RETRIES", "0")
        assert _get("rate_limit_max_retries") == 0

    def test_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("SHIORI_RATE_LIMIT_MAX_RETRIES", "-1")
        assert _get("rate_limit_max_retries") == 3


class TestMaxParallelMaintenanceWorkers:
    """SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS -- zero is the documented
    default and must pass through.

    schema.create_heavy_indexes injects the value verbatim into
    ``SET max_parallel_maintenance_workers`` and documents 0 as "always
    set. Default 0 (serial build)" -- the knob that keeps pgvector's
    PARALLEL HNSW build out of /dev/shm. Negative is outside the PostgreSQL
    GUC's range (0..1024) and would fail that SET during the index build,
    so it falls back (to 0).
    """

    def test_unset_defaults_to_zero(self, monkeypatch):
        monkeypatch.delenv(
            "SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS", raising=False
        )
        assert _get("max_parallel_maintenance_workers") == 0

    @pytest.mark.parametrize("bad", BAD_STRINGS)
    def test_bad_string_falls_back_to_zero(self, monkeypatch, bad):
        monkeypatch.setenv("SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS", bad)
        assert _get("max_parallel_maintenance_workers") == 0

    def test_valid_value(self, monkeypatch):
        monkeypatch.setenv("SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS", "2")
        assert _get("max_parallel_maintenance_workers") == 2

    def test_zero_is_preserved_as_the_documented_serial_build(
        self, monkeypatch
    ):
        monkeypatch.setenv("SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS", "0")
        assert _get("max_parallel_maintenance_workers") == 0

    def test_negative_is_outside_the_guc_range_falls_back(self, monkeypatch):
        monkeypatch.setenv("SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS", "-1")
        assert _get("max_parallel_maintenance_workers") == 0


class TestSettingsConstructionSurvivesComposeEmptyForm:
    """The end-to-end guarantee of #397: a compose file forwarding any of
    the ten with the plain ``${VAR:-}`` form (unset host var -> "" in the
    container) must not crash Settings() construction."""

    TEN: ClassVar[list[str]] = [
        "SHIORI_CHUNK_MAX_CHARS",
        "SHIORI_TOP_K",
        "SHIORI_SNIPPET_CHARS",
        "SHIORI_SYNC_INTERVAL_SECONDS",
        "SHIORI_DEV_SYNC_INTERVAL_SECONDS",
        "SHIORI_REF_SYNC_INTERVAL_SECONDS",
        "SHIORI_MCP_PORT",
        "SHIORI_RATE_LIMIT_MAX_WAIT",
        "SHIORI_RATE_LIMIT_MAX_RETRIES",
        "SHIORI_MAX_PARALLEL_MAINTENANCE_WORKERS",
    ]

    def test_all_ten_empty_at_once(self, monkeypatch):
        for name in self.TEN:
            monkeypatch.setenv(name, "")
        s = Settings()
        assert s.chunk_max_chars == 1200
        assert s.default_top_k == 8
        assert s.snippet_chars == 400
        assert s.sync_interval_seconds == 0
        assert s.dev_sync_interval_seconds == 900
        assert s.ref_sync_interval_seconds == 86400
        assert s.mcp_port == 8765
        assert s.rate_limit_max_wait == 60.0
        assert s.rate_limit_max_retries == 3
        assert s.max_parallel_maintenance_workers == 0


class TestDocsOnlyReposConfig:
    """SHIORI_DOCS_ONLY_REPOS -- issue #441."""

    def test_unset_defaults_to_empty_set(self, monkeypatch):
        monkeypatch.delenv("SHIORI_DOCS_ONLY_REPOS", raising=False)
        s = Settings()
        assert s.docs_only_repos == set()
        assert not s.is_docs_only("owner/repo")

    def test_empty_string_returns_empty_set(self, monkeypatch):
        monkeypatch.setenv("SHIORI_DOCS_ONLY_REPOS", "")
        s = Settings()
        assert s.docs_only_repos == set()
        assert not s.is_docs_only("owner/repo")

    def test_whitespace_string_returns_empty_set(self, monkeypatch):
        monkeypatch.setenv("SHIORI_DOCS_ONLY_REPOS", "   ")
        s = Settings()
        assert s.docs_only_repos == set()

    def test_valid_comma_separated_repos(self, monkeypatch):
        monkeypatch.setenv(
            "SHIORI_DOCS_ONLY_REPOS", "cockroachdb/cockroach, golang/go "
        )
        s = Settings()
        assert s.docs_only_repos == {"cockroachdb/cockroach", "golang/go"}
        assert s.is_docs_only("cockroachdb/cockroach")
        assert s.is_docs_only("golang/go")
        assert not s.is_docs_only("masuda-masuo/shiori")
