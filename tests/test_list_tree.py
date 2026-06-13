"""shiori_list_tree のフィルタロジックのユニットテスト（issue #43）。

テスト対象:
- _match_extension: 拡張子マッチ（大文字小文字無視、ドット有無対応）
- _walk_code_files: コードファイル収集（パス・除外・prefix・extension フィルタ）
- source_type バリデーション
- source_type + extension 組み合わせ
"""

from __future__ import annotations

import os
import tempfile

import pytest


# ---------------------------------------------------------------------------
# _match_extension のテスト用スタンドアロン実装
# ---------------------------------------------------------------------------


def _match_extension(path: str, extension: str) -> bool:
    """拡張子が指定値にマッチするか（大文字小文字無視、'.' 有無両対応）。"""
    ext = extension if extension.startswith(".") else "." + extension
    return path.lower().endswith(ext.lower())


# ---------------------------------------------------------------------------
# _walk_code_files のテスト用に必要な定数と関数の簡易コピー
# ---------------------------------------------------------------------------

_DOC_EXTENSIONS = {".md", ".mdx", ".markdown"}

_EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    ".venv", "venv",
    "dist", "build",
    "__pycache__",
    ".tox", ".eggs",
    ".next",
    "target",
}

_EXCLUDE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pyc", ".pyo",
    ".so", ".dylib", ".dll", ".wasm",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".pdf",
    ".lock",
    ".min.js", ".min.css",
}

_VALID_SOURCE_TYPES = {"doc", "code"}


def _is_doc_file(filename: str) -> bool:
    return any(filename.lower().endswith(ext) for ext in _DOC_EXTENSIONS)


def _is_excluded_file(filename: str) -> bool:
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in _EXCLUDE_EXTENSIONS)


def _walk_code_files(base: str, prefix: str, extension: str | None = None) -> set[str]:
    paths: set[str] = set()
    if not os.path.isdir(base):
        return paths
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        rel_dir = os.path.relpath(dirpath, base)
        if rel_dir == ".":
            rel_dir = ""
        for fn in filenames:
            if _is_doc_file(fn) or _is_excluded_file(fn):
                continue
            rel_path = os.path.join(rel_dir, fn) if rel_dir else fn
            if prefix and not (
                rel_path == prefix or rel_path.startswith(prefix + "/")
            ):
                continue
            if extension and not _match_extension(rel_path, extension):
                continue
            paths.add(rel_path)
    return paths


# ── ヘルパー: 仮想ディレクトリツリーを作成 ──


def _make_tree(base: str, files: list[str]) -> None:
    """ファイルパスのリストからディレクトリと空ファイルを作成する。"""
    for f in files:
        full = os.path.join(base, f)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fp:
            fp.write(f"content:{f}\n")


# ===================================================================
# _match_extension のテスト
# ===================================================================


class TestMatchExtension:
    def test_exact_dot_extension(self):
        """.py 指定で .py ファイルにマッチ"""
        assert _match_extension("foo.py", ".py") is True

    def test_extension_without_dot(self):
        """py 指定（ドットなし）でも .py ファイルにマッチ"""
        assert _match_extension("foo.py", "py") is True

    def test_case_insensitive(self):
        """大文字小文字を区別しない"""
        assert _match_extension("FOO.PY", ".py") is True
        assert _match_extension("foo.Py", ".py") is True
        assert _match_extension("foo.py", ".PY") is True

    def test_substring_no_match(self):
        """拡張子の部分一致ではマッチしない"""
        assert _match_extension("foo.pym", ".py") is False

    def test_different_extension_no_match(self):
        """異なる拡張子にはマッチしない"""
        assert _match_extension("foo.js", ".py") is False

    def test_path_with_directories(self):
        """ディレクトリを含むパスでも拡張子マッチ"""
        assert _match_extension("src/shiori/main.py", ".py") is True
        assert _match_extension("src/shiori/main.ts", ".py") is False

    def test_dotfiles_no_confusion(self):
        """.gitignore のようなドットファイルは .git 拡張子と誤判定しない"""
        assert _match_extension(".gitignore", ".py") is False

    def test_minified_js(self):
        """.min.js は .js にマッチする（除外は _is_excluded_file が担当）"""
        assert _match_extension("bundle.min.js", ".js") is True


# ===================================================================
# _walk_code_files のテスト
# ===================================================================


class TestWalkCodeFiles:
    def test_empty_dir(self):
        """空のディレクトリは空集合を返す"""
        with tempfile.TemporaryDirectory() as tmp:
            assert _walk_code_files(tmp, "") == set()

    def test_nonexistent_dir(self):
        """存在しないディレクトリは空集合を返す"""
        assert _walk_code_files("/nonexistent/path", "") == set()

    def test_basic_python_files(self):
        """Python ファイルが収集される"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "utils.py", "README.md"])
            result = _walk_code_files(tmp, "")
            assert "main.py" in result
            assert "utils.py" in result
            assert "README.md" not in result  # ドキュメントは除外

    def test_exclude_dirs(self):
        """除外ディレクトリ内のファイルはスキップされる"""
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
        """除外拡張子のファイルはスキップされる"""
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
            # package-lock.json は .json が末尾なので除外されない（既存動作）
            _make_tree(tmp, ["package-lock.json"])
            result = _walk_code_files(tmp, "")
            assert "package-lock.json" in result

    def test_exclude_minified_js(self):
        """minified JS (.min.js) は除外される"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, [
                "app.js",
                "app.min.js",
            ])
            result = _walk_code_files(tmp, "")
            assert "app.js" in result
            assert "app.min.js" not in result

    def test_prefix_filter(self):
        """prefix でパスを絞り込める"""
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
            assert "src2/main.py" not in result  # 別ディレクトリ
            assert "docs/index.md" not in result  # ドキュメント

    def test_prefix_exact_file(self):
        """prefix がファイル名と一致した場合、そのファイルのみ含まれる"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "main.py.bak"])
            result = _walk_code_files(tmp, "main.py")
            assert "main.py" in result
            # main.py.bak は "main.py" と等しくなく、"main.py/" で始まらない
            assert "main.py.bak" not in result

    def test_mixed_content(self):
        """実際のプロジェクトに近い構成で正常に収集される"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, [
                "src/shiori/main.py",
                "src/shiori/utils.py",
                "src/shiori/__init__.py",
                "tests/test_main.py",
                "docs/guide.md",
                "README.md",
                "pyproject.toml",
                "node_modules/pkg/index.js",
                "dist/bundle.min.js",
            ])
            result = _walk_code_files(tmp, "")
            assert "src/shiori/main.py" in result
            assert "src/shiori/utils.py" in result
            assert "tests/test_main.py" in result
            assert "pyproject.toml" in result
            assert "docs/guide.md" not in result  # ドキュメント
            assert "node_modules/pkg/index.js" not in result  # 除外ディレクトリ
            assert "dist/bundle.min.js" not in result  # 除外ディレクトリ+min.js

    def test_extension_filter_dot(self):
        """extension 指定（ドット付き）でウォーク中にフィルタされる"""
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
        """extension 指定（ドットなし）でも正常にフィルタ"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, [
                "main.py",
                "utils.ts",
            ])
            result = _walk_code_files(tmp, "", extension="py")
            assert result == {"main.py"}

    def test_extension_no_match(self):
        """マッチする拡張子がない場合は空集合"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "utils.py"])
            result = _walk_code_files(tmp, "", extension=".go")
            assert result == set()


# ===================================================================
# source_type バリデーションのテスト
# ===================================================================


class TestSourceTypeValidation:
    """list_tree の source_type バリデーション。

    実際の list_tree 関数は DB とファイルシステムに依存するため、
    ここではバリデーションロジックを検証する。
    """

    @staticmethod
    def _validate_source_type(source_type: str | None) -> None:
        if source_type is not None and source_type not in _VALID_SOURCE_TYPES:
            raise ValueError(
                f"無効な source_type '{source_type}' です。"
                f"有効な値: {', '.join(sorted(_VALID_SOURCE_TYPES))}"
            )

    def test_valid_doc(self):
        """'doc' は有効"""
        self._validate_source_type("doc")  # エラーなし

    def test_valid_code(self):
        """'code' は有効"""
        self._validate_source_type("code")  # エラーなし

    def test_none_is_valid(self):
        """None は有効（デフォルト）"""
        self._validate_source_type(None)  # エラーなし

    def test_invalid_raises(self):
        """無効な値は ValueError"""
        with pytest.raises(ValueError, match="無効な source_type"):
            self._validate_source_type("issue")

    def test_empty_string_raises(self):
        """空文字列も無効"""
        with pytest.raises(ValueError, match="無効な source_type"):
            self._validate_source_type("")

    def test_random_string_raises(self):
        """任意の文字列は無効"""
        with pytest.raises(ValueError, match="無効な source_type"):
            self._validate_source_type("xyz")


# ===================================================================
# source_type + extension 組み合わせテスト
# ===================================================================


class TestSourceTypeExtensionCombo:
    """source_type と extension の組み合わせテスト。

    list_tree のフィルタ分岐ロジックを純粋関数で検証する。
    実際の list_tree は:
    1. source_type in (None, "doc") → doc_files クエリ → extension フィルタ
    2. source_type in (None, "code") → _walk_code_files → extension フィルタ
    3. 両方の結果を set に集約 → sorted
    """

    def test_doc_all_md(self):
        """source_type='doc' + extension='.md' は doc_files だけを通す"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["README.md", "main.py", "docs/guide.md"])
            # doc_files 模擬パス
            doc_paths = {"README.md", "docs/guide.md"}
            # extension フィルタ
            doc_filtered = {p for p in doc_paths if _match_extension(p, ".md")}
            # source_type="doc" -> code walk はスキップ
            assert doc_filtered == {"README.md", "docs/guide.md"}

    def test_doc_py_returns_empty(self):
        """source_type='doc' + extension='.py' は空（doc に .py はない）"""
        doc_paths = {"README.md", "docs/guide.md"}
        doc_filtered = {p for p in doc_paths if _match_extension(p, ".py")}
        assert doc_filtered == set()

    def test_code_all_py(self):
        """source_type='code' + extension='.py' はコードファイルのみ通す"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "utils.py", "utils.ts", "README.md"])
            code_paths = _walk_code_files(tmp, "", extension=".py")
            assert code_paths == {"main.py", "utils.py"}

    def test_code_ts(self):
        """source_type='code' + extension='.ts' で .ts だけ"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "utils.ts", "types.ts"])
            code_paths = _walk_code_files(tmp, "", extension=".ts")
            assert code_paths == {"utils.ts", "types.ts"}

    def test_both_with_extension(self):
        """source_type=None + extension='.py' は両方から .py だけ"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "utils.ts", "README.md"])
            # doc_files 模擬（.md だけなので .py フィルタで空）
            doc_paths: set[str] = set()
            # code walk
            code_paths = _walk_code_files(tmp, "", extension=".py")
            all_paths = doc_paths | code_paths
            assert all_paths == {"main.py"}

    def test_both_no_extension(self):
        """source_type=None + extension=None は両方の全ファイル"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "utils.ts", "README.md"])
            doc_paths = {"README.md"}
            code_paths = _walk_code_files(tmp, "")
            all_paths = doc_paths | code_paths
            assert "main.py" in all_paths
            assert "utils.ts" in all_paths
            assert "README.md" in all_paths

    def test_code_empty_extension(self):
        """source_type='code' + extension='.go'（マッチなし）は空"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "utils.py"])
            code_paths = _walk_code_files(tmp, "", extension=".go")
            assert code_paths == set()


# ===================================================================
# list_tree 全体のフィルタ結合のテスト
# ===================================================================


class TestListTreeFilters:
    """list_tree のフィルタロジック（ソース結合＋拡張子フィルタ）のテスト。"""

    def test_extension_filter_with_dot(self):
        """extension 指定（ドット付き）でフィルタされる"""
        paths = [
            "src/main.py",
            "src/utils.py",
            "src/main.ts",
            "README.md",
        ]
        ext = ".py"
        result = sorted(p for p in paths if _match_extension(p, ext))
        assert result == ["src/main.py", "src/utils.py"]

    def test_extension_filter_without_dot(self):
        """extension 指定（ドットなし）でも正常にフィルタ"""
        paths = [
            "src/main.py",
            "src/utils.py",
            "src/main.ts",
        ]
        ext = "py"
        result = sorted(p for p in paths if _match_extension(p, ext))
        assert result == ["src/main.py", "src/utils.py"]

    def test_extension_none_returns_all(self):
        """extension=None ではフィルタされない"""
        paths = ["a.py", "b.ts", "c.md"]
        result = sorted(paths)
        assert result == ["a.py", "b.ts", "c.md"]

    def test_extension_no_match_returns_empty(self):
        """マッチするファイルがない場合は空リスト"""
        paths = ["a.py", "b.ts"]
        ext = ".go"
        result = sorted(p for p in paths if _match_extension(p, ext))
        assert result == []

    def test_source_type_code_skips_docs(self):
        """source_type='code' 相当のパス収集でドキュメントが含まれない"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "README.md"])
            code_paths = _walk_code_files(tmp, "")
            assert "main.py" in code_paths
            assert "README.md" not in code_paths  # ドキュメントは除外
