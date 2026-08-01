"""Tests for shiori.embedding: ONNX resolve/fallback semantics (issue #353)
and SentenceTransformer device choice / CUDA fallback (issue #383).

Neither sentence-transformers ([embed]) nor optimum/onnxruntime ([onnx]) are
installed in the [dev] sandbox by design (#179, #353) -- these tests mock
Embedder._init_st / Embedder._init_onnx rather than require the real
libraries, exercising the ImportError-fallback contract in embedding.py
without needing real model inference.

The device-choice tests exercise the REAL _init_st with fake torch /
sentence_transformers modules injected into sys.modules (the sandbox has no
torch, CI installs it, so tests must not rely on either real presence or
real absence).

(Batch_size resolution tests removed in PR #343 -- see git history; this
file was a stub docstring-only placeholder before #353 added the tests
below.)
"""

from __future__ import annotations

import sys
import types

import pytest

from shiori.embedding import (
    Embedder,
    _is_device_acquisition_failure,
    _resolve_onnx_path,
)


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


# --- Device choice / CUDA fallback (issue #383) ---------------------------
# The real Embedder._init_st runs under fake torch / sentence_transformers
# modules injected into sys.modules.  Production code keeps its imports
# function-local (both libraries are optional extras), so the fakes below are
# what those imports resolve to during the tests.


def _block_module(monkeypatch, name):
    """Force ``import name`` to raise ImportError regardless of whether the
    real library is installed (the dev sandbox has no torch, CI installs it
    -- tests must be deterministic either way)."""
    monkeypatch.setitem(sys.modules, name, None)


def _install_fake_st(monkeypatch, st_cls):
    """Inject a fake sentence_transformers module exposing st_cls as
    SentenceTransformer."""
    fake = types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = st_cls
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)


def _install_fake_st_behavior(monkeypatch, behavior):
    """Install a fake SentenceTransformer whose constructor calls
    behavior(device) -> embedding dim, or raises.

    Returns the list of (model_name, device) constructor calls.
    """
    calls = []

    class FakeSentenceTransformer:
        def __init__(self, model_name, device=None):
            calls.append((model_name, device))
            outcome = behavior(device)
            if isinstance(outcome, BaseException):
                raise outcome
            self._dim = outcome

        def get_sentence_embedding_dimension(self):
            return self._dim

    _install_fake_st(monkeypatch, FakeSentenceTransformer)
    return calls


def _install_fake_torch(monkeypatch, is_available=True, oom_cls=None, detection_error=None):
    """Inject a fake torch module: cuda.is_available() returns is_available
    (or raises detection_error), cuda.OutOfMemoryError is oom_cls when given.
    """
    fake_torch = types.ModuleType("torch")
    fake_cuda = types.ModuleType("torch.cuda")

    def is_available_impl():
        if detection_error is not None:
            raise detection_error
        return is_available

    fake_cuda.is_available = is_available_impl
    if oom_cls is not None:
        fake_cuda.OutOfMemoryError = oom_cls
    fake_torch.cuda = fake_cuda
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    return fake_torch


class TestInitStDeviceChoice:
    """The real _init_st: device is always explicit, always logged, and a
    CUDA acquisition failure falls back to CPU while a model-load failure
    still propagates."""

    @staticmethod
    def _embedder():
        # Bare instance: skip Embedder.__init__ (ONNX resolution etc.) and
        # exercise _init_st directly.
        return Embedder.__new__(Embedder)

    def test_healthy_gpu_uses_cuda_and_logs_device(self, monkeypatch, caplog):
        _install_fake_torch(monkeypatch, is_available=True)
        calls = _install_fake_st_behavior(monkeypatch, lambda device: 384)
        embedder = self._embedder()

        with caplog.at_level("INFO", logger="shiori.embedding"):
            embedder._init_st("e5-small")

        assert calls == [("e5-small", "cuda")]
        assert embedder.dim == 384
        assert "device=cuda: auto-detected" in caplog.text

    def test_no_cuda_uses_cpu(self, monkeypatch, caplog):
        _install_fake_torch(monkeypatch, is_available=False)
        calls = _install_fake_st_behavior(monkeypatch, lambda device: 384)
        embedder = self._embedder()

        with caplog.at_level("INFO", logger="shiori.embedding"):
            embedder._init_st("e5-small")

        assert calls == [("e5-small", "cpu")]
        assert "device=cpu: auto-detected (no CUDA)" in caplog.text

    def test_torch_missing_uses_cpu(self, monkeypatch, caplog):
        _block_module(monkeypatch, "torch")
        calls = _install_fake_st_behavior(monkeypatch, lambda device: 384)
        embedder = self._embedder()

        with caplog.at_level("INFO", logger="shiori.embedding"):
            embedder._init_st("e5-small")

        assert calls == [("e5-small", "cpu")]
        assert "device=cpu: auto-detected (torch unavailable)" in caplog.text

    def test_detection_failure_uses_cpu_and_keeps_going(self, monkeypatch, caplog):
        _install_fake_torch(monkeypatch, detection_error=RuntimeError("broken driver"))
        calls = _install_fake_st_behavior(monkeypatch, lambda device: 384)
        embedder = self._embedder()

        with caplog.at_level("INFO", logger="shiori.embedding"):
            embedder._init_st("e5-small")

        assert calls == [("e5-small", "cpu")]
        assert embedder.dim == 384
        assert "detection failed" in caplog.text

    def test_cuda_oom_falls_back_to_cpu_with_warning(self, monkeypatch, caplog):
        class FakeOom(RuntimeError):
            pass

        _install_fake_torch(monkeypatch, is_available=True, oom_cls=FakeOom)

        def behavior(device):
            if device == "cuda":
                raise FakeOom("Tried to allocate 512 MiB; device busy")
            return 384

        calls = _install_fake_st_behavior(monkeypatch, behavior)
        embedder = self._embedder()

        with caplog.at_level("WARNING", logger="shiori.embedding"):
            embedder._init_st("e5-small")

        assert calls == [("e5-small", "cuda"), ("e5-small", "cpu")]
        assert embedder.dim == 384
        assert "device=cpu: fell back after a failure" in caplog.text

    def test_cuda_init_runtime_error_falls_back_to_cpu(self, monkeypatch, caplog):
        _install_fake_torch(monkeypatch, is_available=True)

        def behavior(device):
            if device == "cuda":
                raise RuntimeError("Found no NVIDIA driver on your system")
            return 384

        calls = _install_fake_st_behavior(monkeypatch, behavior)

        with caplog.at_level("WARNING", logger="shiori.embedding"):
            self._embedder()._init_st("e5-small")

        assert calls == [("e5-small", "cuda"), ("e5-small", "cpu")]

    def test_model_load_failure_propagates_without_cpu_retry(self, monkeypatch):
        _install_fake_torch(monkeypatch, is_available=True)

        def behavior(device):
            raise RuntimeError("Error(s) in loading state_dict")

        calls = _install_fake_st_behavior(monkeypatch, behavior)

        with pytest.raises(RuntimeError, match="state_dict"):
            self._embedder()._init_st("e5-small")

        # Exactly one attempt: a non-device failure is not retried on CPU.
        assert calls == [("e5-small", "cuda")]

    def test_corrupt_model_file_propagates(self, monkeypatch):
        _install_fake_torch(monkeypatch, is_available=True)

        def behavior(device):
            raise OSError("cannot open model file")

        calls = _install_fake_st_behavior(monkeypatch, behavior)

        with pytest.raises(OSError, match="model file"):
            self._embedder()._init_st("e5-small")

        assert calls == [("e5-small", "cuda")]

    def test_failure_on_cpu_retry_propagates(self, monkeypatch):
        """After a genuine CUDA fallback, a failure on the CPU retry is by
        definition not a device problem and must propagate."""
        class FakeOom(RuntimeError):
            pass

        _install_fake_torch(monkeypatch, is_available=True, oom_cls=FakeOom)

        def behavior(device):
            if device == "cuda":
                raise FakeOom("Tried to allocate 512 MiB; device busy")
            raise OSError("disk full while downloading on CPU")

        calls = _install_fake_st_behavior(monkeypatch, behavior)

        with pytest.raises(OSError, match="disk full"):
            self._embedder()._init_st("e5-small")

        assert calls == [("e5-small", "cuda"), ("e5-small", "cpu")]


class TestIsDeviceAcquisitionFailure:
    """The rescue predicate in isolation: only device-acquisition failures
    are rescuable, and the exception types resolve at call time."""

    def test_torch_absent_is_never_rescuable(self, monkeypatch):
        _block_module(monkeypatch, "torch")
        assert _is_device_acquisition_failure(RuntimeError("CUDA error: init failed")) is False

    def test_cuda_oom_is_rescuable(self, monkeypatch):
        class FakeOom(RuntimeError):
            pass

        _install_fake_torch(monkeypatch, oom_cls=FakeOom)
        # Message carries no marker: the isinstance check alone must rescue.
        assert _is_device_acquisition_failure(FakeOom("Tried to allocate 512 MiB")) is True

    def test_cuda_init_runtime_errors_are_rescuable(self, monkeypatch):
        _install_fake_torch(monkeypatch)
        assert _is_device_acquisition_failure(
            RuntimeError("Found no NVIDIA driver on your system")
        ) is True
        assert _is_device_acquisition_failure(
            RuntimeError("CUDA error: no kernel image is available for execution on the device")
        ) is True

    def test_plain_runtime_error_is_not_rescuable(self, monkeypatch):
        _install_fake_torch(monkeypatch)
        assert _is_device_acquisition_failure(
            RuntimeError("Error(s) in loading state_dict for model")
        ) is False
