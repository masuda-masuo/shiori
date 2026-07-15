
from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest

from shiori.mcp_server import pr_changes


class TestPrChangesIncludeDiff:
    """pr_changes(include_diff) の振る舞い（issue #259: git clone 層で動作）。"""

    def _setup_basic_mocks(
        self,
        mock_settings,
        mock_isdir,
        mock_realpath,
        mock_build,
    ):
        mock_settings.repo_dir.return_value = "/data/repos/o/r"
        mock_isdir.return_value = True
        mock_realpath.return_value = "/data/repos/o/r"
        mock_build.return_value = MagicMock(name="provider")

    def test_include_diff_false_returns_metadata_only(self):
        """include_diff=False（既定）はファイル一覧のみ返す。"""
        name_status = "M\tsrc/a.py\nA\tsrc/b.py"
        numstat = "5\t2\tsrc/a.py\n10\t0\tsrc/b.py"
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.build_token_provider") as mock_build,
            patch("shiori.mcp_server._git_fetch_ref",
                  side_effect=["refs/shiori/tmp-head", "refs/shiori/tmp-base"]),
            patch("shiori.mcp_server._git_delete_ref"),
            patch("shiori.mcp_server._git",
                  side_effect=[
                      "headsha123",
                      "basesha456",
                      name_status,
                      numstat,
                  ]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir") as mock_isdir,
            patch("shiori.mcp_server.os.path.realpath") as mock_realpath,
        ):
            self._setup_basic_mocks(
                mock_settings, mock_isdir, mock_realpath, mock_build,
            )
            result = pr_changes(number=42, repo="o/r")

        assert result["repo"] == "o/r"
        assert result["number"] == 42
        assert result["head_sha"] == "headsha123"
        assert result["base_sha"] == "basesha456"
        assert result["files"] == [
            {"path": "src/a.py", "status": "M", "additions": 5, "deletions": 2, "changes": 7},
            {"path": "src/b.py", "status": "A", "additions": 10, "deletions": 0, "changes": 10},
        ]
        assert "diff" not in result
        assert "stats" not in result

    def test_include_diff_true_returns_diff(self):
        """include_diff=True で unified diff が返される。"""
        name_status = "M\tsrc/a.py"
        numstat = "5\t2\tsrc/a.py"
        diff_text = "diff --git a/src/a.py b/src/a.py\n@@ -1,3 +1,4 @@\n+new line"
        stat_text = " src/a.py | 1 +\n 1 file changed, 1 insertion(+)"
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.build_token_provider") as mock_build,
            patch("shiori.mcp_server._git_fetch_ref",
                  side_effect=["refs/shiori/tmp-head", "refs/shiori/tmp-base"]),
            patch("shiori.mcp_server._git_delete_ref"),
            patch("shiori.mcp_server._git",
                  side_effect=[
                      "headsha123",
                      "basesha456",
                      name_status,
                      numstat,
                      diff_text,
                      stat_text,
                  ]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir") as mock_isdir,
            patch("shiori.mcp_server.os.path.realpath") as mock_realpath,
        ):
            self._setup_basic_mocks(
                mock_settings, mock_isdir, mock_realpath, mock_build,
            )
            result = pr_changes(number=42, repo="o/r", include_diff=True)

        assert result["repo"] == "o/r"
        assert result["number"] == 42
        assert result["head_sha"] == "headsha123"
        assert result["base_sha"] == "basesha456"
        assert result["files"] == [
            {"path": "src/a.py", "status": "M", "additions": 5, "deletions": 2, "changes": 7},
        ]
        assert "diff --git a/src/a.py b/src/a.py" in result["diff"]
        assert "stats" in result
        assert "1 file changed" in result["stats"]

    def test_include_diff_true_raises_when_clone_missing(self):
        """FileNotFoundError when clone is missing."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=False),
            patch("shiori.mcp_server.os.path.realpath",
                  return_value="/data/repos/o/r"),
        ):
            mock_settings.repo_dir.return_value = "/data/repos/o/r"
            with pytest.raises(FileNotFoundError, match="does not exist"):
                pr_changes(number=42, repo="o/r", include_diff=True)

    def test_include_diff_true_cleans_up_tmp_ref_on_error(self):
        """エラー時に両方の tmp ref が cleanup される。"""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.build_token_provider") as mock_build,
            patch("shiori.mcp_server._git_fetch_ref",
                  side_effect=["refs/shiori/tmp-head", "refs/shiori/tmp-base"]),
            patch("shiori.mcp_server._git_delete_ref") as mock_git_delete,
            patch("shiori.mcp_server._git",
                  side_effect=[
                      "headsha123",
                      "basesha456",
                      RuntimeError("git diff failed"),
                  ]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir") as mock_isdir,
            patch("shiori.mcp_server.os.path.realpath") as mock_realpath,
        ):
            self._setup_basic_mocks(
                mock_settings, mock_isdir, mock_realpath, mock_build,
            )
            with pytest.raises(RuntimeError, match="git diff failed"):
                pr_changes(number=42, repo="o/r", include_diff=True)

        mock_git_delete.assert_has_calls([
            call("refs/shiori/tmp-head", cwd="/data/repos/o/r"),
            call("refs/shiori/tmp-base", cwd="/data/repos/o/r"),
        ], any_order=True)

    def test_include_diff_true_fetches_pr_head_and_origin_head(self):
        """PR head + origin/HEAD の両方を fetch し二点リーダ diff する。"""
        name_status = "M\tsrc/a.py"
        numstat = "2\t1\tsrc/a.py"
        diff_text = "diff --git a/src/a.py b/src/a.py\n@@ -1,3 +1,4 @@\n+new line"
        stat_text = " src/a.py | 1 +\n 1 file changed, 1 insertion(+)"
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.build_token_provider") as mock_build,
            patch("shiori.mcp_server._git_fetch_ref",
                  side_effect=["refs/shiori/tmp-head", "refs/shiori/tmp-base"]) as mock_git_fetch,
            patch("shiori.mcp_server._git_delete_ref"),
            patch("shiori.mcp_server._git",
                  side_effect=[
                      "headsha123",
                      "basesha456",
                      name_status,
                      numstat,
                      diff_text,
                      stat_text,
                  ]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir") as mock_isdir,
            patch("shiori.mcp_server.os.path.realpath") as mock_realpath,
        ):
            self._setup_basic_mocks(
                mock_settings, mock_isdir, mock_realpath, mock_build,
            )
            result = pr_changes(number=42, repo="o/r", include_diff=True)

        assert result["repo"] == "o/r"
        assert result["number"] == 42
        assert result["head_sha"] == "headsha123"
        assert result["base_sha"] == "basesha456"

        assert mock_git_fetch.call_count == 2
        mock_git_fetch.assert_has_calls([
            call("pull/42/head", cwd="/data/repos/o/r", provider=mock_build.return_value),
            call("origin/HEAD", cwd="/data/repos/o/r", provider=mock_build.return_value),
        ], any_order=False)

    def test_include_diff_true_handles_rename_in_name_status(self):
        """Rename エントリ（R100\told\tnew）が正しくパースされる。"""
        name_status = "R100\tsrc/old.py\tsrc/new.py"
        numstat = "0\t0\tsrc/old.py\tsrc/new.py"
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server.build_token_provider") as mock_build,
            patch("shiori.mcp_server._git_fetch_ref",
                  side_effect=["refs/shiori/tmp-head", "refs/shiori/tmp-base"]),
            patch("shiori.mcp_server._git_delete_ref"),
            patch("shiori.mcp_server._git",
                  side_effect=[
                      "headsha123",
                      "basesha456",
                      name_status,
                      numstat,
                  ]),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir") as mock_isdir,
            patch("shiori.mcp_server.os.path.realpath") as mock_realpath,
        ):
            self._setup_basic_mocks(
                mock_settings, mock_isdir, mock_realpath, mock_build,
            )
            result = pr_changes(number=42, repo="o/r")

        assert result["files"] == [
            {"path": "src/new.py", "status": "R100", "additions": 0, "deletions": 0, "changes": 0},
        ]
