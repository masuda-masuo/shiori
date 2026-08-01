"""Embedding (detailed design/03).

Single provider architecture: model baked into the image (docker/app/Dockerfile).
Dimension is auto-detected from the model.

API: embed_passages(texts, batch_size) -> np.ndarray, embed_query(text) -> np.ndarray.

ONNX Runtime (INT8 quantized) for CPU inference speed.
Falls back to SentenceTransformer when ONNX model is unavailable.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

from shiori.config import DEFAULT_EMBEDDING_MODEL

log = logging.getLogger(__name__)

_DEFAULT_ONNX_CANDIDATES: list[Path] = [
    Path("/models/onnx/e5-small-int8"),
    Path("/models/onnx/e5-small"),
]


def _mean_pool(token_embeddings: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    mask_expanded = np.expand_dims(attention_mask, axis=-1).astype(token_embeddings.dtype)
    sum_embeddings = np.sum(token_embeddings * mask_expanded, axis=1)
    sum_mask = np.clip(np.sum(mask_expanded, axis=1), 1e-9, None)
    return sum_embeddings / sum_mask


def _l2_normalize(embeddings: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(embeddings, axis=1, keepdims=True)
    return embeddings / norm


def _resolve_onnx_path() -> Path | None:
    """Resolve the ONNX model directory, or None to use SentenceTransformer.

    ``SHIORI_ONNX_MODEL_PATH=""`` (set but empty) is an explicit off-switch:
    it returns None even if a default candidate path exists. This is what
    the GPU ingest overlay (docker-compose.gpu.yml) sets, so GPU runs keep
    using SentenceTransformer/CUDA regardless of what else is mounted at
    /models. Unset (not present in the environment at all) falls through to
    the built-in default candidates.
    """
    raw = os.environ.get("SHIORI_ONNX_MODEL_PATH")
    if raw == "":
        return None

    def _has_onnx(p: Path) -> bool:
        # is_dir() guards iterdir(): a stray file at the candidate path
        # must not raise NotADirectoryError here.
        return p.is_dir() and any(f.suffix == ".onnx" for f in p.iterdir())

    if raw:
        # An explicitly configured path is a user choice: if it is unusable,
        # do NOT silently try the default candidates (that would load a
        # different model than the one pointed at) -- warn and use ST.
        p = Path(raw)
        if _has_onnx(p):
            return p
        log.warning(
            "SHIORI_ONNX_MODEL_PATH=%s has no .onnx model; "
            "falling back to SentenceTransformer (default candidates are "
            "not consulted when the path is explicitly set)",
            raw,
        )
        return None

    for p in _DEFAULT_ONNX_CANDIDATES:
        if _has_onnx(p):
            return p
    return None


def _pick_device() -> tuple[str, str]:
    """Best-effort device choice for SentenceTransformer: (device, reason).

    Detection must never raise out of here -- no torch installed, a broken
    driver or an odd runtime all resolve to CPU (issue #383).  The chosen
    device is passed to SentenceTransformer explicitly, so a CUDA
    *acquisition* failure can be retried once on CPU; the reason strings keep
    the log greppable: ``device=<name>: <reason>``.
    """
    try:
        import torch  # pyright: ignore[reportMissingImports]
    except ImportError:
        return "cpu", "auto-detected (torch unavailable)"
    try:
        if torch.cuda.is_available():
            return "cuda", "auto-detected"
        return "cpu", "auto-detected (no CUDA)"
    except Exception as e:  # noqa: BLE001 - deliberate: a broken detection must not kill the run
        log.warning(
            "CUDA detection failed (%s: %s); assuming CPU", type(e).__name__, e,
        )
        return "cpu", "detection failed; assuming CPU"


def _is_device_acquisition_failure(exc: BaseException) -> bool:
    """True only when the failure means *the device was not acquired*: CUDA
    out-of-memory (a game or another job holds the VRAM) or CUDA
    initialisation errors (no/faulty driver, kernel-image mismatch).

    Everything else -- a corrupt model file, a failed download, a bad config
    -- is a model problem that CPU does not fix, so it must keep propagating.
    Same standard as Embedder.__init__'s ImportError-only rescue: defensive
    code that swallows unrelated failures hides incidents instead of
    surfacing them.

    Exception types are resolved at call time: torch is an optional extra and
    cannot be referenced at module scope.
    """
    try:
        import torch  # pyright: ignore[reportMissingImports]
    except ImportError:
        return False
    cuda = getattr(torch, "cuda", None)
    oom = getattr(cuda, "OutOfMemoryError", None)
    if isinstance(oom, type) and isinstance(exc, oom):
        return True
    if isinstance(exc, RuntimeError):
        message = str(exc).lower()
        return any(marker in message for marker in (
            "found no nvidia driver",
            "nvidia driver",
            "cuda driver",
            "cuda error",
            "cublas",
            "cudnn",
            "cuda out of memory",
            "no kernel image",
        ))
    return False


class Embedder:
    def __init__(self, model_name: str | None = None):
        model_name = model_name or DEFAULT_EMBEDDING_MODEL
        self.model_name = model_name
        self._is_e5 = "e5" in model_name.lower()
        self._onnx_backend = False

        onnx_path = _resolve_onnx_path()
        if onnx_path is not None:
            try:
                self._init_onnx(onnx_path)
            except ImportError as e:
                # Covers ModuleNotFoundError: an ONNX model artifact is
                # present but optimum/onnxruntime isn't installed. Fall back
                # to SentenceTransformer rather than dying -- but only for
                # this specific, recoverable case. Any other exception
                # (corrupt model file, bad config, ...) still propagates:
                # defensive code that swallows unrelated failures hides
                # incidents instead of surfacing them.
                log.warning(
                    "ONNX model found at %s but the required library is "
                    "missing (%s); falling back to SentenceTransformer. "
                    "Install with: pip install 'shiori[onnx]'",
                    onnx_path, e,
                )
                self._init_st(model_name)
        else:
            self._init_st(model_name)

    def _init_onnx(self, onnx_path: Path) -> None:
        # Optional [onnx] extra -- absent by design in [dev] installs, so the
        # missing-import diagnostic is suppressed inline (config-file discovery
        # is cwd-dependent and cannot be relied on by every pyright caller).
        from optimum.onnxruntime import ORTModelForFeatureExtraction  # pyright: ignore[reportMissingImports]
        from transformers import AutoTokenizer  # pyright: ignore[reportMissingImports]

        onnx_file = next(f for f in onnx_path.iterdir() if f.suffix == ".onnx")
        log.info(
            "loading ONNX quantized model: %s (%s, %.0f MB)",
            onnx_path, onnx_file.name, onnx_file.stat().st_size / 1024 / 1024,
        )
        self._tokenizer = AutoTokenizer.from_pretrained(str(onnx_path))
        self._ort_model = ORTModelForFeatureExtraction.from_pretrained(
            str(onnx_path), file_name=onnx_file.name,
        )
        self.dim = self._ort_model.config.hidden_size
        self._onnx_backend = True

    def _init_st(self, model_name: str) -> None:
        # Optional [embed] extra (#179) -- same inline suppression as above.
        from sentence_transformers import SentenceTransformer  # pyright: ignore[reportMissingImports]

        # Resolve the device ourselves instead of letting the constructor
        # guess (issue #383): a failed CUDA acquisition must fall back to CPU,
        # while a model that fails to load must still propagate.
        device, reason = _pick_device()
        log.info(
            "loading embedding model: %s (pid=%d, fallback ST, device=%s: %s)",
            model_name, os.getpid(), device, reason,
        )
        try:
            self.model = SentenceTransformer(model_name, device=device)
        except Exception as e:
            # Retry once on CPU only for a device-acquisition failure (CUDA
            # OOM, CUDA init error).  A model that fails to load fails the
            # same way on CPU -- that must propagate, not be masked by a
            # second attempt -- and a failure on the CPU retry itself is by
            # definition not a device problem, so it propagates too.
            if not _is_device_acquisition_failure(e):
                raise
            log.warning(
                "CUDA acquisition failed for %s (%s: %s); "
                "device=cpu: fell back after a failure",
                model_name, type(e).__name__, e,
            )
            self.model = SentenceTransformer(model_name, device="cpu")
        self.dim = self.model.get_sentence_embedding_dimension()

    def _prep(self, texts: list[str], kind: str) -> list[str]:
        if not self._is_e5:
            return texts
        prefix = "query: " if kind == "query" else "passage: "
        return [prefix + t for t in texts]

    def embed_passages(self, texts: list[str], batch_size: int = 32):
        texts = self._prep(texts, "passage")
        if self._onnx_backend:
            return self._encode_onnx(texts, batch_size)
        return self.model.encode(
            texts, batch_size=batch_size,
            normalize_embeddings=True, show_progress_bar=False,
        )

    def embed_query(self, text: str):
        text = self._prep([text], "query")[0]
        if self._onnx_backend:
            return self._encode_onnx([text], 1)[0]
        return self.model.encode(text, normalize_embeddings=True)

    def _encode_onnx(self, texts: list[str], batch_size: int) -> np.ndarray:
        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            inputs = self._tokenizer(
                batch, padding=True, truncation=True, return_tensors="np",
            )
            outputs = self._ort_model(
                input_ids=inputs["input_ids"],
                attention_mask=inputs["attention_mask"],
            )
            pooled = _mean_pool(outputs.last_hidden_state, inputs["attention_mask"])
            all_embeddings.append(_l2_normalize(pooled))
        return np.concatenate(all_embeddings, axis=0)
