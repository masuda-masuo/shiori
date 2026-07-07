

from __future__ import annotations

from unittest.mock import MagicMock, patch, ANY, call

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
                                 "blob_url": "url_a"}], "abc1234", None)),
        ):
            result = pr_changes(number=42, repo="o/r")

        assert result["repo"] == "o/r"
        assert result["number"] == 42
        assert result["head_sha"] == "abc1234"
        assert len(result["files"]) == 1
        assert "diff" not in result

    def test_include_diff_true_returns_diff(self):
        """include_diff=True で unified diff が返される。"""
        diff_text = "diff --git a/src/a.py b/src/a.py\n@@ -1,3 +1,4 @@\n+new line"
        stat_text = " src/a.py | 1 +\n 1 file changed, 1 insertion(+)"
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.db.get_pr_changes",
                  return_value=([{"path": "src/a.py", "status": "modified",
                                 "additions": 5, "deletions": 2, "changes": 7,
                                 "blob_url": "url_a"}], "abc1234", None)),
            patch("shiori.mcp_server.build_token_provider") as mock_build,
            patch("shiori.mcp_server._git_fetch_ref",
                  return_value="refs/shiori/tmp-abc") as mock_git_fetch,
            patch("shiori.mcp_server._git_delete_ref"),
            patch("shiori.mcp_server._git",
                  side_effect=[diff_text, stat_text]) as mock_git,
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
        assert "stats" in result
        assert "1 file changed" in result["stats"]

        mock_git_fetch.assert_called_once_with(
            "pull/42/head", cwd="/data/repos/o/r", provider=mock_build.return_value
        )
        assert mock_git.call_count == 2
        mock_git.assert_has_calls([
            call([ANY, ANY, "--unified=3"], cwd="/data/repos/o/r"),
            call([ANY, ANY, "--stat"], cwd="/data/repos/o/r"),
        ])

    def test_include_diff_true_raises_when_clone_missing(self):
        """FileNotFoundError when clone is missing with include_diff=True."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.db.get_pr_changes",
                  return_value=([{"path": "src/a.py", "status": "modified",
                                 "additions": 5, "deletions": 2, "changes": 7,
                                 "blob_url": "url_a"}], "abc1234", None)),
            patch("shiori.mcp_server.settings") as mock_settings,
            patch("shiori.mcp_server.os.path.isdir", return_value=False),
            patch("shiori.mcp_server.os.path.realpath",
                  return_value="/data/repos/o/r"),
        ):
            mock_settings.repo_dir.return_value = "/data/repos/o/r"
            with pytest.raises(FileNotFoundError, match="does not exist"):
                pr_changes(number=42, repo="o/r", include_diff=True)

    def test_include_diff_true_cleans_up_tmp_ref_on_error(self):
        """Ensures tmp_ref is cleaned up on error with include_diff=True."""
        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.db.get_pr_changes",
                  return_value=([{"path": "src/a.py", "status": "modified",
                                 "additions": 5, "deletions": 2, "changes": 7,
                                 "blob_url": "url_a"}], "abc1234", None)),
            patch("shiori.mcp_server.build_token_provider") as mock_build,
            patch("shiori.mcp_server._git_fetch_ref",
                  return_value="refs/shiori/tmp-abc"),
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

    def test_include_diff_true_fetches_base_sha_when_provided(self):
        """base_sha が DB にある場合、追加で _git_fetch_ref され二点リーダ diff が使われる（#143）。"""
        diff_text = "diff --git a/src/a.py b/src/a.py\n@@ -1,3 +1,4 @@\n+new line"
        stat_text = " src/a.py | 1 +\n 1 file changed, 1 insertion(+)"
        side_effects = {
            "pull/42/head": "refs/shiori/tmp-head-abc",
            "deadbeef": "refs/shiori/tmp-base-def",
        }

        def _mock_fetch(ref, cwd, provider):
            return side_effects.get(ref, "refs/shiori/tmp-other")

        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.db.get_pr_changes",
                  return_value=([{"path": "src/a.py", "status": "modified",
                                 "additions": 5, "deletions": 2, "changes": 7,
                                 "blob_url": "url_a"}], "abc1234", "deadbeef")),
            patch("shiori.mcp_server.build_token_provider") as mock_build,
            patch("shiori.mcp_server._git_fetch_ref",
                  side_effect=_mock_fetch) as mock_git_fetch,
            patch("shiori.mcp_server._git_delete_ref") as mock_git_delete,
            patch("shiori.mcp_server._git",
                  side_effect=[diff_text, stat_text]) as mock_git,
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
        assert "diff --git a/src/a.py b/src/a.py" in result["diff"]

        # _git_fetch_ref should be called twice: PR head + base SHA
        assert mock_git_fetch.call_count == 2
        mock_git_fetch.assert_has_calls([
            call("pull/42/head", cwd="/data/repos/o/r", provider=mock_build.return_value),
            call("deadbeef", cwd="/data/repos/o/r", provider=mock_build.return_value),
        ])

        # diff should use two-dot syntax with tmp_base..tmp_head
        mock_git.assert_has_calls([
            call(["diff", "refs/shiori/tmp-base-def..refs/shiori/tmp-head-abc", "--unified=3"],
                 cwd="/data/repos/o/r"),
            call(["diff", "refs/shiori/tmp-base-def..refs/shiori/tmp-head-abc", "--stat"],
                 cwd="/data/repos/o/r"),
        ])

        # both tmp refs should be cleaned up
        mock_git_delete.assert_has_calls([
            call("refs/shiori/tmp-head-abc", cwd="/data/repos/o/r"),
            call("refs/shiori/tmp-base-def", cwd="/data/repos/o/r"),
        ], any_order=True)

    def test_include_diff_true_cleans_up_both_tmp_refs_on_error(self):
        """base_sha がある場合のエラー時、両方の tmp ref が cleanup される（#143）。"""
        side_effects = {
            "pull/42/head": "refs/shiori/tmp-head-abc",
            "deadbeef": "refs/shiori/tmp-base-def",
        }

        def _mock_fetch(ref, cwd, provider):
            return side_effects.get(ref, "refs/shiori/tmp-other")

        with (
            patch("shiori.mcp_server._resolve_repo", return_value="o/r"),
            patch("shiori.mcp_server._conn"),
            patch("shiori.mcp_server.db.get_pr_changes",
                  return_value=([{"path": "src/a.py", "status": "modified",
                                 "additions": 5, "deletions": 2, "changes": 7,
                                 "blob_url": "url_a"}], "abc1234", "deadbeef")),
            patch("shiori.mcp_server.build_token_provider") as mock_build,
            patch("shiori.mcp_server._git_fetch_ref",
                  side_effect=_mock_fetch),
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

        # both tmp refs should be cleaned up despite the error
        mock_git_delete.assert_has_calls([
            call("refs/shiori/tmp-head-abc", cwd="/data/repos/o/r"),
            call("refs/shiori/tmp-base-def", cwd="/data/repos/o/r"),
        ], any_order=True)
