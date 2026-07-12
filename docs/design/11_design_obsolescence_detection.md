# Detailed Design: Design Obsolescence Detection

> **Draft**: Corresponds to Issue #74. Once acceptance criteria are validated, add these details to the `docs/design/00_basic_design.md` decision logs.

---

## 1. Purpose

Detect when design documents drift and fall out of sync with code implementations without relying on manual reviews. 

The validation baseline is set to **"the commit verified by a developer" (`reviewed_at`)** rather than the document's file modification timestamp.

This approach:
*   Minimizes false-positives caused by refactoring, formatting, or docstring edits that do not modify design rules.
*   Enables developers to record verification markers (`acknowledgment`) directly in document front-matter metadata on Git.

---

## 2. Relationship with Adjacent Designs

| Document | Scope | Difference from this Document |
|---|---|---|
| **Issues #22 / #31 / #35 (`shiori_status`)** | Ingestion sync lag (Index vs. Repository) | Independent. The index database can be fresh even if the spec itself is stale. |
| **Issue #78 (Doc ➔ Code Contradiction)** | Semantic contradictions | This document tracks if code files changed (Temporal axis), whereas #78 tracks what differs (Semantic axis). |

---

## 3. Structural Components

### 1. Document Front-Matter (Source of Truth)
Design documents define their scope and last verified commit inside their front-matter metadata block. Being committed to Git allows specifications and verification state to be reviewed in PR threads and managed throughout their lifecycle.

```yaml
---
shiori:
  covers:
    - src/shiori/github_sync.py
    - src/shiori/ingest.py
  reviewed_at: <commit-sha>   # The git commit SHA when the spec and code were aligned
  reviewed_on: 2026-06-15     # Human-readable verification date
---
```

**Storage**: The `doc_files` table adds `covers` (`text[]`), `reviewed_at` (`text`), and `reviewed_on` (`date`) columns. The ingestion pipeline parses these fields when scanning markdown files.

#### Alternatives Considered
*   **Central Manifest (`.shiori/links.yml`)**: Denied. Splitting configuration files away from target documents complicates PR reviews and increases maintenance costs when files are moved or deleted.
*   **Database-only Indexing**: Denied. Verification records would not reside in the Git repository, separating documentation state from development history.

### 2. Drift Calculation (git log drift based on `reviewed_at`)
```bash
git log <reviewed_at>..HEAD --oneline -- <covers_paths>
```

If any commits are returned, the code scope has been modified since the document was verified, indicating that the design spec requires a manual review.

#### History Depth Resolution
Because local repository clones default to `--depth=1` to save space, the local repository history might not contain the `reviewed_at` commit. Shiori resolves this by dynamically pulling historical commits:

```bash
git fetch --shallow-since=<reviewed_on> origin
```

If `reviewed_on` is missing, the fetch falls back to deepening the tree by a set number of commits (`--deepen=N`).

#### Drift Complexity Scoring
To help prioritize reviews, drift states are scored by change complexity:

$$\text{Score} = \text{changed\_lines} + (\text{changed\_files} \times 10) + \text{distinct\_authors}$$

Drift sensitivity can be adjusted per document using a `sensitivity: high|normal|low` front-matter option.

### 3. Verification Updates
When developers confirm that a design document matches the code, they update `reviewed_at` and `reviewed_on` in the front-matter. Committing these modifications resets the evaluation baseline for subsequent drift checks.

---

## 4. Tool Integration (`shiori_stale` Tool)

Drift audits are exposed via a dedicated tool `shiori_stale` to prevent cluttering `shiori_status`:

```python
shiori_stale(
    repo: str | None = None,
    top_k: int = 20,          # Returns top N entries
    min_commits: int = 1,     # Commit count threshold to trigger alert
) -> list[StaleEntry]
```

### StaleEntry Payload Structure
```python
@dataclass
class StaleEntry:
    doc_path: str
    reviewed_at: str          # Verified commit SHA
    reviewed_on: date | None
    covers: list[str]         # Tracked code paths
    drift_commits: list[str]  # List of "SHA - Commit Message" pairs since reviewed_at
    score: int                # Change complexity score
```

---

## 5. Schema Alterations
Add the following metadata columns to `doc_files`:

```sql
ALTER TABLE doc_files
  ADD COLUMN covers       text[],
  ADD COLUMN reviewed_at  text,
  ADD COLUMN reviewed_on  date;
```

`chunks` schema remains unmodified.

---

## 6. Path Tracking
If files are renamed, `covers` paths might point to missing files. Shiori does not automate path updates in v1.0. Instead, it flags missing files during audits:
*   `shiori_stale` reports missing target paths in a `missing_paths` array.

---

## 7. Acceptance Criteria

- [ ] Add `covers`, `reviewed_at`, and `reviewed_on` columns to `doc_files` table via migrations.
- [ ] Parse `shiori:` keys from YAML front-matter during Markdown ingestion.
- [ ] Implement drift detection logic via `git log reviewed_at..HEAD`, pulling history on-demand if missing.
- [ ] Implement the `shiori_stale` tool on the MCP server, returning entries sorted by complexity score.
- [ ] Report missing file targets inside `missing_paths` warnings.
- [ ] Guarantee via tests that drift evaluation relies on `reviewed_at` rather than doc modification times (e.g., modifying code triggers drift, but editing docs does not).
- [ ] Document that `shiori_stale` is separate from `shiori_status` responsibilities.

---

## 8. Non-Goals

*   Modifying sync freshness policies (Issue #22 / #31 / #35).
*   Automating code-to-doc mappings.
*   Automated path rename adjustments.
*   Semantic contradiction checking (Issue #78).

---

## 9. Decisions

| Decision | Implementation | Rationale |
|---|---|---|
| **YAML Front-matter is Truth** | Covers and verified hashes are defined inside document headers. | Integrates with PR reviews and git lifecycles. |
| **Dedicated `shiori_stale` Tool** | Expose via a standalone MCP tool rather than `shiori_status`. | Prevents status endpoint from growing too complex. |
| **On-Demand History Fetch** | Retrieve history via `git fetch --shallow-since` as needed. | Decouples from external APIs and supports shallow checkouts. |
| **Commit-Based Verification** | Evaluate drift against `reviewed_at` hashes instead of file times. | Reduces false-positives during minor format edits. |
