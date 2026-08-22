"""Regression tests for doc sync deleting code rows from doc_files (issue #442)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from shiori.sync_docs import index_docs, sync_docs


class _FakeEmbedder:
    def embed_passages(self, texts):
        return [[0.0] * 8 for _ in texts]


class _MockDBConn:
    def __init__(self):
        # (repo, path) -> dict
        self.doc_files = {}
        self.chunks = {}
        self.executed = []

    def cursor(self):
        return _MockDBCursor(self)

    def commit(self):
        pass


class _MockDBCursor:
    def __init__(self, db: _MockDBConn):
        self.db = db
        self._fetched = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=()):
        self.db.executed.append((sql, params))
        sql_compact = " ".join(sql.split())

        if "SELECT path, content_sha FROM doc_files" in sql_compact:
            repo = params[0]
            if "kind = 'doc'" in sql_compact:
                rows = [
                    (row["path"], row["content_sha"])
                    for row in self.db.doc_files.values()
                    if row["repo"] == repo and row["kind"] == "doc"
                ]
            else:
                rows = [
                    (row["path"], row["content_sha"])
                    for row in self.db.doc_files.values()
                    if row["repo"] == repo
                ]
            self._fetched = rows

        elif "DELETE FROM doc_files" in sql_compact:
            repo = params[0]
            path = params[1]
            if "kind = 'doc'" in sql_compact:
                key = (repo, path)
                if key in self.db.doc_files and self.db.doc_files[key]["kind"] == "doc":
                    del self.db.doc_files[key]
            else:
                key = (repo, path)
                if key in self.db.doc_files:
                    del self.db.doc_files[key]

        elif "DELETE FROM chunks" in sql_compact:
            chunk_key = params[0]
            self.db.chunks.pop(chunk_key, None)

        elif "INSERT INTO doc_files" in sql_compact:
            repo, path, content_sha, language = params[:4]
            self.db.doc_files[(repo, path)] = {
                "repo": repo,
                "path": path,
                "content_sha": content_sha,
                "language": language,
                "kind": "doc",
            }

        elif "INSERT INTO sync_state" in sql_compact or "UPDATE sync_state" in sql_compact:
            pass

    def fetchall(self):
        return self._fetched

    def fetchone(self):
        return self._fetched[0] if self._fetched else None


class _FakeSettings:
    def __init__(self, repo_dir: str):
        self._repo_dir = repo_dir
        self.chunk_max_chars = 1200

    def repo_dir(self, repo: str) -> str:
        return self._repo_dir


class TestDocCodeSyncIsolation:
    """Tests ensuring doc sync operates only on kind='doc' rows in doc_files (issue #442)."""

    def _setup_repo(self, tmp_path: Path):
        repo_dir = tmp_path / "o__r"
        repo_dir.mkdir(exist_ok=True)
        doc_content = "# README\n\nDoc content.\n"
        readme = repo_dir / "README.md"
        readme.write_text(doc_content, encoding="utf-8")
        doc_sha = hashlib.sha256(doc_content.encode("utf-8")).hexdigest()
        return repo_dir, doc_sha

    def test_index_docs_preserves_code_rows(self, tmp_path):
        repo_dir, doc_sha = self._setup_repo(tmp_path)
        settings = _FakeSettings(str(repo_dir))
        conn = _MockDBConn()
        embedder = _FakeEmbedder()

        repo = "o/r"
        conn.doc_files[(repo, "README.md")] = {
            "repo": repo,
            "path": "README.md",
            "content_sha": doc_sha,
            "language": "en",
            "kind": "doc",
        }
        conn.doc_files[(repo, "src/main.py")] = {
            "repo": repo,
            "path": "src/main.py",
            "content_sha": "code_sha_1",
            "language": None,
            "kind": "code",
        }
        conn.doc_files[(repo, "src/utils.py")] = {
            "repo": repo,
            "path": "src/utils.py",
            "content_sha": "code_sha_2",
            "language": None,
            "kind": "code",
        }

        with patch("shiori.sync_docs._git", return_value="main"):
            index_docs(cast(Any, settings), cast(Any, conn), cast(Any, embedder), repo)

        assert (repo, "src/main.py") in conn.doc_files
        assert conn.doc_files[(repo, "src/main.py")]["kind"] == "code"
        assert conn.doc_files[(repo, "src/main.py")]["content_sha"] == "code_sha_1"

        assert (repo, "src/utils.py") in conn.doc_files
        assert conn.doc_files[(repo, "src/utils.py")]["kind"] == "code"
        assert conn.doc_files[(repo, "src/utils.py")]["content_sha"] == "code_sha_2"

        code_rows = [r for r in conn.doc_files.values() if r["kind"] == "code"]
        assert len(code_rows) == 2

    def test_sync_docs_preserves_code_rows(self, tmp_path):
        repo_dir, doc_sha = self._setup_repo(tmp_path)
        settings = _FakeSettings(str(repo_dir))
        conn = _MockDBConn()
        embedder = _FakeEmbedder()
        provider = MagicMock()

        repo = "o/r"
        conn.doc_files[(repo, "README.md")] = {
            "repo": repo,
            "path": "README.md",
            "content_sha": doc_sha,
            "language": "en",
            "kind": "doc",
        }
        conn.doc_files[(repo, "src/main.py")] = {
            "repo": repo,
            "path": "src/main.py",
            "content_sha": "code_sha_1",
            "language": None,
            "kind": "code",
        }

        with (
            patch("shiori.refresh.refresh_clone", return_value="head123"),
            patch("shiori.sync_docs._git", return_value="main"),
        ):
            sync_docs(cast(Any, settings), cast(Any, conn), cast(Any, embedder), repo, provider)

        assert (repo, "src/main.py") in conn.doc_files
        assert conn.doc_files[(repo, "src/main.py")]["kind"] == "code"
        assert conn.doc_files[(repo, "src/main.py")]["content_sha"] == "code_sha_1"

    def test_doc_sync_removes_deleted_doc_file(self, tmp_path):
        repo_dir, doc_sha = self._setup_repo(tmp_path)
        settings = _FakeSettings(str(repo_dir))
        conn = _MockDBConn()
        embedder = _FakeEmbedder()

        repo = "o/r"
        conn.doc_files[(repo, "README.md")] = {
            "repo": repo,
            "path": "README.md",
            "content_sha": doc_sha,
            "language": "en",
            "kind": "doc",
        }
        conn.doc_files[(repo, "OLD.md")] = {
            "repo": repo,
            "path": "OLD.md",
            "content_sha": "old_sha",
            "language": "en",
            "kind": "doc",
        }
        conn.chunks["doc:o/r:OLD.md"] = [{"content": "old"}]
        conn.doc_files[(repo, "src/main.py")] = {
            "repo": repo,
            "path": "src/main.py",
            "content_sha": "code_sha_1",
            "language": None,
            "kind": "code",
        }

        with patch("shiori.sync_docs._git", return_value="main"):
            index_docs(cast(Any, settings), cast(Any, conn), cast(Any, embedder), repo)

        assert (repo, "OLD.md") not in conn.doc_files
        assert "doc:o/r:OLD.md" not in conn.chunks
        assert (repo, "README.md") in conn.doc_files
        assert (repo, "src/main.py") in conn.doc_files
