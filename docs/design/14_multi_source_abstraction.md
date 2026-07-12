# Detailed Design: Multi-Source Abstraction

> **Status: Future Direction (Unscheduled)**. This document proposes an abstract design to index and query knowledge from multiple sources (e.g. Jira, Confluence, Notion) alongside GitHub.
>
> **Current Policy**: We do not implement this. It is a future roadmap blueprint. Developers should follow the guardrails below to ensure future extensibility is not blocked.

---

## 1. Extension Guardrails

When writing new code or modifying the database, follow these rules to keep the architecture extensible at zero cost:

*   **Treat `source_type` as a category of knowledge, not a product**: Tag entries as `issue`, `doc`, or `code`. Avoid coupling terms to "GitHub Issue" or "Git Clone" in core modules.
*   **Keep `chunk_key` opaque**: Do not write parser logic that assumes `chunk_key` follows a GitHub-specific format (e.g. splitting by `:` to extract GitHub URLs).
*   **Avoid embedding GitHub-specific schemas in core search logic**: Do not write queries that assume `issue_no` is always an integer.
*   **Differentiate product logic from category logic**: Wrap product-specific behaviors (e.g., API payloads) inside connector wrappers, leaving the database tables neutral.

---

## 2. Abstraction Framework

The architecture separates the product source from the category of knowledge:

*   **`source` (Product Location)**: e.g. `github`, `jira`, `confluence`, `fs`.
*   **`source_type` (Category)**: e.g. `doc`, `issue`, `code`, `pr_review`.

### Examples
*   GitHub Issue: `source="github"`, `source_type="issue"`
*   Jira Ticket: `source="jira"`, `source_type="issue"`
*   Confluence Page: `source="confluence"`, `source_type="doc"`
*   Local File: `source="fs"`, `source_type="doc"`

Under this structure, a query like `shiori_search(source_type="issue")` searches both GitHub issues and Jira tickets in a single hybrid ranking.

---

## 3. Database Updates

The schema handles non-GitHub platforms by adding generic columns:

*   **`source` (text)**: Identifies the source platform (defaults to `'github'`).
*   **`repo` (text)**: Redefined as a generic "Scope Key" (e.g., `jira:PROJ`, `confluence:SPACE`). GitHub values continue to use `owner/name`.
*   **`native_id` (text)**: A generic identifier representing the source ID (e.g. `PROJ-123`, `PAGE-99`).

```sql
ALTER TABLE chunks ADD COLUMN source TEXT NOT NULL DEFAULT 'github';
ALTER TABLE chunks ADD COLUMN native_id TEXT;
CREATE INDEX chunks_source_idx ON chunks (source);
```

---

## 4. Connector Interface (`SourceConnector`)

Sync jobs are decoupled using a registry of connectors implementing a shared protocol:

```python
from typing import Protocol

class SyncResult:
    chunks_added: int
    items_deleted: int

class SourceConnector(Protocol):
    source: str  # 'github', 'jira', etc.
    
    def scopes(self, settings) -> list[str]:
        """Returns sync targets (projects, spaces, or repos)."""
        
    def sync(self, settings, conn, embedder, scope, *, buffer=None) -> SyncResult:
        """Syncs a single scope, indexes chunks, and caches raw timeline states."""
```

This abstracts API connections, cursor states, rate limits, and authentication wrappers from the main `ingest` command loop.
