"""Unit tests for shiori_list_tree filter logic (issue #43).

Test scope:
- _match_extension: extension matching (case-insensitive, dot-optional)
- _walk_code_files: code file collection (exclusions, prefix, extension filter)
- list_tree: source_type validation, source_type + extension dispatch/aggregation,
  source field (two-store model origin identification)
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from shiori.mcp_server import (
    _VALID_SOURCE_TYPES,
    _match_extension,
    _walk_code_files,
    list_tree,
)


# ── ヘルパー: 仮想ディレクトリツリーを作成 ──


def _make_tree(base: str, files: list[str]) -> None:
    """Create directories and empty files from a list of file paths."""
    for f in files:
        full = os.path.join(base, f)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fp:
            fp.write(f"content:{f}\n")


# ===================================================================
# _match_extension のテスト（本番コードを import）
# ===================================================================


class TestMatchExtension:
    def test_exact_dot_extension(self):
        """.py specified matches .py files."""
        assert _match_extension("foo.py", ".py") is True

    def test_extension_without_dot(self):
        """py (no dot) also matches .py files."""
        assert _match_extension("foo.py", "py") is True

    def test_case_insensitive(self):
        """Case-insensitive match."""
        assert _match_extension("FOO.PY", ".py") is True
        assert _match_extension("foo.Py", ".py") is True
        assert _match_extension("foo.py", ".PY") is True

    def test_substring_no_match(self):
        """Substring match of extension does not match."""
        assert _match_extension("foo.pym", ".py") is False

    def test_different_extension_no_match(self):
        """Different extension does not match."""
        assert _match_extension("foo.js", ".py") is False

    def test_path_with_directories(self):
        """Extension matches even with directory path."""
        assert _match_extension("src/shiori/main.py", ".py") is True
        assert _match_extension("src/shiori/main.ts", ".py") is False

    def test_dotfiles_no_confusion(self):
        """Dotfiles like .gitignore are not confused with .git extension."""
        assert _match_extension(".gitignore", ".py") is False

    def test_minified_js(self):
        """.min.js matches .js (exclusion handled by _is_excluded_file)."""
        assert _match_extension("bundle.min.js", ".js") is True


# ===================================================================
# _walk_code_files のテスト（本番コードを import）
# ===================================================================


class TestWalkCodeFiles:
    def test_empty_dir(self):
        """Empty directory returns empty set."""
        with tempfile.TemporaryDirectory() as tmp:
            assert _walk_code_files(tmp, "") == set()

    def test_nonexistent_dir(self):
        """Non-existent directory returns empty set."""
        assert _walk_code_files("/nonexistent/path", "") == set()

    def test_basic_python_files(self):
        """Python files are collected."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "utils.py", "README.md"])
            result = _walk_code_files(tmp, "")
            assert "main.py" in result
            assert "utils.py" in result
            assert "README.md" not in result  # ドキュメントは除外

    def test_exclude_dirs(self):
        """Files in excluded directories are skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, [
                "src/main.py",
                "node_modules/foo/index.js",
                ".venv/lib/site.py",
            ])
            result = _walk_code_files(tmp, "")
            assert "src/main.py" in result
            assert "node_modules/foo/index.js" not in result
            assert ".venv/lib/site.py" not in result

    def test_exclude_extensions(self):
        """Files with excluded extensions are skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, [
                "main.py",
                "image.png",
                "archive.zip",
                "yarn.lock",  # .lock で除外
            ])
            result = _walk_code_files(tmp, "")
            assert "main.py" in result
            assert "image.png" not in result
            assert "archive.zip" not in result
            assert "yarn.lock" not in result

    def test_exclude_minified_js(self):
        """Minified JS (.min.js) is excluded."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["app.js", "app.min.js"])
            result = _walk_code_files(tmp, "")
            assert "app.js" in result
            assert "app.min.js" not in result

    def test_prefix_filter(self):
        """prefix filters paths."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, [
                "src/main.py",
                "src/utils.py",
                "src2/main.py",
                "docs/index.md",
                "README.md",
            ])
            result = _walk_code_files(tmp, "src")
            assert "src/main.py" in result
            assert "src/utils.py" in result
            assert "src2/main.py" not in result

    def test_prefix_exact_file(self):
        """When prefix matches a filename exactly, only that file is included."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "main.py.bak"])
            result = _walk_code_files(tmp, "main.py")
            assert "main.py" in result
            assert "main.py.bak" not in result

    def test_mixed_content(self):
        """Works correctly with a realistic project-like structure."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, [
                "src/shiori/main.py",
                "tests/test_main.py",
                "docs/guide.md",
                "pyproject.toml",
                "node_modules/pkg/index.js",
                "dist/bundle.min.js",
            ])
            result = _walk_code_files(tmp, "")
            assert "src/shiori/main.py" in result
            assert "tests/test_main.py" in result
            assert "pyproject.toml" in result
            assert "docs/guide.md" not in result  # ドキュメント
            assert "node_modules/pkg/index.js" not in result  # 除外ディレクトリ
            assert "dist/bundle.min.js" not in result  # 除外ディレクトリ+min.js

    def test_extension_filter_dot(self):
        """extension filter (with dot) applied during walk."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, [
                "src/main.py",
                "src/utils.py",
                "src/main.ts",
                "README.md",
            ])
            result = _walk_code_files(tmp, "", extension=".py")
            assert result == {"src/main.py", "src/utils.py"}

    def test_extension_filter_without_dot(self):
        """extension filter works without leading dot."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "utils.ts"])
            result = _walk_code_files(tmp, "", extension="py")
            assert result == {"main.py"}

    def test_extension_no_match(self):
        """Empty set when no matching extension."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "utils.py"])
            result = _walk_code_files(tmp, "", extension=".go")
            assert result == set()


# ===================================================================
# list_tree のテスト（本番コードを実呼び出し）
# ===================================================================


class TestListTreeSourceTypeValidation:
    """list_tree source_type validation.

    Validation occurs before DB connection, so only mocking settings is needed.
    """

    @staticmethod
    def _call_list_tree(source_type: str | None) -> None:
        """Call list_tree. Only validates source_type; errors in code file collection are OK."""
        with (
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server._conn"),
        ):
            mock_settings.repos = ["test/repo"]
            list_tree(source_type=source_type)

    def test_valid_doc(self):
        """'doc' is valid (validation passes before DB error)."""
        self._call_list_tree("doc")  # ValueError 以外は OK

    def test_valid_code(self):
        """'code' is valid."""
        self._call_list_tree("code")

    def test_none_is_valid(self):
        """None is valid (default)."""
        self._call_list_tree(None)

    def test_invalid_raises(self):
        """Invalid source_type raises ValueError."""
        with (
            patch("shiori.mcp_server.settings"),
            patch("shiori.mcp_server._conn"),
            pytest.raises(ValueError, match="無効な source_type"),
        ):
            list_tree(source_type="issue")

    def test_empty_string_raises(self):
        """Empty string is also invalid."""
        with (
            patch("shiori.mcp_server.settings"),
            patch("shiori.mcp_server._conn"),
            pytest.raises(ValueError, match="無効な source_type"),
        ):
            list_tree(source_type="")

    def test_random_string_raises(self):
        """Arbitrary string is invalid."""
        with (
            patch("shiori.mcp_server.settings"),
            patch("shiori.mcp_server._conn"),
            pytest.raises(ValueError, match="無効な source_type"),
        ):
            list_tree(source_type="xyz")


# ── helpers for end-to-end tests ──


def _entries_to_paths(entries: list[dict]) -> list[str]:
    """Extract path-only list from list_tree result (backward compat check)."""
    return [e["path"] for e in entries]


def _paths_with_source(
    entries: list[dict], source: str
) -> list[str]:
    """Extract paths for a specific source from list_tree result."""
    return [e["path"] for e in entries if e["source"] == source]


class TestListTreeEndToEnd:
    """list_tree end-to-end test.

    Mocks DB and filesystem; verifies source_type/extension dispatch,
    aggregation, sorting, and correct source field assignment.
    """

    def _mock_cursor(self, rows: list[tuple[str]]) -> MagicMock:
        """Create a mock cursor returning doc_files query results."""
        cur = MagicMock()
        cur.fetchall.return_value = rows
        return cur

    def _call_list_tree(
        self,
        tmp: str,
        source_type: str | None = None,
        extension: str | None = None,
        doc_rows: list[tuple[str]] | None = None,
        code_files: list[str] | None = None,
        mock_walk_return: set[str] | None = None,
    ) -> list[dict]:
        """Call list_tree in a mocked environment.

        When mock_walk_return is set, mocks _walk_code_files with that return value.
        Otherwise creates real files from code_files and calls real _walk_code_files.
        """
        # コードファイルを実際に作成
        if code_files:
            _make_tree(tmp, code_files)

        mock_conn = MagicMock()
        mock_cur = self._mock_cursor(doc_rows or [])
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        def _fake_conn():
            return mock_conn

        if mock_walk_return is not None:
            with (
                patch("shiori.mcp_server.settings") as mock_settings,
                patch("shiori.mcp_server._conn", side_effect=_fake_conn),
                patch(
                    "shiori.mcp_server._walk_code_files",
                    return_value=mock_walk_return,
                ),
            ):
                mock_settings.repos = ["test/repo"]
                mock_settings.repo_dir.return_value = tmp
                return list_tree(source_type=source_type, extension=extension)
        else:
            with (
                patch("shiori.mcp_server.settings") as mock_settings,
                patch("shiori.mcp_server._conn", side_effect=_fake_conn),
            ):
                mock_settings.repos = ["test/repo"]
                mock_settings.repo_dir.return_value = tmp
                return list_tree(source_type=source_type, extension=extension)

    def test_source_type_doc_only(self):
        """source_type='doc' returns only doc_files paths with source='doc'."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._call_list_tree(
                tmp,
                source_type="doc",
                doc_rows=[("README.md",), ("docs/guide.md",)],
                code_files=["main.py", "utils.py"],
            )
            assert _entries_to_paths(result) == ["README.md", "docs/guide.md"]
            for entry in result:
                assert entry["source"] == "doc"

    def test_source_type_code_only(self):
        """source_type='code' returns only code files with source='code'."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._call_list_tree(
                tmp,
                source_type="code",
                doc_rows=[("README.md",)],
                code_files=["main.py", "utils.py"],
            )
            assert _entries_to_paths(result) == ["main.py", "utils.py"]
            for entry in result:
                assert entry["source"] == "code"

    def test_source_type_none_returns_both(self):
        """source_type=None returns both doc + code with source assigned per entry."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._call_list_tree(
                tmp,
                source_type=None,
                doc_rows=[("README.md",)],
                code_files=["main.py"],
            )
            assert _entries_to_paths(result) == ["README.md", "main.py"]
            assert result[0] == {"path": "README.md", "source": "doc"}
            assert result[1] == {"path": "main.py", "source": "code"}

    def test_doc_with_extension_md(self):
        """source_type='doc' + extension='.md' returns only .md files."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._call_list_tree(
                tmp,
                source_type="doc",
                extension=".md",
                doc_rows=[("README.md",), ("CHANGELOG.rst",)],
            )
            assert _entries_to_paths(result) == ["README.md"]
            assert result[0]["source"] == "doc"

    def test_doc_with_extension_py_returns_empty(self):
        """source_type='doc' + extension='.py' returns empty (doc has no .py)."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._call_list_tree(
                tmp,
                source_type="doc",
                extension=".py",
                doc_rows=[("README.md",), ("docs/guide.md",)],
            )
            assert result == []

    def test_code_with_extension_py(self):
        """source_type='code' + extension='.py' returns only .py files."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._call_list_tree(
                tmp,
                source_type="code",
                extension=".py",
                code_files=["main.py", "utils.py", "utils.ts"],
            )
            assert _entries_to_paths(result) == ["main.py", "utils.py"]
            for entry in result:
                assert entry["source"] == "code"

    def test_code_with_extension_ts(self):
        """source_type='code' + extension='.ts' returns only .ts files."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._call_list_tree(
                tmp,
                source_type="code",
                extension=".ts",
                code_files=["main.py", "utils.ts", "types.ts"],
            )
            assert _entries_to_paths(result) == ["types.ts", "utils.ts"]
            for entry in result:
                assert entry["source"] == "code"

    def test_code_extension_no_match(self):
        """source_type='code' + extension='.go' (no match) returns empty."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._call_list_tree(
                tmp,
                source_type="code",
                extension=".go",
                code_files=["main.py", "utils.py"],
            )
            assert result == []

    def test_both_with_extension_py(self):
        """source_type=None + extension='.py' returns .py from both stores."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._call_list_tree(
                tmp,
                source_type=None,
                extension=".py",
                doc_rows=[("README.md",)],
                code_files=["main.py", "utils.ts"],
            )
            assert _entries_to_paths(result) == ["main.py"]
            assert result[0]["source"] == "code"

    def test_sorted_result(self):
        """Results are sorted by path."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._call_list_tree(
                tmp,
                source_type=None,
                doc_rows=[("a.md",), ("z.md",)],
                code_files=["c.py", "b.py"],
            )
            assert _entries_to_paths(result) == ["a.md", "b.py", "c.py", "z.md"]
            # source フィールドも確認
            assert result[0]["source"] == "doc"
            assert result[1]["source"] == "code"
            assert result[2]["source"] == "code"
            assert result[3]["source"] == "doc"

    def test_duplicate_path_across_stores(self):
        """When doc and code share the same path, doc takes precedence.

        In production this rarely happens because _walk_code_files excludes Markdown,
        but test verifies the defensive seen-set dedup + doc-priority logic.
        """
        with tempfile.TemporaryDirectory() as tmp:
            result = self._call_list_tree(
                tmp,
                source_type=None,
                doc_rows=[("src/foo.py",)],
                mock_walk_return={"src/foo.py"},
            )
            assert len(result) == 1
            assert result[0] == {"path": "src/foo.py", "source": "doc"}
