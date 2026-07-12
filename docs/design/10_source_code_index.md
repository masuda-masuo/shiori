# Detailed Design: Source Code Indexing

## 1. Purpose & Background

Enable Shiori to index source code definitions alongside Markdown documents and issue timelines. 

In prior versions, when AI agents checked design documents against implementations, source code structures were invisible to Shiori's search index. This required agents to run separate file system tools to locate references.

By indexing code structures in a "map-like" index (signatures and docstrings) and keeping the full-text reads deferred to `shiori_read_file` (the *pointer-then-fetch* pattern), we enable cross-lingual matching (e.g. Japanese queries finding English method definitions) without bloating the context window.

---

## 2. Key Decisions

*   **Map-Like Indexing**: We parse code structures into classes, functions, and methods using tree-sitter. We do not store the full code body in the index database.
    *   *Content*: Stores signatures, parameters, docstrings, and comments.
    *   *Path/Hierarchy*: Parent contexts are saved in `heading_path` (e.g. `module.py > class Foo > def bar`).
    *   *Ranges*: Stores `start_line` and `end_line`. AI agents are directed to `shiori_read_file` to fetch the actual code body.
*   **Tokenizing Identifiers (`symbols` Column)**: Code RAG relies heavily on exact matching of identifiers (variables, class names, API paths). Because standard Mecab and Bigram tokenizers do not split camelCase or snake_case boundaries correctly, Shiori walks the AST, splits identifiers at boundaries, and stores them in a space-separated `symbols` column. Full-text search filters lookups on `content OR symbols`.
*   **Language Mapping**: To keep metadata searches clean, the programming language name is stored in a dedicated `prog_lang` column, leaving the `language` column set to `NULL` for code chunks.
*   **Differential Tracking**: File modification states are tracked in `doc_files` by adding a `kind` column (`doc` or `code`), avoiding database schema bloat.
*   **Allowlists and Glob Filtering**: Indexing is guarded by configuration keys: `SHIORI_INDEX_CODE` (defaults to `False`), `SHIORI_CODE_EXTENSIONS`, and `SHIORI_CODE_EXCLUDE_GLOBS`. Folders containing dependencies or builds (e.g. `node_modules`, `.venv`, `dist`) are ignored. Files in unsupported languages fall back to plain-text line-limit chunking.

---

## 3. Schema Adjustments

| Table | Modification |
|---|---|
| `chunks` | Add `'code'` to `source_type` check constraints. |
| `chunks` | Add `end_line` (integer) and `commit_sha` (text). |
| `chunks` | Add `prog_lang` (text) and `symbols` (text with pgroonga indexing). |
| `doc_files` | Add `kind` (text, default `'doc'`). |
| `sync_runs` | Add `code_indexed` (integer) to track sync volumes. |

---

## 4. Implementation Steps

1.  **Expose `shiori_list_tree`**: Updated to support `source_type` (`doc` or `code`) and `extension` filters (Issue #43).
2.  **Schema Migration**: Idempotent SQL check and column alterations.
3.  **Parser Integration (`chunking.py`)**: Implement `split_code` leveraging tree-sitter to output heading paths, line offsets, and split symbols.
4.  **Sync loop (`github_sync.py`)**: Create `sync_code` to scan cloned branches, match content hashes in `doc_files`, and populate code chunks.
5.  **Search Wiring**: Modify `shiori_search` and `shiori_keyword_search` queries to look up split values in the `symbols` column and apply `prog_lang` filters.
