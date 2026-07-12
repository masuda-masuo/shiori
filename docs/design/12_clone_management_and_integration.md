# Detailed Design: Clone Management & Integration

## 1. Purpose

Define directory path structures and access rules for local repository clones managed by Shiori. This allows external tools (such as `sunaba`) to copy from existing clones, bypassing network cloning and speeding up sandboxed test executions.

---

## 2. Directory Path Convention

Repository checkouts are cloned into the following path on the host:

```
{SHIORI_DATA_DIR}/repos/{owner}__{name}
```

*   `{SHIORI_DATA_DIR}`: Root data directory path (defaults to `/data`).
*   `{owner}__{name}`: Repository owner and name joined by a double underscore (e.g. `masuda-masuo__sunaba`). This prevents directory nesting anomalies.
*   *Implementation*: Handled by `config.py::Settings.repo_dir()`.

### Host Path Mapping for External Tools
Under Docker Compose deployments, `/data` is mounted to the host's `./data` directory using a **bind mount**. Consequently, external tools on the host resolve repository clone paths at:

```
<compose_directory>/data/repos/{owner}__{name}
```

#### Why Bind Mounts?
We use bind mounts instead of named volumes to share code files. Docker's default named volume root folders (`/var/lib/docker/volumes/...`) restrict traverse access (`drwx--x---`), preventing non-root host processes from accessing files. Bind mounts bypass this, allowing sandboxes to copy code safely.

---

## 3. Clone Management Strategy

The `sync_docs` module manages git checkouts using the following flow:
*   **Initial Setup**: Runs `git clone --depth=1 <remote_url> <repo_dir>`.
*   **Sync Update**: Runs `git fetch --depth=1 origin` followed by `git reset --hard origin/HEAD` to pull clean main branches.
*   **Auth injection**: Injects credentials via `http.extraHeader` configuration arguments.

---

## 4. Constraints & Shallow Clones

*   **No local history**: Because checkouts are shallow clones (`--depth=1`), operations like viewing logs (`git log` beyond the head), calculating merge-bases, checking out non-remote branches, or pushing directly will fail.
    *   *Solution*: Copying tools (like `sunaba`) can lift this constraint after copy extraction by running `git fetch --unshallow` inside the sandbox.
*   **Freshness Verification**: Users should check the `last_synced_at` field in `shiori_status()` to determine checkout age, updating the index via `python -m shiori ingest` if stale.
*   **Access Validation**: Sync updates are restricted to repositories defined in `SHIORI_REPOS`. Attempting to ingest non-listed targets is rejected. External tools copying from these paths must restrict reads to paths matching the `{SHIORI_DATA_DIR}/repos/` prefix to prevent path traversal issues.
