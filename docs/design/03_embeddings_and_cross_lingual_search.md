# Detailed Design: Embeddings & Cross-Lingual Search

## 1. Purpose

Design vector representation logic for semantic retrieval, optimized for local execution on bilingual (Japanese and English) datasets.

---

## 2. Model Selection Criteria

*   **Cross-Lingual Search**: The vector model must map Japanese and English into a shared vector space. This allows Japanese queries to match English specifications or discussions (and vice versa).
*   **Local Execution**: The model must run locally without sending private repository code or text to external API endpoints.
*   **Caching**: Model weight files (usually several hundred MBs) must be cached in a named volume to prevent redownloading during image updates.

---

## 3. Decisions (v1.0)

*   **Default Model (`intfloat/multilingual-e5-small`)**: We select this 384-dimension model as the default. It supports cross-lingual search and is lightweight enough to run CPU inference quickly. The model is baked into the Docker image at build time and loaded with `HF_HUB_OFFLINE=1` at runtime (issue #238). Runtime env var override is not supported — to change the model, fork the image and rebuild (#255).
*   **E5 Prefixing**: The required E5 query/passage prefixes (`query: ` and `passage: `) are automatically prepended by the embedding class if the model name contains `e5`.
*   **Normalization & Indexing**: Vectors are normalized before insertion and indexed using pgvector's HNSW method (`vector_cosine_ops`). HNSW parameters are set to pgvector defaults (`m=16`, `ef_construction=64`).
*   **Inference Caching & Image-Baked Weights**:
    *   *Incremental Sync*: Runs `embed_passages` with a default batch size of 32.
    *   *Bulk Load (Issue #72)*: `ChunkBuffer` buffers chunks and sends them in large passes to `embed_passages` to prevent thread thrashing.
    *   Model weights are pre-downloaded into the image during `docker build` (`docker/app/Dockerfile`). The default model (`intfloat/multilingual-e5-small`) is baked into `/models` at build time and loaded with `HF_HUB_OFFLINE=1` at runtime, eliminating all startup-time network requests to HuggingFace (issue #238).

---

## 4. ONNX INT8 CPU-inference path (issue #353)

PR #343 introduced an ONNX Runtime (INT8 quantized) code path in `Embedder`
for faster CPU inference, alongside the SentenceTransformer path above.
Issue #353 made it actually usable: the `[onnx]` extra is declared
(`optimum[onnxruntime]>=1.16`, kept separate from `[embed]`/`[dev]` since it
pulls large wheels not needed for tests or the plain ST path), the app image
installs it (`docker/app/Dockerfile` installs `.[embed,onnx]`), and the
model artifact itself is a host-built, bind-mounted file rather than
something baked into the image or produced in CI.

**When it engages.** `Embedder.__init__` calls `_resolve_onnx_path()`; a
non-`None` result makes it choose ONNX unconditionally over
SentenceTransformer. Resolve order:

1. `SHIORI_ONNX_MODEL_PATH` from the environment, if set and non-empty.
   An explicitly set path is a user choice: if it has no usable model, the
   default candidates are **not** consulted (that would silently load a
   different model than the one pointed at) — a warning is logged and the
   SentenceTransformer path is used instead.
2. Only when the variable is unset: the built-in default candidates
   (`/models/onnx/e5-small-int8`, then `/models/onnx/e5-small`).

A candidate is used only if it's a directory containing at least one
`*.onnx` file; a stray file at the path is treated as "not found" rather
than raising.

**The empty-string off-switch.** `SHIORI_ONNX_MODEL_PATH=""` (set but
explicitly empty) is different from unset: it disables ONNX outright and
`_resolve_onnx_path()` returns `None` even if a default candidate path has a
real model in it. `docker-compose.gpu.yml` sets this on the `ingest`
service so GPU ingest runs keep using SentenceTransformer/CUDA regardless of
what's mounted at `/models` -- the ONNX path is CPU-only and offers no
benefit there.

**Who benefits.** Primarily the serve path's query-time embedding: the MCP
app container runs CPU torch, so `search`/`keyword_search` latency improves
with INT8 CPU inference. Secondarily, the CPU steady-sync ingest lanes
(#347 timers) also read the same mount.

**Building the artifact.** `scripts/build_onnx_model.py` exports
`DEFAULT_EMBEDDING_MODEL` (or `--model`) to ONNX and applies dynamic INT8
quantization, writing to `--output` (default `models/onnx/e5-small-int8`
under the repo root). It requires network access (downloads the model from
the Hugging Face Hub) and is a host-side, occasional step -- never run in CI
or in tests. Typical invocation, using the app image (which already has the
`[onnx]` extra; most host venvs don't):

```
docker compose run --rm app python scripts/build_onnx_model.py
```

`docker-compose.yml` bind-mounts `./models:/models:ro` into both the `app`
and `ingest` services; `models/` is gitignored, so the artifact stays a
local/host concern.

**Fallback behavior.** If a model is present at the resolved path but the
`[onnx]` extra isn't installed, `_init_onnx()` raises `ImportError`
(`ModuleNotFoundError` for `optimum`/`transformers`). `Embedder.__init__`
catches specifically `ImportError`, logs a warning naming the missing
library and the fix (`pip install 'shiori[onnx]'`), and falls back to
`_init_st()`. Any other exception (e.g. a corrupted model file) is left to
propagate -- silently degrading on an unexpected failure would hide a real
incident instead of surfacing it.
