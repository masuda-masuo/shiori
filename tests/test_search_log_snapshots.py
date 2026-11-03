"""Acceptance checks for search snapshot CLI and immutable result semantics."""
from __future__ import annotations

import subprocess
import sys


def test_forget_help_discloses_snapshot_and_external_copy_limits() -> None:
    """CLI help names the preserved query/filters and excluded data stores."""
    result = subprocess.run(
        [sys.executable, "-m", "shiori", "forget", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    help_text = result.stdout.lower()
    normalized = " ".join(help_text.replace("-", " ").split())

    # Semantically required disclosures; phrasing and order may vary.
    assert "query" in normalized
    assert "filter" in normalized
    assert "wal" in normalized
    assert "backup" in normalized
    assert "exported" in normalized and ("sheet" in normalized or "spreadsheet" in normalized)
    assert "version 2" in normalized
    assert "legacy" in normalized and "unversioned" in normalized
    assert "id" in normalized
    assert "exclusive lock" in normalized
    assert "searches wait" in normalized
    assert "large repositories can hold it longer" in normalized
