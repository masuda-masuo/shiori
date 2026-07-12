# Detailed Design: Chunking Strategy

## 1. Purpose

Slice raw documents and issue threads into retrieval chunks that maintain semantic context while remaining within optimal vector model sizes.

---

## 2. Ingest Types & Strategies

### Documents (Markdown)
*   **Structure-Aware Slicing**: Markdown files are sliced by heading boundaries (`#`, `##`, etc.). This preserves local document structure.
*   **Heading Path**: The nesting structure of the heading (e.g. `Architecture > Database > Schema`) is captured as metadata and appended to the chunk text to maintain context.
*   **Text Boundary Slicing**: To avoid cutting Japanese text in the middle of sentences or words, slices are made on sentence endings (`。`, `.`) and paragraph breaks.

### Issues and Pull Requests (Conversations)
*   Slicing by headings is ineffective for threaded issues. Instead, chunks are sliced by individual comments or replies.
*   The issue description and title are prepended to each comment chunk to maintain global context.
*   Review comments include code snippets (`diff_hunk`) as additional context.

---

## 3. Decisions (v1.0)

*   **Raw Discussion Thread Indexing**: Discussions are indexed in their raw state. Automatic summary generation is excluded from v1.0, as summarizing requires running LLM instances, which increases ingestion costs and compromises local execution speed. Chunks are split per comment, with the issue title prepended as `[Title]`.
*   **Max Chunk Size (1200 Characters)**: Chunks are sized by character length (up to 1200 characters, configurable via `SHIORI_CHUNK_MAX_CHARS`). We avoid token-based chunking because it depends on the active embedding model's tokenizer, which reduces portability. Splitting prioritizes punctuation (`。`, `．`, `！`, `？`, `!`, `?`) and empty lines. Longer, continuous blocks (e.g. code fences) are split mathematically.
*   **Heading Path Prepend**: Heading paths are stored in metadata and prepended to the text chunk as `[Grandparent > Parent > Child]` to guide both vector embeddings and keyword indexes. Markdown code block fences (`#`) are ignored when parsing headings.
*   **Language Heuristics**: A chunk is tagged as `ja` if Japanese characters (Hiragana, Katakana, Kanji) make up 20% or more of its content. Otherwise, it is tagged as `en`.
*   **Adjacent Chunk Access**: We do not implement explicit "get next/previous chunk" tools. For documents, agents can fetch surrounding text via `shiori_read_file`. For issues, agents can retrieve the full thread sequentially via `shiori_read_issue`.
