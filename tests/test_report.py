"""Tests for shiori_report (issue #153)."""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import pytest

from shiori.mcp_server import report


class TestReport:
    """shiori_report tool."""

    def test_stats_basic(self):
        """stats template returns markdown table."""
        tokei_out = '{"Python": {"code": 10, "comments": 2, "blanks": 3, "reports": [{"name": "f.py", "stats": {"code": 10, "comments": 2, "blanks": 3}}]}, "Total": {"code": 10, "comments": 2, "blanks": 3}}'
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = tokei_out
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="stats")

        assert result["repo"] == "o/r"
        assert result["template"] == "stats"
        assert "markdown" in result
        assert "| Python | 1 | 10 | 2 | 3 |" in result["markdown"]
        assert "| **Total** | **1** | **10** | **2** | **3** |" in result["markdown"]

    def test_stats_multiple_languages(self):
        """Multiple languages render as separate rows."""
        tokei_out = '''{
  "Python": { "code": 10, "comments": 2, "blanks": 3, "reports": [{"name": "a.py", "stats": {"code": 10, "comments": 2, "blanks": 3}}] },
  "Rust": { "code": 20, "comments": 4, "blanks": 5, "reports": [{"name": "b.rs", "stats": {"code": 20, "comments": 4, "blanks": 5}}] },
  "Total": { "code": 30, "comments": 6, "blanks": 8 }
}'''
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = tokei_out
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="stats")

        markdown = result["markdown"]
        assert "| Python | 1 | 10 | 2 | 3 |" in markdown
        assert "| Rust | 1 | 20 | 4 | 5 |" in markdown
        assert "| **Total** | **2** | **30** | **6** | **8** |" in markdown

    def test_unknown_template(self):
        """Unknown template name raises ValueError with valid list."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
        ):
            with pytest.raises(ValueError, match="Unknown template"):
                report(template="nonexistent")

    def test_tokei_not_installed(self):
        """Missing tokei raises RuntimeError."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run", side_effect=FileNotFoundError),
        ):
            with pytest.raises(RuntimeError, match="tokei is not installed"):
                report(template="stats")

    def test_stats_sorted_alphabetically(self):
        """Languages are sorted alphabetically (case-insensitive)."""
        tokei_out = '''{
  "Rust": { "code": 1, "comments": 0, "blanks": 0, "reports": [{"name": "a.rs", "stats": {"code": 1, "comments": 0, "blanks": 0}}] },
  "c": { "code": 2, "comments": 0, "blanks": 0, "reports": [{"name": "a.c", "stats": {"code": 2, "comments": 0, "blanks": 0}}] },
  "Python": { "code": 3, "comments": 0, "blanks": 0, "reports": [{"name": "a.py", "stats": {"code": 3, "comments": 0, "blanks": 0}}] },
  "Total": { "code": 6, "comments": 0, "blanks": 0 }
}'''
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = tokei_out
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="stats")

        lines = [line_str.strip() for line_str in result["markdown"].split("\n")]
        data_rows = [line_str for line_str in lines if line_str.startswith("|") and "Language" not in line_str and "---" not in line_str and "Total" not in line_str]
        langs = [line_str.split("|")[1].strip() for line_str in data_rows]
        assert langs == ["c", "Python", "Rust"]

    def test_repo_path_param(self):
        """path parameter scopes tokei to subdirectory."""
        tokei_out = '{"Python": { "code": 1, "comments": 0, "blanks": 0, "reports": [{"name": "a.py", "stats": {"code": 1, "comments": 0, "blanks": 0}}] }, "Total": { "code": 1, "comments": 0, "blanks": 0 }}'
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = tokei_out
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="stats", path="src/")

        cmd = mock_run.call_args[0][0]
        assert "src/" in cmd[-1]
        assert result["template"] == "stats"


NDJSON_SYMBOLS = """\
{"name":"my_func","path":"/data/repos/o__r/src/lib.py","line":10,"kind":"function","access":"public"}
{"name":"MyClass","path":"/data/repos/o__r/src/lib.py","line":42,"kind":"class","access":"public"}
{"name":"_helper","path":"/data/repos/o__r/src/lib.py","line":5,"kind":"function","access":"private"}
{"name":"another_func","path":"/data/repos/o__r/src/utils.py","line":1,"kind":"function","access":"public"}
{"name":"Hidden","path":"/data/repos/o__r/src/private.py","line":3,"kind":"class","access":"protected"}
"""


class TestSymbolIndex:
    """symbol_index template (issue #154)."""

    def test_basic(self):
        """Basic symbol_index returns markdown table."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = NDJSON_SYMBOLS
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="symbol_index")

        assert result["template"] == "symbol_index"
        assert "truncated" in result
        assert result["truncated"] is False
        md = result["markdown"]
        assert "| symbol | kind | visibility | location |" in md
        assert "| my_func | function | public | src/lib.py:10 |" in md
        assert "| MyClass | class | public | src/lib.py:42 |" in md
        assert "| _helper | function | private | src/lib.py:5 |" in md

    def test_kind_filter(self):
        """kind parameter filters to matching symbol kinds."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = NDJSON_SYMBOLS
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="symbol_index", kind="class")

        assert "| MyClass | class | public | src/lib.py:42 |" in result["markdown"]
        assert "| Hidden | class | protected | src/private.py:3 |" in result["markdown"]
        assert "my_func" not in result["markdown"]
        assert "_helper" not in result["markdown"]

    def test_public_only(self):
        """public_only=True excludes private/protected symbols."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = NDJSON_SYMBOLS
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="symbol_index", public_only=True)

        assert "| my_func | function | public | src/lib.py:10 |" in result["markdown"]
        assert "| MyClass | class | public | src/lib.py:42 |" in result["markdown"]
        assert "| another_func | function | public | src/utils.py:1 |" in result["markdown"]
        assert "_helper" not in result["markdown"]
        assert "Hidden" not in result["markdown"]

    def test_public_only_missing_access(self):
        """Symbols without access field are kept when public_only=True."""
        ndjson = """\
{"name":"visible","path":"/data/repos/o__r/src/a.py","line":1,"kind":"function"}
{"name":"hidden","path":"/data/repos/o__r/src/b.py","line":2,"kind":"function","access":"private"}
"""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = ndjson
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="symbol_index", public_only=True)

        assert "| visible | function |  | src/a.py:1 |" in result["markdown"]
        assert "hidden" not in result["markdown"]

    def test_max_results_truncation(self):
        """max_results limits output and sets truncated flag."""
        lines = "\n".join(
            '{{"name":"s{}","path":"/data/repos/o__r/f.py","line":{},"kind":"function","access":"public"}}'.format(i, i)
            for i in range(10)
        )
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = lines
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="symbol_index", max_results=3)

        assert result["truncated"] is True
        assert "*Truncated: showing 3 of 10 symbols.*" in result["markdown"]

    def test_ctags_not_installed(self):
        """Missing ctags raises RuntimeError."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run", side_effect=FileNotFoundError),
        ):
            with pytest.raises(RuntimeError, match="universal-ctags is not installed"):
                report(template="symbol_index")

    def test_path_param(self):
        """path parameter is passed to ctags target."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            report(template="symbol_index", path="src/")

        cmd = mock_run.call_args[0][0]
        # ctags command should include target_path (resolved base/path)
        assert "src/" in cmd[-1]


NDJSON_MODULE_TREE = """\
{"name":"main","path":"/data/repos/o__r/src/main.py","line":1,"kind":"function","access":"public"}
{"name":"MyClass","path":"/data/repos/o__r/src/main.py","line":5,"kind":"class","access":"public"}
{"name":"helper","path":"/data/repos/o__r/src/main.py","line":3,"kind":"function","access":"private"}
{"name":"my_method","path":"/data/repos/o__r/src/main.py","line":6,"kind":"method","access":"public","scope":"MyClass","scopeKind":"class"}
{"name":"_inner","path":"/data/repos/o__r/src/main.py","line":7,"kind":"method","access":"private","scope":"MyClass","scopeKind":"class"}
{"name":"setup","path":"/data/repos/o__r/tests/conftest.py","line":1,"kind":"function","access":"public"}
"""


class TestModuleTree:
    """module_tree template (issue #155)."""

    def test_basic(self):
        """Basic module_tree returns Mermaid mindmap."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = NDJSON_MODULE_TREE
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="module_tree")

        assert result["template"] == "module_tree"
        assert "truncated" in result
        assert result["truncated"] is False
        md = result["markdown"]
        assert md.startswith("```mermaid")
        assert "mindmap" in md
        assert md.endswith("```")
        assert "src" in md
        assert "main.py" in md
        assert "tests" in md
        assert "conftest.py" in md

    def test_symbol_hierarchy(self):
        """Symbols are nested under files; scoped symbols under parents."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = NDJSON_MODULE_TREE
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="module_tree")

        md = result["markdown"]
        assert "main" in md
        assert "MyClass" in md
        assert "my_method" in md
        assert "helper" in md

    def test_degrade_when_exceeds_max_nodes(self):
        """Symbol level is dropped when node count exceeds max_nodes."""
        ndjson_lines = []
        for i in range(10):
            ndjson_lines.append(
                '{{"name":"f{}","path":"/data/repos/o__r/src/a.py","line":{},"kind":"function","access":"public"}}'.format(i, i)
            )
        ndjson = "\n".join(ndjson_lines)
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = ndjson
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="module_tree", max_results=3)

        assert result["truncated"] is True
        md = result["markdown"]
        assert "f0" not in md
        assert "f1" not in md
        # File should remain
        assert "a.py" in md
        # Directory should remain
        assert "src" in md

    def test_degrade_empty(self):
        """Degradation with empty tree produces valid mindmap."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="module_tree")

        assert result["truncated"] is False
        md = result["markdown"]
        assert md.startswith("```mermaid")
        assert md.endswith("```")

    def test_path_param(self):
        """path parameter scopes ctags to subdirectory."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            report(template="module_tree", path="src/")

        cmd = mock_run.call_args[0][0]
        assert "src/" in cmd[-1]

    def test_ctags_not_installed(self):
        """Missing ctags raises RuntimeError."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run", side_effect=FileNotFoundError),
        ):
            with pytest.raises(RuntimeError, match="universal-ctags is not installed"):
                report(template="module_tree")

    def test_no_symbols(self):
        """Repo with no symbols produces mindmap with dir+file only."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report.subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.returncode = 0
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = report(template="module_tree")

        assert result["truncated"] is False
        assert "Truncated" not in result["markdown"]


class TestApiReference:
    """api_reference template (issue #156)."""

    def test_basic_api_reference(self):
        """api_reference groups by path and formats code blocks correctly."""
        dummy_chunks = [
            {
                "path": "src/a.py",
                "heading_path": "a.py",
                "line": 10,
                "end_line": 20,
                "content": "[a.py > (function foo)]\ndef foo():\n    pass",
                "prog_lang": "python",
            },
            {
                "path": "src/a.py",
                "heading_path": "a.py",
                "line": 5,
                "end_line": 8,
                "content": "[a.py] (module)\ngap chunk",
                "prog_lang": "python",
            },
            {
                "path": "src/b.py",
                "heading_path": "b.py",
                "line": 1,
                "end_line": 15,
                "content": "[b.py > (class Bar)]\nclass Bar:\n    pass",
                "prog_lang": "python",
            },
        ]

        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report._conn"),
            patch("shiori.report.db.get_code_chunks", return_value=dummy_chunks) as mock_get,
        ):
            result = report(template="api_reference", prog_lang="python", path="src/")

        mock_get.assert_called_once_with(
            mock_get.call_args[0][0],  # conn
            repo="o/r",
            prog_lang="python",
            path_prefix="src/",
        )

        assert result["repo"] == "o/r"
        assert result["template"] == "api_reference"
        assert result["truncated"] is False

        md = result["markdown"]
        assert "## src/a.py" in md
        assert "## src/b.py" in md
        assert "[a.py] (module)" not in md

        expected_a = "L10-L20\n```python\n[a.py > (function foo)]\ndef foo():\n    pass\n```"
        expected_b = "L1-L15\n```python\n[b.py > (class Bar)]\nclass Bar:\n    pass\n```"
        assert expected_a in md
        assert expected_b in md

    def test_max_chars_truncation(self):
        """api_reference truncates and sets truncated=True when exceeding max_chars."""
        dummy_chunks = [
            {
                "path": "src/a.py",
                "heading_path": "a.py",
                "line": 1,
                "end_line": 5,
                "content": "[a.py > foo]\nfoo",
                "prog_lang": "python",
            },
            {
                "path": "src/b.py",
                "heading_path": "b.py",
                "line": 1,
                "end_line": 5,
                "content": "[b.py > bar]\nbar",
                "prog_lang": "python",
            },
        ]

        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report._conn"),
            patch("shiori.report.db.get_code_chunks", return_value=dummy_chunks),
        ):
            result = report(template="api_reference", max_chars=80)

        assert result["truncated"] is True
        assert "## src/a.py" in result["markdown"]
        assert "## src/b.py" not in result["markdown"]

    def test_only_gap_chunks(self):
        """api_reference returns empty string when only gap chunks exist for a path."""
        dummy_chunks = [
            {
                "path": "src/a.py",
                "heading_path": "a.py",
                "line": 5,
                "end_line": 8,
                "content": "[a.py] (module)\ngap chunk",
                "prog_lang": "python",
            },
        ]

        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report._conn"),
            patch("shiori.report.db.get_code_chunks", return_value=dummy_chunks),
        ):
            result = report(template="api_reference", prog_lang="python", path="src/")

        assert result["markdown"] == ""
        assert result["truncated"] is False

    def test_empty_results(self):
        """api_reference returns empty string and truncated=False when no chunks are returned."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath", side_effect=lambda p: p),
            patch("shiori.report._conn"),
            patch("shiori.report.db.get_code_chunks", return_value=[]),
        ):
            result = report(template="api_reference", prog_lang="python", path="src/")

        assert result["markdown"] == ""
        assert result["truncated"] is False