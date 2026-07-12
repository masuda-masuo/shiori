# Detailed Design: Multi-Source Abstraction (Cross-Product Search)

> **Status: Future Direction (Unscheduled)**. This document outlines an abstract design to index and search knowledge scattered across multiple platforms (e.g., "issues in Jira, specs in Confluence") under Shiori's single search index.
>
> **Current Policy**: We do not implement this. It is a future roadmap blueprint. The purpose of this document is to establish design boundaries so we do not block future extensibility. Currently, developers only need to keep the "Extension Guardrails" below in mind. The subsequent schema changes, connector abstractions, and phase plans represent the blueprint for future work and are not current TODO items. When implemented, core definitions in `00_basic_design.md` (§3, §4, §5) and `13_product_definition_and_use_cases.md` must be updated (see § "Basic Design Integration" below).

---

## 1. Extension Guardrails (Maintaining Future Extensibility)

When writing new code, follow these guidelines to keep the multi-source pathway open at zero additional cost:

*   **Treat `source_type` as a category of knowledge, not a product**: Tag entries as `issue`, `doc`, `pr_review`, or `code`. Do not assume `issue` is tightly coupled only to "GitHub Issues".
*   **Keep `chunk_key` opaque**: Do not write parsing logic that assumes `chunk_key` follows a GitHub-specific format (e.g. splitting by `:` to extract coordinates).
*   **Avoid embedding GitHub-specific schemas in core search logic**: Do not write queries that assume `issue_no` is always an integer.
*   **Differentiate product logic from category logic**: Wrap product-specific behaviors (e.g., API payloads) inside connector wrappers, leaving the database tables neutral.

Conversely, **no pre-emptive coding is required**. The `source` column and connector abstractions can be added when needed (the schema is designed to migrate existing rows without modifications — see § "Design" below).

---

## 2. Purpose

Shiori currently indexes and queries GitHub-specific knowledge (docs, issues/PRs, code). However, in production, project details are often distributed across multiple platforms:
*   **Issues / Tickets** ➔ Jira
*   **Specifications / Wikis** ➔ Confluence, Notion, Google Drive
*   **Code / PRs** ➔ GitHub, GitLab

By separating the **category of knowledge** (e.g., "issue", "spec") from the **source product** (e.g. GitHub, Jira), Shiori can aggregate these items into a single search index. This extends Shiori's core value—unified cross-referencing and navigation—beyond the boundaries of GitHub.

---

## 3. Background: Why Shiori is Suited for this Extension

A massive rewrite is unnecessary because the "hard parts" of cross-source search are already source-independent:
1.  **Source-Neutral Datastore**: The `chunks` table (see `04`) is a single table, and `search.py` filters results using generic columns like `source_type`, `repo`, and `state`. The core of hybrid search—combining `pgvector`, `pgroonga`, RRF ranking, and cross-lingual embeddings—does not care where chunks originate.
2.  **Pointer-then-Fetch Architecture**: Shiori does not store full text for in-flight resources; it returns coordinates and URLs, delegating full-text retrievals to other servers (like the GitHub MCP). This delegation model easily generalizes to a Jira MCP or Confluence MCP.
3.  **Local Filesystem Precedent**: [Filesystem Ingestion](08_filesystem_ingestion.md) establishes a precedent for co-existing non-GitHub sources (using `repo="fs:{name}"`) in a single table without modifying chunking, embedding, or search modules.

Therefore, multi-source abstraction boils down to: **(a) introducing a source dimension, (b) defining a connector interface, and (c) generalizing coordinates.**

---

## 4. Conceptual Model: Separating `source` and `source_type`

The current `source_type` (`doc` / `issue` / `pr_review` / `code`) implicitly blends two orthogonal concepts:
*   **Where** the knowledge comes from (implicitly GitHub).
*   **What kind** of knowledge it is (document, issue, code, etc.).

Multi-source abstraction separates this into two dimensions:

| Dimension | Meaning | Examples |
|---|---|---|
| **`source`** | Where the knowledge originates (Connector name) | `github` / `jira` / `confluence` / `fs` / `gitlab` |
| **`source_type`** | What category of knowledge it is | `doc` / `issue` / `code` / `pr_review` |

### Matrix Mapping Examples

| Asset | `source` | `source_type` |
|---|---|---|
| GitHub Issue | `github` | `issue` |
| Jira Ticket | `jira` | `issue` |
| main branch Markdown | `github` | `doc` |
| Confluence Page | `confluence` | `doc` |
| Local Notes | `fs` | `doc` |

Queries like `shiori_search(source_type="issue")` will rank GitHub issues and Jira tickets **together in a single list**.

---

## 5. Design Specifications

### 1. Database Schema Extensions (Extending `04`)
Add a `source` column to `chunks` (and `issue_items`). Existing rows default to `'github'`.

```sql
ALTER TABLE chunks ADD COLUMN source TEXT NOT NULL DEFAULT 'github';
CREATE INDEX chunks_source_idx ON chunks (source);
-- The CHECK constraint on source_type is updated to restrict categories only.
```

#### Generalizing Coordinates
The current natural key columns are mapped to GitHub structures: `repo` (owner/name), `issue_no` (INTEGER), and `comment_id` (BIGINT). Jira keys (e.g. `PROJ-123`) or Confluence page IDs are alphanumeric strings and cannot fit into an integer `issue_no`.
*   **`chunk_key` (text)**: Remains a source-neutral opaque key (e.g., `doc:repo:path`). Connectors define their own namespace keys (e.g., `jira:PROJ-123:comment:456`), requiring no schema changes.
*   **`repo` (text)**: Redefined as a generic "Scope Key". Similar to the filesystem precedent (`repo="fs:notes"`), it stores workspace scopes like `jira:PROJ` or `confluence:SPACE`. GitHub continues to use `owner/name`. The column name remains `repo` for backward compatibility but is conceptually treated as the scope key.
*   **`native_id` (text, New)**: Stores the source-neutral alphanumeric ID (e.g. `PROJ-123`, `#42`, or Confluence page ID). The legacy `issue_no` remains as a database projection for GitHub assets. Search pointers return `{source, scope(repo), native_id, url}`.

Migrations use the existing idempotent `ADD COLUMN IF NOT EXISTS` pattern in `_run_alter_statements` (`db.py`). Existing GitHub setups run without modifications with `source='github'` and `native_id=NULL`.

### 2. Connector Interface (`SourceConnector` Protocol)
Currently, `ingest.py` calls `sync_docs`, `sync_issues`, and `sync_code` in `github_sync.py` directly. This will be refactored into a registry of connectors implementing a shared protocol:

```python
from typing import Protocol

class SyncResult:
    chunks_added: int
    items_deleted: int

class SourceConnector(Protocol):
    source: str                       # e.g., 'github' | 'jira' | 'confluence'
    
    def scopes(self, settings) -> list[str]:
        """Returns sync targets (projects, spaces, or repos)."""
        
    def sync(self, settings, conn, embedder, scope, *, buffer=None) -> SyncResult:
        """Runs incremental sync, indexes chunks, and saves raw records. Returns counts."""
```

*   `github_sync` is wrapped as `GitHubConnector` implementing this protocol.
*   `fs_sync` (see `08`) is wrapped as `FsConnector`.
*   `ingest.py` traverses registered connectors:
    ```python
    for connector in registered_connectors:
        for scope in connector.scopes(settings):
            connector.sync(settings, conn, embedder, scope)
    ```
*   Connectors manage their own authentication, API pagination, rate limits, sync cursors, and chunk normalization.

### 3. Search Layer Modifications (Extending `05`)
Modify `_filter_sql` in `search.py` to support `source` (matching the `source_type` filter implementation). RRF and ranking logic (`_rank_candidates`) remain unchanged. `SearchHit` is updated to include `source` and `native_id` pointers.

Relevance tie-breaking for secondary sources remains based on `source_type`.

### 4. Inspection & Relationship Layers (Layers 2 & 3)
While Layer 1 (Search) is source-neutral, Layers 2 and 3 (Inspection and Relationships) contain product-specific tools (e.g., PR diffs have no Jira equivalent).
*   **Dispatch by Source**: Search pointers contain `source`, allowing inspection tools to dispatch calls (e.g. `shiori_read_issue` routes to GitHub API calls or Jira API calls based on the source value).
*   **Delegated Actions**: Full-text retrievals are delegated to respective platform MCPs (GitHub MCP, Jira MCP, Confluence MCP), preserving the pointer-then-fetch philosophy.

---

## 6. Implementation Phases

We phase deployment to ensure existing GitHub configurations remain unaffected:

*   **Phase 0 (This Document)**: Decision phase. Define `source`/`source_type` boundaries.
*   **Phase 1 (Abstraction Layer)**: Add the `source` column, introduce the `SourceConnector` protocol, wrap `github_sync`, and add `source` filters to the search query. No new connectors are added.
*   **Phase 2 (Coordinate Generalization)**: Add `native_id`, refactor inspection tools to dispatch by source, and implement the local `fs` connector as a proof-of-concept.
*   **Phase 3 (First External Source)**: Implement the Jira or Confluence connector. Measure search accuracy and performance against real datasets.

---

## 7. Open Questions

*   **First Target Selection**: Verify if Jira (Issue-centric) or Confluence (Doc-centric) provides the highest immediate value.
*   **Cursor Schema**: Determine if `sync_state.cursor` should be closed inside connector modules or split into generic tables.
*   **Cross-Source Linking**: Extracting links across boundaries (e.g. parsing a `PROJ-123` string in a GitHub PR and resolving it as a Jira pointer).

---

## 8. Basic Design Integration

*   `00` §3: Add "External Products (Jira/Confluence)" to Data Sources.
*   `00` §4: Add "Connector Abstraction" to Key Components.
*   `00` §5: Add "Source/SourceType Separation" to Decision Logs.
*   `13` §1: Generalize "GitHub Repositories" to "Project Knowledge Sources".
*   `04`: Document `source` and `native_id` columns.
*   `08`: Re-map the `fs` source as the second implementation of the connector registry.
