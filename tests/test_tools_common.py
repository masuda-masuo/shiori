"""Regression test for Issue #323: httpx.Client follow_redirects=True in _github_client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from shiori.tools.common import _github_client


class TestGithubClientFollowRedirects:
    """Verify _github_client passes follow_redirects=True to httpx.Client."""

    @patch("shiori.tools.common.build_token_provider")
    def test_github_client_has_follow_redirects(self, mock_build: MagicMock) -> None:
        mock_build.return_value = MagicMock()

        captured_kwargs: dict[str, object] = {}

        class _SpyClient:
            def __init__(self, **kwargs: object) -> None:
                captured_kwargs.update(kwargs)

            def __enter__(self) -> _SpyClient:
                return self

            def __exit__(self, *args: object) -> None:
                pass

        with patch("shiori.tools.common.httpx.Client", _SpyClient):
            with _github_client() as _client:
                pass

        assert captured_kwargs.get("follow_redirects") is True, (
            f"Expected follow_redirects=True in _github_client "
            f"but got {captured_kwargs}"
        )
