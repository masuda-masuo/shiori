"""Unit tests for embedding batch_size resolution (GPU support)."""

from __future__ import annotations

import builtins
import sys
from unittest.mock import MagicMock

import pytest

from shiori.embedding import resolve_batch_size


def _original_import(name: str, *args, **kwargs):
    """Delegate to the real builtins.__import__."""
    return builtins.__import__(name, *args, **kwargs)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove SHIORI_EMBED_BATCH_SIZE from env for every test."""
    monkeypatch.delenv("SHIORI_EMBED_BATCH_SIZE", raising=False)


def test_batch_size_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """SHIORI_EMBED_BATCH_SIZE=128 returns 128."""
    monkeypatch.setenv("SHIORI_EMBED_BATCH_SIZE", "128")
    assert resolve_batch_size() == 128


def test_batch_size_from_env_zero_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """SHIORI_EMBED_BATCH_SIZE=0 is treated as invalid → falls back to auto."""
    monkeypatch.setenv("SHIORI_EMBED_BATCH_SIZE", "0")
    # CPU default (no torch.cuda available in this test setup)
    assert resolve_batch_size() == 32


def test_batch_size_from_env_negative_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """SHIORI_EMBED_BATCH_SIZE=-1 is treated as invalid → falls back to auto."""
    monkeypatch.setenv("SHIORI_EMBED_BATCH_SIZE", "-1")
    assert resolve_batch_size() == 32


def test_batch_size_from_env_invalid_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """SHIORI_EMBED_BATCH_SIZE=abc is treated as invalid → falls back to auto."""
    monkeypatch.setenv("SHIORI_EMBED_BATCH_SIZE", "abc")
    assert resolve_batch_size() == 32


def test_auto_cuda_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """torch.cuda.is_available() → True returns 512."""
    mock_cuda = MagicMock()
    mock_cuda.is_available.return_value = True
    mock_torch = MagicMock(cuda=mock_cuda)

    # Replace torch in sys.modules so import torch returns our mock
    monkeypatch.setitem(sys.modules, "torch", mock_torch)
    assert resolve_batch_size() == 512


def test_auto_cuda_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """torch.cuda.is_available() → False returns 32."""
    mock_cuda = MagicMock()
    mock_cuda.is_available.return_value = False
    mock_torch = MagicMock(cuda=mock_cuda)

    monkeypatch.setitem(sys.modules, "torch", mock_torch)
    assert resolve_batch_size() == 32


def test_auto_no_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    """No torch at all returns 32 (CPU default)."""
    # Make import torch raise ImportError
    def _mock_import(name: str, *args, **kwargs):
        if name == "torch":
            raise ImportError("mock: No module named 'torch'")
        return builtins.__import__(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _mock_import)
    assert resolve_batch_size() == 32
