# Detailed Design: Datastore & Schema

## 1. Purpose

Consolidate vector representations, text tokens, and metadata under a single PostgreSQL database to execute hybrid retrieval and filtering in a single SQL query.

---

## 2. Technologies

*   **PostgreSQL**: The relational database engine.
*   **pgvector**: Manages vector embeddings and similarity metrics.
*   **pgroonga**: Handles multi-language full-text search. Attempts to use `TokenMecab` (morphological parsing) first, falling back to `TokenBigram` if Mecab is unavailable.
*   **Structured Columns**: Holds metadata values (`repo`, `path`, `source_type`, `language`, `state`, timestamps).

This consolidation allows metadata filters (`WHERE` clause) and hybrid scoring (RRF) to be computed natively in SQL, eliminating the need to coordinate synchronization or result merging between separate search engines.

---

## 3. Schema Design

```sql
CREATE TABLE chunks (
    id BIGSERIAL PRIMARY KEY,
    chunk_key TEXT NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    source_type TEXT NOT NULL CHECK (source_type IN ('doc', 'issue', 'pr_review', 'code')),
    repo TEXT NOT NULL,
    path TEXT,
    issue_no INTEGER,
    comment_id BIGINT,
    language TEXT,
    heading_path TEXT,
    content TEXT NOT NULL,
    embedding vector({dim}),
    state TEXT,
    author TEXT,
    line INTEGER,
    end_line INTEGER,
    commit_sha TEXT,
    prog_lang TEXT,
    symbols TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    url TEXT,
    UNIQUE (chunk_key, chunk_index)
);
```

---

## 4. Indexes

### Lightweight Indexes (Always Active)
*   `chunks_repo_idx` (btree, repo)
*   `chunks_source_type_idx` (btree, source_type)
*   `chunks_updated_at_idx` (btree, updated_at)
*   `chunks_repo_issue_no_idx` (btree, repo, issue_no)

### Heavyweight Indexes (Deferred on Bulk Runs, Issue #72)
*   `chunks_embedding_hnsw` — HNSW (cosine) for vector search.
*   `chunks_content_pgroonga` — pgroonga on `content` for keyword search.
*   `chunks_symbols_pgroonga` — pgroonga on `symbols` for code identifier tokenization (Issue #33).

### Bulk Load Optimizations (Issue #72)
To optimize initial ingestion performance:
1.  **Lightweight Init**: `migrate_light()` builds tables and B-tree indexes only. Heavy indexes are dropped or deferred.
2.  **Bulk Insert**: Chunks are loaded in bulk using `executemany` with no active HNSW or pgroonga index updates.
3.  **Heavyweight Build**: Once all data is loaded, `create_heavy_indexes()` creates HNSW and pgroonga indexes in a single pass.

Incremental sync runs maintain all indexes actively during inserts.

---

## 5. Operations Tables

*   `sync_state(repo, kind, cursor)`: Keeps sync cursors.
*   `sync_runs(repo, route, finished_at, docs_updated, issues_indexed, code_indexed)`: Ingestion stats log (Issue #22).
*   `doc_files(repo, path, content_sha, language, kind)`: Tracks document content hashes.
*   `issue_items(repo, issue_no, comment_id, kind, title, author, is_bot, state, path, line, body, url, created_at, updated_at)`: Raw timeline data for `shiori_read_issue`.
*   `pr_changes(repo, issue_no, head_sha, path, status, additions, deletions, changes, blob_url)`: Tracked PR change details (Issue #54).
