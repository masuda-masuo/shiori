"""shiori_pr_changes の include_diff パラメータのユニットテスト（issue #100）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from shiori.mcp_server import pr_changes


class TestPrChangesIncludeDiff:
    """pr_changes(include_diff=True) の振る舞い。"""

    def test_include_diff_false_returns_metadata_only(self):
        """include_diff=False（既定）は従来通りメタデータのみ返す。"""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.db.get_pr_changes",
                  return_value=([{"path": "src/a.py", "status": "modified",
                                 "additions": 5, "deletions": 2, "changes": 7,
                                 "blob_url": "url_a"}], "abc1234")),
        ):
            result = pr_changes(number=42, repo="o/r")

        assert result["repo"] == "o/r"
        assert result["number"] == 42
        assert result["head_sha"] == "abc1234"
        assert len(result["files"]) == 1
        assert "diff" not in result

    def test_include_diff_true_returns_diff(self):
        """include_diff=True で unified diff が返される。"""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.db.get_pr_changes",
                  return_value=([{"path": "src/a.py", "status": "modified",
                                 "additions": 5, "deletions": 2, "changes": 7,
                                 "blob_url": "url_a"}], "abc1234")),
            patch("shiori.mcp_server.build_token_provider") as mock_build,
            patch("shiori.mcp_server._git_fetch_ref",
                  return_value="refs/shiori/tmp-abc") as mock_git_fetch,
            patch("shiori.mcp_server._git_delete_ref") as mock_git_delete,
            patch("shiori.mcp_server._git",
                  return_value="diff --git a/src/a.py b/src/a.py\n@@ -1,3 +1,4 @@\n+new line") as mock_git,
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath",
                  return_value="/data/repos/o/r"),
        ):
            mock_build.return_value = MagicMock()
            mock_settings.repo_dir.return_value = "/data/repos/o/r"

            result = pr_changes(number=42, repo="o/r", include_diff=True)

        assert result["repo"] == "o/r"
        assert result["number"] == 42
        assert result["head_sha"] == "abc1234"
        assert len(result["files"]) == 1
        assert "diff --git a/src/a.py b/src/a.py" in result["diff"]

        mock_git_fetch.assert_called_once_with(
            "pull/42/head", cwd="/data/repos/o/r", provider=mock_build.return_value
        )
        mock_git.assert_called_once_with(
            ["diff", "HEAD...refs/shiori/tmp-abc", "--unified=3"],
            cwd="/data/repos/o/r",
        )
        mock_git_delete.assert_called_once_with("refs/shiori/tmp-abc", cwd="/data/repos/o/r")

    def test_include_diff_true_raises_when_clone_missing(self):
        """include_diff=True でクローンが無ければ FileNotFoundError。"""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.db.get_pr_changes",
                  return_value=([{"path": "src/a.py", "status": "modified",
                                 "additions": 5, "deletions": 2, "changes": 7,
                                 "blob_url": "url_a"}], "abc1234")),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=False),
            patch("shiori.mcp_server.os.path.realpath",
                  return_value="/data/repos/o/r"),
        ):
            mock_settings.repo_dir.return_value = "/data/repos/o/r"
            with pytest.raises(FileNotFoundError, match="クローンが存在しません"):
                pr_changes(number=42, repo="o/r", include_diff=True)

    def test_include_diff_true_cleans_up_tmp_ref_on_error(self):
        """include_diff=True で例外発生時も tmp_ref を確実に削除する。"""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.db.get_pr_changes",
                  return_value=([{"path": "src/a.py", "status": "modified",
                                 "additions": 5, "deletions": 2, "changes": 7,
                                 "blob_url": "url_a"}], "abc1234")),
            patch("shiori.mcp_server.build_token_provider") as mock_build,
            patch("shiori.mcp_server._git_fetch_ref",
                  return_value="refs/shiori/tmp-abc") as mock_git_fetch,
            patch("shiori.mcp_server._git_delete_ref") as mock_git_delete,
            patch("shiori.mcp_server._git",
                  side_effect=RuntimeError("git diff failed")),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=True),
            patch("shiori.mcp_server.os.path.realpath",
                  return_value="/data/repos/o/r"),
        ):
            mock_build.return_value = MagicMock()
            mock_settings.repo_dir.return_value = "/data/repos/o/r"

            with pytest.raises(RuntimeError, match="git diff failed"):
                pr_changes(number=42, repo="o/r", include_diff=True)

        mock_git_delete.assert_called_once_with("refs/shiori/tmp-abc", cwd="/data/repos/o/r")
