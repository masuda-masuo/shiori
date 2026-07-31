"""Tests for the SHIORI_LOG_LEVEL control and per-file log volume (issue #372).

The 15-minute dev lane emitted one INFO line per indexed file, blowing
through the journal's ~9h retention so the daily ref lane's failure logs
were gone before anyone could investigate.  The per-file "indexed ..."
lines are demoted to DEBUG; SHIORI_LOG_LEVEL re-enables them.

No PostgreSQL, no network, no embedding model: the dynamic test drives
shiori.sync_docs.index_docs with fakes, and the level control is tested
through shiori.__main__.log_level_from_env plus the CLI --help path.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from shiori.__main__ import log_level_from_env, main

# ===================================================================
# log_level_from_env: resolution and fallback (outcomes 2 and 3)
# ===================================================================


class TestLogLevelFromEnv:
    def test_unset_defaults_to_info(self, monkeypatch):
        monkeypatch.delenv("SHIORI_LOG_LEVEL", raising=False)
        assert log_level_from_env() == logging.INFO

    def test_empty_value_defaults_to_info(self, monkeypatch):
        """Outcome 3: empty value falls back, does not crash."""
        monkeypatch.setenv("SHIORI_LOG_LEVEL", "")
        assert log_level_from_env() == logging.INFO

    def test_blank_value_defaults_to_info(self, monkeypatch):
        monkeypatch.setenv("SHIORI_LOG_LEVEL", "   ")
        assert log_level_from_env() == logging.INFO

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("DEBUG", logging.DEBUG),
            ("debug", logging.DEBUG),  # case-insensitive
            ("Info", logging.INFO),
            ("WARNING", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("CRITICAL", logging.CRITICAL),
        ],
    )
    def test_valid_names(self, monkeypatch, raw, expected):
        monkeypatch.setenv("SHIORI_LOG_LEVEL", raw)
        assert log_level_from_env() == expected

    def test_unrecognised_value_falls_back_to_info(self, monkeypatch):
        """Outcome 3: a garbage value must not crash the process."""
        monkeypatch.setenv("SHIORI_LOG_LEVEL", "not-a-level")
        assert log_level_from_env() == logging.INFO

    def test_numeric_string_falls_back_to_info(self, monkeypatch):
        """Only level *names* are recognised; numbers are not special-cased."""
        monkeypatch.setenv("SHIORI_LOG_LEVEL", "10")
        assert log_level_from_env() == logging.INFO


class TestMainHelpWithEnv:
    """Smoke parity: SHIORI_LOG_LEVEL=... python -m shiori --help exits 0."""

    def _run_help(self) -> None:
        with patch.object(sys, "argv", ["shiori", "--help"]):
            main()

    def test_help_exits_0_with_garbage_level(self, monkeypatch):
        monkeypatch.setenv("SHIORI_LOG_LEVEL", "not-a-level")
        with pytest.raises(SystemExit) as exc:
            self._run_help()
        assert exc.value.code == 0

    def test_help_exits_0_with_debug_level(self, monkeypatch):
        monkeypatch.setenv("SHIORI_LOG_LEVEL", "DEBUG")
        with pytest.raises(SystemExit) as exc:
            self._run_help()
        assert exc.value.code == 0

    def test_help_documents_env_var(self, monkeypatch, capsys):
        monkeypatch.delenv("SHIORI_LOG_LEVEL", raising=False)
        with pytest.raises(SystemExit):
            self._run_help()
        out = capsys.readouterr().out
        assert "SHIORI_LOG_LEVEL" in out


# ===================================================================
# Per-file "indexed ..." lines are DEBUG (outcome 1)
# ===================================================================


class _FakeCursor:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        pass

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def cursor(self):
        return _FakeCursor(self._rows)

    def commit(self):
        pass


class _FakeSettings:
    def __init__(self, repo_dir: str):
        self._repo_dir = repo_dir
        self.chunk_max_chars = 1200

    def repo_dir(self, repo: str) -> str:
        return self._repo_dir


class _FakeEmbedder:
    def embed_passages(self, texts):
        return [[0.0] * 8 for _ in texts]


class TestPerFileLinesAreDebug:
    """A healthy index_docs run is silent at INFO, detailed at DEBUG."""

    def _run_index_docs(self, tmp_path: Path, caplog, level: int):
        repo_dir = tmp_path / "o__r"
        repo_dir.mkdir()
        (repo_dir / "guide.md").write_text("# Guide\n\nHello world\n", encoding="utf-8")

        settings = _FakeSettings(str(repo_dir))
        conn = _FakeConn()
        embedder = _FakeEmbedder()

        caplog.set_level(level, logger="shiori.sync_docs")
        with patch("shiori.sync_docs._git", return_value="origin/main"):
            from shiori.sync_docs import index_docs

            index_docs(settings, conn, embedder, "o/r")
        return caplog

    def test_no_per_file_line_at_default_info_level(self, tmp_path, caplog):
        caplog = self._run_index_docs(tmp_path, caplog, logging.INFO)
        per_file = [r for r in caplog.records if "indexed doc" in r.getMessage()]
        assert per_file == []

    def test_per_file_line_visible_at_debug_level(self, tmp_path, caplog):
        caplog = self._run_index_docs(tmp_path, caplog, logging.DEBUG)
        per_file = [
            r
            for r in caplog.records
            if "indexed doc" in r.getMessage() and r.levelno == logging.DEBUG
        ]
        assert len(per_file) == 1
        assert "guide.md" in per_file[0].getMessage()


class TestPerFileSitesAreDebug:
    """Regression guard pinning the three issue #372 log sites to DEBUG.

    Driving sync_code.index_code (tree-sitter) and the second sync_docs
    path end-to-end needs a live DB, so this pins the exact sites by
    source; it fails if any is flipped back to log.info.
    """

    @staticmethod
    def _src(name: str) -> str:
        path = Path(__file__).parent.parent / "src" / "shiori" / name
        return path.read_text(encoding="utf-8")

    def test_sync_code_per_file_line_is_debug(self):
        src = self._src("sync_code.py")
        assert src.count('log.debug("indexed code %s (%d chunks, %s)"') == 1
        assert 'log.info("indexed code' not in src

    def test_sync_docs_per_file_lines_are_debug(self):
        src = self._src("sync_docs.py")
        assert src.count('log.debug("indexed doc %s (%d chunks)"') == 2
        assert 'log.info("indexed doc' not in src
