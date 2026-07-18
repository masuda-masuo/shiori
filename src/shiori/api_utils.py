from __future__ import annotations

from collections.abc import Iterator

import httpx

from .github_auth import TokenProvider

API = "https://api.github.com"


class _GitHubAuth(httpx.Auth):
    """httpx Auth hook. Gets token from provider per request.
    Refreshes token near expiry to survive long ingests.
    """

    def __init__(self, provider: TokenProvider) -> None:
        self._provider = provider

    def auth_flow(self, request):
        token = self._provider.get_token()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request


def _api_pages(client: httpx.Client, url: str, params: dict) -> list[dict]:
    """Paginate all pages via Link header."""
    items: list[dict] = []
    next_params: dict | None = params
    next_url: str | None = url
    while next_url:
        resp = client.get(next_url, params=next_params)
        resp.raise_for_status()
        items.extend(resp.json())
        next_url = resp.links.get("next", {}).get("url")  # type: ignore[assignment]
        next_params = None  # None not {}; preserves next URL query params as-is
    return items


def _api_pages_gen(
    client: httpx.Client, url: str, params: dict,
    not_found_ok: bool = False,
) -> Iterator[list[dict]]:
    """Yield one page at a time via Link header.
    Page-at-a-time processing avoids idle-in-transaction timeout on large repos
    and enables per-page cursor updates for resume on interruption (issue #250).

    When not_found_ok is True, a 404 response is treated as an empty result
    (graceful skip for repos with Issues disabled, issue #291).
    """
    next_params: dict | None = params
    next_url: str | None = url
    while next_url:
        resp = client.get(next_url, params=next_params)
        if not_found_ok and resp.status_code == 404:
            yield []
            next_url = None
        else:
            resp.raise_for_status()
            yield resp.json()
            next_url = resp.links.get("next", {}).get("url")
            next_params = None
