# Detailed Design: Search & Hybrid Search

## 1. Purpose

Combine semantic vector search with keyword matching to rank query results. Returns pointers and text snippets rather than full documents.

---

## 2. Hybrid Search Mechanics

*   **Semantic Vector Search (pgvector)**: Handles concept mappings, synonyms, and cross-lingual matching (e.g. mapping Japanese queries to English specs).
*   **Keyword Match (pgroonga)**: Handles exact identifier queries (variable names, API routes, error codes).
*   **Fusion (RRF)**: Merges vector similarity rankings and keyword match rankings using Reciprocal Rank Fusion (RRF, k=60).
*   **Filters**: Applies metadata filters (`source_type`, `language`, `state`, timestamps) in the SQL `WHERE` clause.

---

## 3. Relevance vs. Recency Ranking (Issue #41)

Search queries default to **relevance ranking (RRF)**. We omit direct chronological (date-based) sorting for specifications since spec files do not have a chronological correctness gradient.

### Ranking Strategies by Source

*   **Primary Sources (Docs / Code)**: Ranked strictly by relevance (RRF). Chronological sorting is disabled.
*   **Secondary Sources (Issues / PR Reviews)**: Chronological sorting is not supported as a standalone replacement. Instead, candidates are ranked by relevance, with `state` and `updated_at` used as secondary tie-breakers.

### Retrieval Intentions and Rankings

| Intent | Example Query | Ideal Sorting | Shiori Implementation |
|---|---|---|---|
| **Design Intent** | "Where was this schema decided?" | Relevance + Conclusion | Relevance + Conclusion Boost (e.g. merged PRs) |
| **Prior Decisions** | "Has this approach been rejected?" | Relevance + `state` | Relevance (includes closed/rejected issues) |
| **Current specs** | "What is the current policy?" | Relevance | Query docs (primary specs) |
| **Chrono triage** | "What issues closed recently?" | Chronological list | *Out of Scope* (Delegate to GitHub MCP) |
| **Historical 変遷** | "How did this design evolve?" | Chronological thread | Query topic + `shiori_read_issue` sequentially |

Chronological triage (listing recently modified issues) is delegated to the GitHub MCP. Shiori avoids duplicating raw VCS indexing.

---

## 4. Search Decoupling

*   The primary search API `shiori_search` defaults to relevance ranking. Secondary metadata (`state`, `updated_at`) serves as a tie-breaker.
*   Tie-breaking is evaluated on the database side *before* top-k truncation to prevent pagination errors.
*   `shiori_keyword_search` is kept separate to handle exact identifier lookups.
