# Detailed Design: Filesystem Ingestion (fs Source)

> **Status: Unimplemented (Future Plan)**. This document outlines the design for local directory indexing. It is out of scope for v1.0. The referenced modules `doc_indexer.py`, `fs_sync.py`, configuration keys `fs_*`, and command line flags `--source` are currently unimplemented.

---

## 1. Purpose

Support indexing local directories (e.g. documentation folders, Obsidian vaults, or code generators) outside of GitHub, passing them through the same search pipeline (chunking, embedding, indexing, and hybrid retrieval).

The design introduces a third ingestion path (`fs`) into the ingestion layer while leaving the chunking (`02`), embedding (`03`), database schema (`04`), and hybrid search (`05`) modules unmodified.

---

## 2. Ingestion Rules

1.  **Repository Column mapping**: Local directories are stored in the database's `repo` column with the prefix `fs:{name}` (e.g. `fs:notes`). Since GitHub repos contain a `/` separator and local folder names do not, naming conflicts are avoided.
2.  **Source Type Mapping**: Local files are tagged with `source_type="doc"` so that existing search queries and filter blocks function without modification.
3.  **Differential Sync**: Uses the existing hash tracker `doc_files` to store SHA-256 content hashes, mapping the path to `fs:{name}`.
4.  **URLs**: Relative paths are resolved into `file://` URLs. Path mismatches between the container and host are resolved via a `url_base` configuration map.
5.  **Cursors**: We bypass `sync_state` cursors since local directories do not have an equivalent to a git commit head.
6.  **Refactoring**: Directory traversal and indexing logic will be refactored into a reusable `doc_indexer.py` shared by both git clone scans and local directory scans.

---

## 3. Configuration Variables

| Key | Required | Example | Description |
| --- | --- | --- | --- |
| `SHIORI_FS_SOURCES` | Yes (for fs) | `notes=/data/notes;specs=/data/specs` | Semicolon-separated list of `name=path` pairs. |
| `SHIORI_FS_INCLUDE_EXTS` | No | `.md,.mdx,.markdown` | Comma-separated list of target extensions. Defaults to `.md,.mdx,.markdown`. |
| `SHIORI_FS_EXCLUDE_DIRS` | No | `.git,node_modules,.obsidian` | Directory names to exclude from scans. |
| `SHIORI_FS_URL_BASE` | No | `notes=file:///Users/me/notes` | Maps source names to host-side URL bases. |

---

## 4. Implementation Steps

### 1. Unified Indexer (`src/shiori/doc_indexer.py`)
Extract the folder scan and index loop from `github_sync.sync_docs` into a general-purpose function:

```python
from collections.abc import Callable

def index_doc_tree(
    settings: Settings,
    conn: psycopg.Connection,
    embedder: Embedder,
    *,
    repo_key: str,                      # e.g., "owner/name" or "fs:notes"
    root_dir: str,                      # Target root path
    url_for: Callable[[str, str], str], # URL builder: (rel_path, anchor) -> url
    include_exts: tuple[str, ...],
    exclude_dirs: set[str],
) -> int:
    """Recursively scans root_dir and indexes modified files. Returns updated file count."""
```

### 2. Filesystem Sync (`src/shiori/fs_sync.py`)
Write a sync wrapper for local directories:

```python
def sync_fs(
    settings: Settings, conn: psycopg.Connection, embedder: Embedder,
    name: str, root: str,
) -> int:
    if not os.path.isdir(root):
        raise RuntimeError(f"Directory not found: {root}")
    base = settings.fs_url_bases.get(name, f"file://{root.rstrip('/')}")
    
    def url_for(rel: str, anchor: str) -> str:
        return f"{base}/{rel}{anchor}"
        
    return index_doc_tree(
        settings, conn, embedder,
        repo_key=f"fs:{name}", root_dir=root, url_for=url_for,
        include_exts=settings.fs_include_exts,
        exclude_dirs=settings.fs_exclude_dirs,
    )
```

### 3. Refactoring `github_sync.sync_docs`
Retain Git repository operations (cloning, fetching, resolving revisions) and call `index_doc_tree` to perform the actual scans:

```python
def url_for(rel: str, anchor: str) -> str:
    return f"https://github.com/{repo}/blob/{default_branch}/{rel}{anchor}"

n = index_doc_tree(
    settings, conn, embedder,
    repo_key=repo, root_dir=repo_dir, url_for=url_for,
    include_exts=(".md", ".mdx", ".markdown"),
    exclude_dirs={".git"},
)
```

### 4. CLI & Docker Integration
*   The ingest command supports a `--source <key>` argument (e.g. `python -m shiori ingest --source fs:notes`).
*   Path traversals in `read_file` or `list_tree` are blocked by checking that resolved absolute paths begin with the registered source root path:
    ```python
    if not os.path.realpath(joined).startswith(os.path.realpath(root) + os.sep):
        raise ValueError("path escapes source root")
    ```
*   Mount target folders as read-only volumes in Docker Compose configurations.
