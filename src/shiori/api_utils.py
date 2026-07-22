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
    repo: str = "",
    max_wait: float = 60.0,
    max_retries: int = 3,
) -> Iterator[list[dict]]:
    """Yield one page at a time via Link header.
    Page-at-a-time processing avoids idle-in-transaction timeout on large repos
    and enables per-page cursor updates for resume on interruption (issue #250).

    When not_found_ok is True, a 404 response is treated as an empty result
    (graceful skip for repos with Issues disabled, issue #291).

    Rate limiting (429, 403 with ``x-ratelimit-remaining: 0``) is handled
    inline: waits for ``Retry-After`` / ``x-ratelimit-reset`` and retries
    the same request, bounded by *max_retries* and *max_wait* (issue #345).

    Non-retryable errors (401, 403 non-rate-limit) raise
    :class:`~shiori.github_errors.NonRetryableGitHubError` instead of the
    raw ``httpx.HTTPStatusError`` (issue #345).
    """
    import logging
    import time

    from .github_errors import (
        NonRetryableGitHubError,
        RateLimitExhausted,
        RateLimitHit,
        classify_response,
        compute_wait_seconds,
    )

    log = logging.getLogger(__name__)

    next_params: dict | None = params
    next_url: str | None = url
    while next_url:
        retries = 0
        while True:
            try:
                resp = client.get(next_url, params=next_params)
                if not_found_ok and resp.status_code == 404:
                    next_url = None
                    break
                resp.raise_for_status()
                yield resp.json()
                next_url = resp.links.get("next", {}).get("url")
                next_params = None
                break
            except httpx.HTTPStatusError as exc:
                # 404 with not_found_ok: graceful skip
                if not_found_ok and exc.response.status_code == 404:
                    next_url = None
                    break

                status = exc.response.status_code
                headers = dict(exc.response.headers)
                req_url = (
                    str(exc.response.request.url)
                    if exc.response.request is not None
                    else next_url or url
                )

                classified = classify_response(status, headers, req_url, repo)

                if isinstance(classified, RateLimitHit):
                    if retries >= max_retries:
                        raise RateLimitExhausted(
                            status=status,
                            url=req_url,
                            repo=repo,
                            message=(
                                f"Rate limit retries exhausted ({max_retries})"
                            ),
                        ) from exc
                    wait = compute_wait_seconds(headers, max_wait)
                    log.warning(
                        "Rate limit hit for %s (attempt %d/%d), waiting %.1fs",
                        repo or "?",
                        retries + 1,
                        max_retries + 1,
                        wait,
                    )
                    time.sleep(wait)
                    retries += 1
                    continue

                if isinstance(classified, NonRetryableGitHubError):
                    raise classified from exc

                # Unclassified HTTP error — re-raise as-is
                raise
