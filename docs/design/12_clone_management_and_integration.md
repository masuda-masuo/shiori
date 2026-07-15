# Detailed Design: Clone Management & Integration

> **Issue #89**: Defines path structures and prerequisites for reusing Git clones managed by Shiori in external sandbox environments (such as `sunaba`).

---

## 1. Purpose

Shiori maintains Git clones of indexed repositories using shallow checkouts (`--depth=1`). By copying these existing clones directly into sandbox containers (`cp -r`), external tools can bypass remote cloning, improving execution performance.

---

## 2. Directory Path Convention

Cloned checkouts are structured on the host under the following directory:

```
{SHIORI_DATA_DIR}/repos/{owner}__{name}
```

*   `{SHIORI_DATA_DIR}`: Root data directory path (defaults to `/data`).
*   `{owner}__{name}`: Repository owner and name joined by a double underscore (e.g., `masuda-masuo__shiori`). 
*   *Implementation*: `config.py::Settings.repo_dir()` (`src/shiori/config.py:122`).

### Host Path Mapping for External Integration
In Docker Compose deployments, `/data` is mounted to the host's `./data` directory using a **bind mount** (`docker-compose.yml`). The repository root as seen by host-side tools is:

```
<compose_directory>/data/repos/{owner}__{name}
```

(e.g., `/opt/shiori/data/repos/masuda-masuo__sunaba`).

#### Why Bind Mounts?
We use bind mounts instead of named volumes to share code files. Docker's default named volume root folders (`/var/lib/docker/volumes/...`) restrict traverse access (`drwx--x---`), preventing non-root host processes from accessing files. Bind mounts bypass this, allowing sandboxes to copy code safely.

Because containers run as root, files inside the container are owned by root. The host-side reader relies on files being created with `umask 022` (making them world-readable). Tightening permission constraints under `/data` will break external tool integrations.

Double underscores (`__`) are used instead of `/` to avoid introducing deep folder structures that do not match repository layout roots.

### Examples

| `SHIORI_DATA_DIR` | repo | Target Clone Path |
|---|---|---|
| `/data` | `masuda-masuo/shiori` | `/data/repos/masuda-masuo__shiori` |
| `/data` | `masuda-masuo/sunaba` | `/data/repos/masuda-masuo__sunaba` |
| `/var/shiori` | `org/foo` | `/var/shiori/repos/org__foo` |

---

## 3. Clone Management Strategy

Clone refreshes are managed by `src/shiori/refresh.py` (`refresh_clone()`, extracted from `sync_docs` in #236):

*   **Initial Setup**: Runs `git clone --depth=1 <remote_url> <repo_dir>`.
*   **Sync Update**: Runs `git fetch --depth=1 origin` followed by `git reset --hard origin/HEAD` to pull clean main branches.
*   **Authentication**: Injects credentials via token-embedded URL (`_authed_url()`).

Clone refresh is the **cheap path (Phase 1)** of the pull-type sync model (#236). It runs inline (blocking) when tools that read clone files directly (`shiori_read_file`, `shiori_grep`) are called, and as a precondition for search tools (`shiori_search`, `shiori_keyword_search`). The expensive re-indexing (Phase 2) runs in the background and does not block clone freshness.

---

## 4. Constraints & Shallow Clones

### depth=1 shallow clone
Because checkouts are shallow clones (`--depth=1`), operations like viewing logs (`git log` beyond the head), calculating merge-bases, checking out non-remote branches, or pushing directly will fail.
*   *Solution*: Copying tools (like `sunaba`) can lift this constraint after copy extraction by running `git fetch --unshallow` inside the sandbox.

### Freshness Verification
Shiori uses pull-type on-demand sync (#236). Clone freshness is verified via:

*   **`clone_head`** in `shiori_status()`: The on-disk HEAD after the most recent Phase 1 (clone refresh).
*   **`indexed_head`** in `shiori_status()`: The HEAD reflected in the index after the most recent Phase 2 (re-indexing).
*   **`index_stale`** in `shiori_status()`: True when `clone_head != indexed_head`, meaning the on-disk clone is ahead of the indexed content.

The `SHIORI_SYNC_INTERVAL_SECONDS` setting is now a **debounce interval** (max N seconds between pulls for the same repo), not a push interval. There is no background auto-sync loop.

External tools using these folders must assume that the clone state might be out of date; calling `shiori_read_file` or `shiori_search` will trigger a Phase 1 refresh inline if the debounce interval has elapsed.

### Allowlist Validation
Sync updates are restricted to repositories defined in `SHIORI_REPOS`. Attempting to ingest non-listed targets is rejected. External tools copying from these paths must restrict reads to paths matching the `{SHIORI_DATA_DIR}/repos/` prefix to prevent path traversal issues.

---

## 5. Downstream Integration

### Sunaba
If `clone_repo="masuda-masuo/shiori"` is passed to `sandbox_initialize` or `run_container_and_exec`, the sandbox copier pulls code from the host's `${SHIORI_DATA_DIR}/repos/{owner}__{name}` into the container's workspace via `cp -r`.

Sunaba maps the host path using the `--shiori-repos-path` option or `SUNABA_SHIORI_REPOS_PATH` environment variable. If pre-clones are missing, Sunaba falls back to executing a full network clone (sunaba#178 / #532).

### Custom Integrations
Any external tool can follow the path structures defined here to reuse Shiori's local clones.
