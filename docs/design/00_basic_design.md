# Basic Design

## 1. Purpose and Scope

Shiori is a local **Project Knowledge Search MCP** server that enables AI agents to search across and navigate between project knowledge scattered throughout GitHub repositories (documents, source code, issues, and PR threads). Initially conceived as a broad RAG backend, its scope has been redefined to focus strictly on search retrieval and knowledge navigation (see [Product Definition & Use Cases](13_product_definition_and_use_cases.md) for details).

*   **Problem to Solve**: Knowledge answering a single question is often scattered across specs (docs and code) and context/intent (issue/PR discussions). AI agents have had no practical way to access intentions or decisions buried in closed GitHub threads (since GitHub's standard search is neither semantic nor cross-lingual). Fetching full-text files via general-purpose MCPs also wastes tokens and pollutes the context window.
*   **Core Policy**: Index all three knowledge types under a single unified store, returning pointers instead of full texts. This *pointer-then-fetch* flow narrows down the context loaded by the LLM to only relevant fragments.
*   **Target**: Both public and private repositories. Bilingual or mixed content (assuming individual files are written in a single language).

---

## 2. Design Principles

*   **Local-First & Private**: Run entirely locally to keep private repository code and data secure.
*   **Search First, Fetch Later (Pointer-then-Fetch)**: Return bookmarks rather than full files.
*   **Hybrid Search**: Integrate semantic vector similarity search with tokenized full-text keyword search.
*   **Unified Store**: Place vector embeddings, full-text indexes, and metadata in a single PostgreSQL database.
*   **Custom Retrieval, Standard Tools**: Reuse commodity toolchains (PostgreSQL, Docker) while custom-building the retrieval and search layer.

---

## 3. System Architecture

The pipeline flow is: **Sync/Ingest → Chunk → Embed → Index → Search → MCP Tools**.

To ensure both privacy and ultra-fast context retrieval, Shiori is structured as a **3-Tier Data Architecture**:

1.  **Tier 1: Database (PostgreSQL)**:
    *   **Store**: Handles semantic vectors (`pgvector`), full-text tokens (`pgroonga`), and metadata.
    *   **Role**: Resolves queries and returns "pointers" (coordinates like file path, line range, or issue ID) and small context snippets.
2.  **Tier 2: Local Clones (Local Disk)**:
    *   **Store**: Shallow checkouts of target repositories' `main` branches.
    *   **Role**: Serves full-text file inspections locally via `shiori_read_file`. AI agents fetch code fragments only after finding coordinates in Tier 1.
3.  **Tier 3: Egress & Live Integration (GitHub Live API / External MCPs)**:
    *   **Store**: Relies on external, remote sources.
    *   **Role**: Handles in-flight modifications (creating issues, posting comments, diffing pull request heads). Shiori remains a read-only local search engine, delegating write actions to standard GitHub MCPs.

*   **Deployments**: Two containers: Database (PostgreSQL) and MCP server (managed via Docker Compose).

---

## 4. Key Components

| Component | Responsibility | Reference | Source File |
| --- | --- | --- | --- |
| **Ingestion/Sync** | Pull and update documents, code, and issue threads | [Data Ingestion & Sync](01_data_ingestion_and_sync.md) | `src/shiori/github_sync.py` |
| **Chunking** | Slice sources into search units | [Chunking Strategy](02_chunking_strategy.md) | `src/shiori/chunking.py` |
| **Embeddings** | Generate vector representations | [Embeddings & Cross-Lingual Search](03_embeddings_and_cross_lingual_search.md) | `src/shiori/embedding.py` |
| **Datastore** | Manage vector, text, and metadata | [Datastore & Schema](04_datastore_and_schema.md) | `src/shiori/db.py` |
| **Search** | Execute hybrid queries and RRF ranking | [Search & Hybrid Search](05_search_and_hybrid_search.md) | `src/shiori/search.py` |
| **MCP Server** | Expose tools to AI clients | [MCP Server & Tool Design](06_mcp_server_and_tool_design.md) | `src/shiori/mcp_server.py` |
| **Runtime** | Define container configurations | [Runtime Environment & Deployment](07_runtime_environment_and_deployment.md) | `docker-compose.yml`, `docker/` |

---

## 5. Architectural Decisions (Decision Log)

*   **GitHub Authentication**: Decoupled using a `TokenProvider` interface. Priority sequence: **TokenSocket > TokenCommand > PAT > anonymous**. Under Docker Compose deployments, the token is pulled from a host-side mint socket (`GITHUB_TOKEN_SOCKET`); the GitHub App private key stays in the host keyring and never enters the container. (The in-container App-PEM mount and the token-file sharing mechanism were retired — #243 / #204.) (See [GitHub App Auth](09_github_app_auth.md) and [Token Supply Path](15_token_supply_path.md) for details).
*   **Unified Store (PostgreSQL)**: Consolidate embeddings (via `pgvector`) and full-text indexing (via `pgroonga`) under a single PostgreSQL instance to perform hybrid ranking (RRF) and metadata filtering (`WHERE` clause) in a single query.
*   **No Re-ranking**: We omit local re-ranking models in v1.0. Hybrid queries are ranked using Reciprocal Rank Fusion (RRF, k=60). `shiori_search` is the unified entrance, and `shiori_keyword_search` is kept separate for exact matching.
*   **One-Shot Ingestion**: Sync operations run as disposable ingestion jobs (`python -m shiori ingest --repo owner/repo`). Differential sync relies on content hashes for docs, and `updated_at` cursors for issues/PRs. Bot comments are filtered by default.
*   **Default Embedding Model**: Pinned to `intfloat/multilingual-e5-small` (384 dimensions) for fast CPU inference. Kept in a named volume (`HF_HOME=/models`).
*   **Search Ranking is Relevance-First**: Relevance-first ranking is enforced. Direct date-based sorting is not provided for specs (docs/code) since specs do not have a chronological correctness gradient. Date sort is deferred to the GitHub MCP.

---

## 6. Open Issues (Summary)

All initial v1.0 issues (full-text engines, summary generations, re-ranking, and sync pathways) have been resolved. Remaining investigations:
*   Integrating merge statuses and conclusion-oriented relevance boosting for PRs ([Sync](01_data_ingestion_and_sync.md), [Search](05_search_and_hybrid_search.md)).
*   Validating retrieval accuracy against real-world test datasets to evaluate model upgrades ([Embeddings](03_embeddings_and_cross_lingual_search.md)).
*   Local filesystem integration (detailed design in [Filesystem Ingestion](08_filesystem_ingestion.md), unimplemented in v1.0).
