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

*   **Default Model (`intfloat/multilingual-e5-small`)**: We select this 384-dimension model as the default. It supports cross-lingual search and is lightweight enough to run CPU inference quickly. Larger models (e.g. `multilingual-e5-large` or `bge-m3`) are supported as configuration overrides.
*   **Configurable Models**: The model can be overridden using `EMBEDDING_MODEL` and `EMBEDDING_DIM` environment variables. If the dimension changes, the index must be rebuilt using `shiori ingest --rebuild`. Dimension mismatches are caught during startup, raising initialization errors.
*   **E5 Prefixing**: The required E5 query/passage prefixes (`query: ` and `passage: `) are automatically prepended by the embedding class if the model name contains `e5`.
*   **Normalization & Indexing**: Vectors are normalized before insertion and indexed using pgvector's HNSW method (`vector_cosine_ops`). HNSW parameters are set to pgvector defaults (`m=16`, `ef_construction=64`).
*   **Inference Caching & Image-Baked Weights**:
    *   *Incremental Sync*: Runs `embed_passages` with a default batch size of 32.
    *   *Bulk Load (Issue #72)*: `ChunkBuffer` buffers chunks and sends them in large passes to `embed_passages` to prevent thread thrashing.
    *   Model weights are pre-downloaded into the image during `docker build` (`docker/app/Dockerfile`). The default model (`intfloat/multilingual-e5-small`) is baked into `/models` at build time and loaded with `HF_HUB_OFFLINE=1` at runtime, eliminating all startup-time network requests to HuggingFace (issue #238).
