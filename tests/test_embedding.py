"""Tests for shiori.embedding: ONNX resolve/fallback semantics (issue #353).

Neither sentence-transformers ([embed]) nor optimum/onnxruntime ([onnx]) are
installed in the [dev] sandbox by design (#179, #353) -- these tests mock
Embedder._init_st / Embedder._init_onnx rather than require the real
libraries, exercising the ImportError-fallback contract in embedding.py
without needing real model inference.

(Batch_size resolution tests removed in PR #343 -- see git history; this
file was a stub docstring-only placeholder before #353 added the tests
below.)
"""

from __future__ import annotations

import pytest

from shiori.embedding import Embedder, _resolve_onnx_path


def _make_onnx_dir(tmp_path, name="onnx"):
    onnx_dir = tmp_path / name
    onnx_dir.mkdir()
    (onnx_dir / "model.onnx").write_bytes(b"")
    return onnx_dir


class TestResolveOnnxPath:
    def test_empty_string_disables_even_if_default_paths_exist(self, tmp_path, monkeypatch):
        """SHIORI_ONNX_MODEL_PATH="" is an explicit off-switch (used by the
        GPU ingest overlay): it must win even when a default candidate path
        genuinely has a model in it.
        """
        fake_default = _make_onnx_dir(tmp_path, "default-onnx")
        monkeypatch.setattr("shiori.embedding._DEFAULT_ONNX_CANDIDATES", [fake_default])
        monkeypatch.setenv("SHIORI_ONNX_MODEL_PATH", "")

        assert _resolve_onnx_path() is None

    def test_env_path_used_when_it_has_an_onnx_file(self, tmp_path, monkeypatch):
        onnx_dir = _make_onnx_dir(tmp_path)
        monkeypatch.setattr("shiori.embedding._DEFAULT_ONNX_CANDIDATES", [])
        monkeypatch.setenv("SHIORI_ONNX_MODEL_PATH", str(onnx_dir))

        assert _resolve_onnx_path() == onnx_dir

    def test_file_not_dir_at_candidate_path_does_not_raise(self, tmp_path, monkeypatch):
        """A stray file (not a directory) at the candidate path must not
        raise NotADirectoryError out of iterdir()."""
        stray_file = tmp_path / "not-a-dir"
        stray_file.write_bytes(b"")
        monkeypatch.setattr("shiori.embedding._DEFAULT_ONNX_CANDIDATES", [])
        monkeypatch.setenv("SHIORI_ONNX_MODEL_PATH", str(stray_file))

        assert _resolve_onnx_path() is None

    def test_explicit_but_unusable_env_path_does_not_consult_defaults(
        self, tmp_path, monkeypatch, caplog
    ):
        """An explicitly set path with no .onnx model means ST, NOT a silent
        switch to a different model found under the default candidates
        (reviewer finding, issue #353)."""
        empty_env_dir = tmp_path / "env-model"
        empty_env_dir.mkdir()
        fake_default = tmp_path / "default-model"
        fake_default.mkdir()
        (fake_default / "model.onnx").touch()
        monkeypatch.setattr("shiori.embedding._DEFAULT_ONNX_CANDIDATES", [fake_default])
        monkeypatch.setenv("SHIORI_ONNX_MODEL_PATH", str(empty_env_dir))

        with caplog.at_level("WARNING", logger="shiori.embedding"):
            assert _resolve_onnx_path() is None
        assert "no .onnx model" in caplog.text

    def test_unset_falls_back_to_default_candidates(self, tmp_path, monkeypatch):
        onnx_dir = _make_onnx_dir(tmp_path, "default")
        monkeypatch.setattr("shiori.embedding._DEFAULT_ONNX_CANDIDATES", [onnx_dir])
        monkeypatch.delenv("SHIORI_ONNX_MODEL_PATH", raising=False)

        assert _resolve_onnx_path() == onnx_dir

    def test_no_candidate_has_onnx_file_returns_none(self, tmp_path, monkeypatch):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.setattr("shiori.embedding._DEFAULT_ONNX_CANDIDATES", [empty_dir])
        monkeypatch.delenv("SHIORI_ONNX_MODEL_PATH", raising=False)

        assert _resolve_onnx_path() is None


class TestEmbedderOnnxImportFallback:
    def test_missing_onnx_library_falls_back_to_sentence_transformer(
        self, tmp_path, monkeypatch, caplog
    ):
        """A fake ONNX dir is present but optimum is absent (the sandbox's
        real state): Embedder() must not raise, must log a warning, and must
        land on the ST path.
        """
        onnx_dir = _make_onnx_dir(tmp_path)
        monkeypatch.setenv("SHIORI_ONNX_MODEL_PATH", str(onnx_dir))

        def fake_init_st(self, model_name):
            self.model = "fake-st-model"
            self.dim = 384

        monkeypatch.setattr(Embedder, "_init_st", fake_init_st)

        with caplog.at_level("WARNING"):
            embedder = Embedder()

        assert embedder._onnx_backend is False
        assert embedder.model == "fake-st-model"
        warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("onnx" in msg.lower() and "shiori[onnx]" in msg for msg in warnings)

    def test_non_import_error_from_init_onnx_propagates(self, tmp_path, monkeypatch):
        """A corrupt model file (or any non-ImportError failure) must crash
        loudly rather than silently falling back -- only the missing-library
        case is recoverable.
        """
        onnx_dir = _make_onnx_dir(tmp_path)
        monkeypatch.setenv("SHIORI_ONNX_MODEL_PATH", str(onnx_dir))

        def raise_runtime_error(self, onnx_path):
            raise RuntimeError("corrupt model file")

        monkeypatch.setattr(Embedder, "_init_onnx", raise_runtime_error)

        with pytest.raises(RuntimeError, match="corrupt model file"):
            Embedder()
