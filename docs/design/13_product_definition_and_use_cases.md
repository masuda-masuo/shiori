# Detailed Design: Product Definition & Use Cases

## 1. Core Definition

**Shiori is a local MCP server that enables AI agents to search across and navigate between project knowledge scattered throughout GitHub repositories (documents, source code, issues, and PR threads).**

"Project Knowledge" represents the collection of details required to answer questions about a project. It is structured into three categories:

| Category | Realization | Purpose |
|---|---|---|
| **Factual Spec (What/How)** | Documents and code on the main branch. | How does it behave? How is it used? |
| **History & Decisions (Why)** | Issue descriptions, PR reviews, comments. | Why is it configured this way? Known bugs. |
| **In-Flight Changes** | Open PR modifications, diff maps. | What is currently changing? |

### Value Hierarchy

Shiori's values are categorized by their indispensability:

1.  **Unified Cross-Referencing (Core Strength)**: Indexing all three knowledge types under a single unified store allows a single search to rank specifications, code definitions, and discussions together. Agents can traverse repositories by going from a hit issue to its resolving PR, from the PR to modified files, and on to specifications. Without Shiori, closed GitHub threads are practically unreachable by agents (since standard GitHub search is neither semantic nor cross-lingual).
2.  **Cross-Lingual Matching**: Allows queries in Japanese to discover documents or issues written in English (and vice versa), preventing indexing from splitting across languages.
3.  **Context Optimization (Pointer-then-Fetch)**: A core design principle that requires search APIs to return lightweight pointers rather than full texts. This protects the LLM's context window from being cluttered by irrelevant files.

---

## 2. Why Shiori is Not a General-Purpose RAG Server

Retrieval-Augmented Generation (RAG) is a pipeline where search results are stuffed into a context window to **generate** answers. Shiori focuses strictly on retrieval and navigation and **does not generate answers or summaries**.

*   **Agent Ownership**: Generating summaries and drawing conclusions remains the responsibility of the AI agent. Shiori returns raw pointers, snippets, and coordinates.
*   **Deferred Ingestion**: Shiori returns bookmarks, and the agent decides if it needs to fetch full texts via read tools.
*   **Navigation Services**: Shiori provides structured navigation tools (like `shiori_list_tree`, `shiori_issue_links`, `shiori_pr_changes`) that extend beyond standard keyword RAG queries.

---

## 3. Scope Boundaries

### In Scope (What Shiori Does)
*   **Unified Cross-Searching**: Index specs, code definitions, and discussion logs together.
*   **Discovering Intent**: Searching historical discussions to find why design decisions were made.
*   **Pointer Delivery**: Keeping context clean via the pointer-then-fetch strategy.
*   **Cross-Lingual Search**: Bypassing language barriers for Japanese/English repositories.
*   **Local Execution**: Running CPU embeddings locally to preserve data privacy.

### Out of Scope (Delegated Actions)
*   **Answer Synthesis**: Shiori does not write summaries or answer questions directly. (Delegated to: Agent/LLM).
*   **VCS Mutations**: Shiori is read-only. Creating issues, modifying files, or merging branches is out of scope. (Delegated to: GitHub MCP / Git).
*   **Code Validation**: Executing tests or compiling builds is out of scope. (Delegated to: Sunaba MCP).
*   **Chronological Triage**: Listing recently closed issues or active notifications is out of scope. (Delegated to: GitHub MCP).

---

## 4. Tool Taxonomy (4-Layer Model)

The 12 tools are structured based on user query intent:

### Layer 1: Retrieval (Where is it written?)
*   `shiori_search`: Hybrid semantic + keyword search. Primary entry point.
*   `shiori_keyword_search`: Exact identifier match helper.
*   `shiori_grep`: Stage-2 line-level search.

### Layer 2: Inspection (What is written?)
*   `shiori_read_file`: Reads cloned files from the main branch.
*   `shiori_read_issue`: Retrieves timeline timelines for issues or PRs.
*   `shiori_read_pr_file`: Transparently reads files at PR head commits.
*   `shiori_list_tree`: Lists indexed folder paths.

### Layer 3: Relationship & Changes (How is it linked? What changes?)
*   `shiori_issue_links`: Analyzes refs (closes, duplicate, refs, mentions).
*   `shiori_pr_changes`: Retrieves changed files in a PR.
*   `shiori_pr_diff`: Calculates unified git diffs for a PR.
*   `shiori_pr_review_comments`: Lists PR review comments.

### Layer 4: Operations (Is the index fresh?)
*   `shiori_status`: System sanity and warnings audit.

---

## 5. Developer Use Cases

### UC-1: Specs Onboarding
AI agent queries `shiori_search` to find setup or architectural documents. It reads specific sections via `shiori_read_file` to understand the codebase without checking out the entire tree.

### UC-2: Design Intent Audit ("Why")
Agent queries `shiori_search` to search historical discussions, reads the timeline via `shiori_read_issue`, and traces decisions to downstream pull requests via `shiori_issue_links`.

### UC-3: Code Discovery
Agent runs `shiori_keyword_search` to find class or function definitions, pinpointing lines via `shiori_grep` and fetching definitions via `shiori_read_file`.

### UC-4: Pull Request Reviews
Agent queries `shiori_pr_changes` to see overall changes, fetches file diffs via `shiori_pr_diff`, reads final file state via `shiori_read_pr_file`, and checks reviewer feedback via `shiori_pr_review_comments`.

### UC-5: Known Bug Checks
Agent checks error logs via `shiori_keyword_search` to discover duplicate issues, traces them to resolving PRs via `shiori_issue_links`, and reads the fixes via `shiori_pr_changes`.
