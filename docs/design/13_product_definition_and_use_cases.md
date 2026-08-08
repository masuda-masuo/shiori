# Detailed Design: Product Definition & Use Cases

> **Context**: Shiori was initially conceived as a database backend for Retrieval-Augmented Generation (RAG). However, as features were added, search capabilities expanded from documents to issue/PR threads and source code, and tools beyond search (reading files, viewing trees, traversing links) grew. Shiori has evolved into an "access layer for project knowledge." If the definition remains vague, users (both humans and AI agents) cannot determine when to leverage Shiori. This document defines Shiori's core product scope, tool indexing layers, and developer use cases.

---

## 1. Core Definition

**Shiori is a local MCP server that enables AI agents to search across and navigate between project knowledge scattered throughout GitHub repositories (documents, source code, issues, and PR threads).**

"Project Knowledge" refers to the collection of details required to answer questions about a project. It is structured into three categories:

| Category | Realization | Purpose |
|---|---|---|
| **Factual Specs (What / How)** | Documents and code on the main branch. | What does this project do? How does it behave? How is it used? |
| **History & Intent (Why)** | Issue descriptions, PR reviews, comment threads. | Why is it configured this way? Known bugs. Workarounds. |
| **In-Flight Changes** | Open PR modifications, diff maps. | What is currently changing? |

### Value Hierarchy

Shiori's values are categorized by their indispensability:

1.  **Unified Cross-Referencing (Core Strength)**: Indexing all three knowledge types under a single unified store allows a single search to rank specifications, code definitions, and discussions together. Agents can traverse repositories by going from a hit issue to its resolving PR (`shiori_issue_links`), from the PR to modified files (`shiori_pr_changes`), and on to design specs and implementation. Because standard GitHub search is neither semantic nor cross-lingual, closed GitHub threads are practically unreachable by agents without Shiori.
2.  **Cross-Lingual Matching**: Allows queries in Japanese to discover documents or issues written in English (and vice versa), preventing indexing from splitting across languages.
3.  **Context Optimization (Pointer-then-Fetch)**: A core design principle that requires search APIs to return lightweight pointers rather than full texts. This protects the LLM's context window from being cluttered by irrelevant files.

---

## 2. Why Shiori is Not a RAG Server

RAG (Retrieval-Augmented Generation) refers to a complete pipeline where search results are stuffed into a context window to **generate** answers. Shiori focuses strictly on retrieval and navigation and **does not generate answers or summaries**.

*   **Agent Ownership**: Generating summaries and drawing conclusions remains the responsibility of the AI agent. Shiori returns raw pointers, snippets, and coordinates (e.g. indexing issue comments in their raw state).
*   **Deferred Ingestion**: Shiori returns bookmarks, and the agent decides if it needs to fetch full texts via read tools.
*   **Navigation Services**: Shiori provides structured navigation tools (like `shiori_list_tree`, `shiori_issue_links`, `shiori_pr_changes`, `shiori_pr_diff`) that extend beyond standard keyword RAG queries.

---

## 3. Scope Boundaries

### In Scope (What Shiori Does)
*   **Unified Cross-Searching**: Index specs, code definitions, and discussion logs together, allowing links (issue ➔ PR ➔ modified files ➔ spec) to be traversed.
*   **Discovering Intent**: Searching historical discussions to find why design decisions were made.
*   **Pointer Delivery**: Keeping context clean via the pointer-then-fetch strategy.
*   **Cross-Lingual Search**: Bypassing language barriers for Japanese/English repositories.
*   **Local Execution**: Running CPU embeddings locally to preserve data privacy.

### Out of Scope (Delegated Actions)
*   **Answer Synthesis**: Shiori does not write summaries or answer questions directly. (Delegated to: Agent / LLM).
*   **VCS Mutations**: Shiori is read-only. Creating issues, modifying files, or merging branches is out of scope. (Delegated to: GitHub MCP / Git).
*   **Code Validation**: Executing tests or compiling builds is out of scope. (Delegated to: Sunaba MCP).
*   **Chronological Triage**: Listing recently closed issues or active notifications is out of scope. (Delegated to: GitHub MCP).
*   **Real-Time Assurances**: Indexes reflect snapshots of the last sync run. Current data freshness is self-reported in `shiori_status`.

---

## 4. Tool Taxonomy (4-Layer Model)

The 13 tools are structured based on user query intent. When adding new tools, confirm which category they occupy:

| Layer | Intent | Tool |
|---|---|---|
| **① Retrieval (Entry)** | Where is it written? | `shiori_search` / `shiori_keyword_search` / `shiori_grep` |
| **② Inspection** | What is written? | `shiori_read_file` / `shiori_read_issue` / `shiori_read_pr_file` / `shiori_list_tree` |
| **③ Relationships & Changes** | How is it connected? What changes? | `shiori_issue_links` / `shiori_pr_changes` / `shiori_pr_diff` / `shiori_pr_review_comments` |
| **④ Operations** | Is the index fresh? What is the repo structure? | `shiori_status` / `shiori_report` |

### ① Retrieval Layer — Core Search Funnel

*   `shiori_search`: Unified hybrid search (vector + keyword RRF). Strong at conceptual mapping, phrasing variations, and cross-lingual queries. Primary entry point.
*   `shiori_keyword_search`: Exact match and identifier search. Designed for function names, APIs, and error codes.
*   `shiori_grep`: Line-level ripgrep on cloned repositories. Used as a Stage-2 search to locate lines once files are identified.

*Note: Retrieval results return pointers (paths, anchors, issue numbers, snippets, and URLs) rather than full texts.*

Filters: `source_type` (`doc`/`issue`/`pr_review`/`code`), `kind` (`issue`/`pr`), `repo` (`"*"` for all), `path_prefix`, `prog_lang`, `state`, `updated_after`.

### ② Inspection Layer — Reading Content

*   `shiori_read_file`: Reads files from the main-branch clone (supports line ranges).
*   `shiori_read_issue`: Retrieves issue or PR timelines (descriptions, comments, reviews). Supports batching up to 50 items.
*   `shiori_read_pr_file`: Transparently reads files at PR head commits via Git wrappers (Issue #81).
*   `shiori_list_tree`: Lists indexed folder paths, filterable by `source_type` and `extension`.

### ③ Relationships & Changes Layer — Traversal

*   `shiori_issue_links`: Analyzes references in descriptions and comments (closes, duplicate, refs, mentions), returning target title and state (Issue #97).
*   `shiori_pr_changes`: Returns changed files in a PR (paths, status, and lines).
*   `shiori_pr_diff`: Calculates and returns unified Git diffs for a PR (supports path scoping).
*   `shiori_pr_review_comments`: Lists review comments with line numbers and file paths.

### ④ Operations Layer
*   `shiori_status`: Inspects index status, sync times, and warnings.
*   `shiori_report`: Generates structured reports (`stats`, `module_tree`, `symbol_index`, `api_reference`) from the clone and the search index.

---

## 5. Developer Use Cases

### UC-1: Specs Onboarding
*   **Question**: "Where is this feature's spec?" "What is the onboarding setup?"
*   **Flow**:
    ```
    shiori_search(query, source_type="doc")
      ➔ Matched document pointers and snippets
      ➔ shiori_read_file(path, start_line, end_line)
    ```
*   Allows developers or agents to locate specifications without parsing the entire repository tree. Cross-lingual matching permits Japanese queries to retrieve English documents.

### UC-2: Design Intent Audit ("Why")
*   **Question**: "Why did we adopt plan A instead of plan B?" "Where was this constraint decided?"
*   **Flow**:
    ```
    shiori_search(query, kind="issue")  # or kind="pr"
      ➔ Timeline thread pointers
      ➔ shiori_read_issue(number)
      ➔ shiori_issue_links(number)
    ```
*   Exposes implicit context (rejections, trade-offs) that does not exist in specs. Useful to verify if an optimization has been attempted before proposing design changes.

### UC-3: Code Discovery
*   **Question**: "Where is this routine implemented?" "Where is this function called?"
*   **Flow**:
    ```
    shiori_keyword_search(identifier, source_type="code")
      ➔ Target file pointers
      ➔ shiori_grep(pattern, path)
      ➔ shiori_read_file(path, start_line, end_line)
    ```
*   Conceptual queries ("where are credentials handled?") use `shiori_search(source_type="code")`. Alternatively, trees can be audited via `shiori_list_tree`.

### UC-4: Pull Request Reviews
*   **Question**: "What files does this PR change?" "What did other reviewers comment on?"
*   **Flow**:
    ```
    shiori_pr_changes(number)
      ➔ shiori_pr_diff(number, path?)
      ➔ shiori_read_pr_file(number, path)
      ➔ shiori_pr_review_comments(number)
      ➔ shiori_read_issue(number)
    ```
*   By combining this with `shiori_search` to fetch specifications, agents can verify if implementation changes match design documentation.

### UC-5: Bug & Troubleshooting Audits
*   **Question**: "Is this error known?" "What is the workaround?"
*   **Flow**:
    ```
    shiori_keyword_search(error_code_or_message)
      ➔ shiori_read_issue(number)
      ➔ shiori_issue_links(number)  # check duplicate or resolving PRs
      ➔ shiori_pr_changes(resolving_pr_number)
    ```

### UC-6: Specs and Code Alignment
*   **Question**: "Does this spec match the implementation?"
*   **Flow**:
    ```
    shiori_read_file(spec_path)
      ➔ shiori_keyword_search(identifier, source_type="code")
      ➔ shiori_grep(pattern) ➔ shiori_read_file(code_path, range)
    ```
*   Manual counterpart to [Design Obsolescence Detection](11_design_obsolescence_detection.md). Writing update commits is delegated to GitHub tools.

### UC-7: Multi-Repository Index Scans
*   **Question**: "Which repository uses this configuration?"
*   **Flow**:
    ```
    shiori_search(query, repo="*")  or shiori_grep(pattern, repo="*")
      ➔ Matches containing repo fields
    ```

### UC-8: Ingestion Validation
*   **Question**: "Is the local index fresh?"
*   **Flow**:
    ```
    shiori_status()
      ➔ Verify last_synced_at and warnings
    ```

### UC-9: Non-Duplicate Issue Creation
*   **Question**: "Has this bug been reported?"
*   **Flow**:
    ```
    shiori_search(query, kind="issue")
      ➔ shiori_issue_links(number)
      ➔ shiori_read_issue(number)
      ➔ Delegate to GitHub MCP to create issue if non-duplicate
    ```

> [!NOTE]
> **Label Indexing (Under Consideration)**
> To improve duplicate detection and triaging accuracy, indexing issue labels and types to allow search filters is proposed ([#165](https://github.com/masuda-masuo/shiori/issues/165)).

### UC-10: Detailed Specification Refinements
*   **Question**: "What prior decisions, implementations, and rejections are needed to convert this issue into code specs?"
*   **Flow**:
    ```
    shiori_read_issue(number)
      ➔ shiori_search(query)
      ➔ shiori_grep(pattern, path)
      ➔ shiori_read_file(path, range)
      ➔ Write specifications to issue via GitHub MCP
    ```

---

## 6. Antipatterns (When Not to Use Shiori)

*   **Creating Issues / Posting Comments**: Use the GitHub MCP.
*   **Checking Sync-Delayed Actions**: Run ingestion first, or query GitHub directly.
*   **Checking CI Status or Running Releases**: Use the GitHub MCP.
*   **Testing Code / Compiling Runs**: Use the Sunaba MCP.
*   **Sorting Lists by Date**: Use the GitHub MCP.

---

## 7. Decisions

*   Redefine the product description from "RAG" to "Project Knowledge Search MCP".
*   Define the value hierarchy as: (1) Unified cross-referencing, (2) Cross-lingual search, and (3) Context optimization.
*   Categorize tools into the 4-layer model (Retrieval, Inspection, Relationships & Changes, Operations).
*   Align `instructions` files and README with the 13 tools cataloged here.
