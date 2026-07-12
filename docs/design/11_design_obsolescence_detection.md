# Detailed Design: Design Obsolescence Detection

## 1. Purpose

Detect when design documents drift and fall out of sync with code implementations. 

Instead of relying on fragile "last updated" file modification timestamps, Shiori evaluates drift by comparing document-defined cover paths against target git commit hashes (`reviewed_at`).

This design:
*   Minimizes false-positives caused by refactoring or formatting edits.
*   Enables developers to record verification markers directly in document front-matter headers.

---

## 2. Structural Design

### 1. Document Front-Matter Configuration
Developers link design documents to implementation files by adding a `shiori` metadata block to the document's YAML front-matter:

```yaml
---
shiori:
  covers:
    - src/shiori/github_sync.py
    - src/shiori/ingest.py
  reviewed_at: <commit-sha>   # The git commit SHA when the spec matched the code
  reviewed_on: 2026-06-15     # Human-readable date
---
```

When documents are synced, Shiori parses this block and saves the values into the `doc_files` table (`covers`, `reviewed_at`, `reviewed_on`).

### 2. Git Drift Calculation
To determine if a document is out of date, Shiori runs the equivalent of:

```bash
git log <reviewed_at>..HEAD --oneline -- <covers_paths>
```

If any commits are returned, the code has been updated since the document was last verified.

#### Dynamic Depth Fetching
Because local repository clones default to `--depth=1` to save space, the local repository history might not contain the `reviewed_at` commit. Shiori resolves this by dynamically pulling historical commits:

```bash
git fetch --shallow-since=<reviewed_on> origin
```

If `reviewed_on` is missing, the fetch falls back to deepening the tree by a set number of commits (`--deepen=N`).

### 3. Drift Scoring
Drifts are scored by evaluated complexity to help prioritize document reviews:

$$\text{Score} = \text{changed\_lines} + (\text{changed\_files} \times 10) + \text{distinct\_authors}$$

---

## 3. Tool Integration (`shiori_stale` Tool)

Rather than cluttering `shiori_status`, drift audits are exposed via a dedicated tool `shiori_stale`:

```python
shiori_stale(
    repo: str | None = None,
    top_k: int = 20,
    min_commits: int = 1,
) -> list[StaleEntry]
```

### StaleEntry Payload Structure
```python
@dataclass
class StaleEntry:
    doc_path: str
    reviewed_at: str
    reviewed_on: date | None
    covers: list[str]
    drift_commits: list[str]  # List of "SHA - Commit Message" pairs
    score: int
    missing_paths: list[str]  # Covers paths that no longer exist in the repository
```

---

## 4. Schema Changes
The `doc_files` table is modified to store front-matter configurations:

```sql
ALTER TABLE doc_files
  ADD COLUMN covers       text[],
  ADD COLUMN reviewed_at  text,
  ADD COLUMN reviewed_on  date;
```
