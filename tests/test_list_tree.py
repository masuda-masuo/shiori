"""shiori_list_tree のフィルタロジックのユニットテスト（issue #43）。

テスト対象:
- _match_extension: 拡張子マッチ（大文字小文字無視、ドット有無対応）
- _walk_code_files: コードファイル収集（パス・除外・prefix・extension フィルタ）
- list_tree: source_type バリデーション、source_type + extension の分岐・集約、
  source フィールド（二ストア・モデルの出所識別）
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from shiori.mcp_server import (
    list_tree,
)
from shiori.walk_utils import (
    _match_extension,
    _walk_code_files,
)

# ── ヘルパー: 仮想ディレクトリツリーを作成 ──


def _make_tree(base: str, files: list[str]) -> None:
    """ファイルパスのリストからディレクトリと空ファイルを作成する。"""
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
# _walk_code_files のテスト（本番コードを import）
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
            assert "README.md" not in result  # docs excluded

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

    def test_exclude_dashboard_dist_suffix(self):
        """完全一致しないビルド成果物ディレクトリ(dashboard_dist等)もスキップされる(issue #235)"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, [
                "src/main.py",
                "dashboard_dist/assets/index-abc123.js",
                "web-dist/bundle.js",
            ])
            result = _walk_code_files(tmp, "")
            assert "src/main.py" in result
            assert "dashboard_dist/assets/index-abc123.js" not in result
            assert "web-dist/bundle.js" not in result

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

    def test_exclude_minified_js(self):
        """minified JS (.min.js) は除外される"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["app.js", "app.min.js"])
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
            assert "src2/main.py" not in result

    def test_prefix_exact_file(self):
        """prefix がファイル名と一致した場合、そのファイルのみ含まれる"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "main.py.bak"])
            result = _walk_code_files(tmp, "main.py")
            assert "main.py" in result
            assert "main.py.bak" not in result

    def test_mixed_content(self):
        """実際のプロジェクトに近い構成で正常に収集される"""
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
            assert "node_modules/pkg/index.js" not in result  # excluded dir
            assert "dist/bundle.min.js" not in result  # excluded dir+min.js

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
            _make_tree(tmp, ["main.py", "utils.ts"])
            result = _walk_code_files(tmp, "", extension="py")
            assert result == {"main.py"}

    def test_extension_no_match(self):
        """マッチする拡張子がない場合は空集合"""
        with tempfile.TemporaryDirectory() as tmp:
            _make_tree(tmp, ["main.py", "utils.py"])
            result = _walk_code_files(tmp, "", extension=".go")
            assert result == set()


# ===================================================================
# list_tree のテスト（本番コードを実呼び出し）
# ===================================================================


class TestListTreeSourceTypeValidation:
    """list_tree の source_type バリデーション。

    バリデーションは DB 接続より前に行われるため、
    settings だけモックすれば実呼び出し可能。
    """

    @staticmethod
    def _call_list_tree(source_type: str | None) -> None:
        """list_tree を呼び出す。バリデーション通過を確認するだけなので
        コードファイル収集部分でエラーになってもよい。
        """
        with (
            patch("shiori.tools.list_tree.settings") as mock_settings,
            patch("shiori.tools.common.settings", mock_settings),
            patch("shiori.tools.list_tree._conn"),
        ):
            mock_settings.repos = ["test/repo"]
            list_tree(source_type=source_type)

    def test_valid_doc(self):
        """'doc' は有効（DB エラー以前にバリデーション通過）"""
        self._call_list_tree("doc")  # ValueError 以外は OK

    def test_valid_code(self):
        """'code' は有効"""
        self._call_list_tree("code")

    def test_none_is_valid(self):
        """None は有効（デフォルト）"""
        self._call_list_tree(None)

    def test_invalid_raises(self):
        """Invalid source_type raises ValueError"""
        with (
            patch("shiori.tools.list_tree.settings") as mock_settings,
            patch("shiori.tools.common.settings", mock_settings),
            patch("shiori.tools.list_tree._conn"),
            pytest.raises(ValueError, match="Invalid source_type"),
        ):
            list_tree(source_type="issue")

    def test_empty_string_raises(self):
        """Empty string is also invalid"""
        with (
            patch("shiori.tools.list_tree.settings") as mock_settings,
            patch("shiori.tools.common.settings", mock_settings),
            patch("shiori.tools.list_tree._conn"),
            pytest.raises(ValueError, match="Invalid source_type"),
        ):
            list_tree(source_type="")

    def test_random_string_raises(self):
        """Arbitrary string is also invalid"""
        with (
            patch("shiori.tools.list_tree.settings") as mock_settings,
            patch("shiori.tools.common.settings", mock_settings),
            patch("shiori.tools.list_tree._conn"),
            pytest.raises(ValueError, match="Invalid source_type"),
        ):
            list_tree(source_type="xyz")


# ── Helpers for end-to-end tests ──


def _entries_to_paths(entries: list[dict]) -> list[str]:
    """list_tree の戻り値から path のみのリストを抽出（後方互換確認用）。"""
    return [e["path"] for e in entries]


def _paths_with_source(
    entries: list[dict], source: str
) -> list[str]:
    """list_tree の戻り値から特定 source の path リストを抽出。"""
    return [e["path"] for e in entries if e["source"] == source]


class TestListTreeEndToEnd:
    """list_tree の end-to-end テスト。

    DB とファイルシステムをモックし、
    source_type / extension の分岐・集約・ソートを検証する。
    また、戻り値に source フィールドが正しく付与されていることも確認する。
    """

    def _mock_cursor(self, rows: list[tuple[str]]) -> MagicMock:
        """doc_files クエリ結果を返すモックカーソルを作成。"""
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
        """list_tree をモック環境で呼び出す。

        mock_walk_return が指定された場合は _walk_code_files をその戻り値でモックする。
        指定されない場合は code_files から実ファイルを作成し本物の _walk_code_files を呼ぶ。
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
                patch("shiori.tools.list_tree.settings") as mock_settings,
                patch("shiori.tools.common.settings", mock_settings),
                patch("shiori.tools.list_tree._conn", side_effect=_fake_conn),
                patch(
                    "shiori.tools.list_tree._walk_code_files",
                    return_value=mock_walk_return,
                ),
            ):
                mock_settings.repos = ["test/repo"]
                mock_settings.repo_dir.return_value = tmp
                return list_tree(source_type=source_type, extension=extension)
        else:
            with (
                patch("shiori.tools.list_tree.settings") as mock_settings,
                patch("shiori.tools.common.settings", mock_settings),
                patch("shiori.tools.list_tree._conn", side_effect=_fake_conn),
            ):
                mock_settings.repos = ["test/repo"]
                mock_settings.repo_dir.return_value = tmp
                return list_tree(source_type=source_type, extension=extension)

    def test_source_type_doc_only(self):
        """source_type='doc' は doc_files のパスのみを返し、source='doc' が付与される"""
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
        """source_type='code' はコードファイルのみを返し、source='code' が付与される"""
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
        """source_type=None は doc + code の両方を返し、各エントリに出所が付与される"""
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
        """source_type='doc' + extension='.md' で .md だけ"""
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
        """source_type='doc' + extension='.py' は空（doc に .py はない）"""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._call_list_tree(
                tmp,
                source_type="doc",
                extension=".py",
                doc_rows=[("README.md",), ("docs/guide.md",)],
            )
            assert result == []

    def test_code_with_extension_py(self):
        """source_type='code' + extension='.py' で .py だけ"""
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
        """source_type='code' + extension='.ts' で .ts だけ"""
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
        """source_type='code' + extension='.go'（マッチなし）は空"""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._call_list_tree(
                tmp,
                source_type="code",
                extension=".go",
                code_files=["main.py", "utils.py"],
            )
            assert result == []

    def test_both_with_extension_py(self):
        """source_type=None + extension='.py' は両方から .py だけ"""
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
        """結果が path でソートされている"""
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
        """doc と code で同名パスが衝突した場合、doc が優先される。

        _walk_code_files は Markdown を除外するため本番では通常発生しないが、
        防御的コードとして seen による重複排除と doc 優先を検証する。
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
