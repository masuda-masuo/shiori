"""GitHub API error types for the sync pipeline.

Distinguishes retryable errors (rate limits, transport) from
non-retryable ones (permission, credential) so callers can decide
whether to retry or skip (issue #345).
"""

from __future__ import annotations

import re


class GitHubApiError(Exception):
    """Base for all classified GitHub API errors.

    Carries the HTTP *status*, the request *url* and the affected *repo*
    so that logging and circuit-breaking have structured access without
    parsing message strings.
    """

    def __init__(
        self,
        status: int,
        url: str,
        repo: str,
        message: str = "",
    ) -> None:
        self.status = status
        self.url = url
        self.repo = repo
        super().__init__(message or f"HTTP {status} for {url} (repo: {repo})")


class NonRetryableGitHubError(GitHubApiError):
    """Non-retryable error — 401 (bad credentials) or 403 (forbidden).

    A 403 that is caused by rate limiting (``x-ratelimit-remaining: 0``)
    is classified as :class:`RateLimitHit` instead.
    """


class RateLimitHit(GitHubApiError):
    """Rate-limit hit — 429 or 403 with zero remaining quota.

    The fetch layer should wait and retry rather than fail the repo.
    """


class RateLimitExhausted(GitHubApiError):
    """Rate-limit retries exhausted without success.

    Distinct from :class:`NonRetryableGitHubError` so the caller can
    distinguish between "will never succeed" and "try again later".
    """


# -------------------------------------------------------------------
# Response classification
# -------------------------------------------------------------------

_RATE_LIMIT_REMAINING_RE = re.compile(r"^\s*0\s*$")


def _is_rate_limit_response(headers: dict[str, str] | None) -> bool:
    """Return True when *headers* indicate the quota is exhausted.

    Checks ``x-ratelimit-remaining`` (if present) for a value of 0.
    """
    if headers is None:
        return False
    val = headers.get("x-ratelimit-remaining")
    if val is not None and _RATE_LIMIT_REMAINING_RE.match(val):
        return True
    return False


def classify_response(
    status: int,
    headers: dict[str, str] | None,
    url: str,
    repo: str,
) -> GitHubApiError | None:
    """Classify a GitHub API HTTP response.

    Returns ``None`` for status codes that should continue with normal
    error handling (e.g. 5xx, 422 — they remain as generic errors).

    Returns:
        - :class:`NonRetryableGitHubError` for **401** or **403** that is
          *not* rate-limit related.
        - :class:`RateLimitHit` for **429** or a **403** that *is*
          rate-limit related (``x-ratelimit-remaining: 0``).
        - ``None`` for all other statuses (caller handles as before).
    """
    if status == 429:
        return RateLimitHit(
            status=status,
            url=url,
            repo=repo,
            message="Rate limit exceeded (HTTP 429)",
        )

    if status == 403 and _is_rate_limit_response(headers):
        return RateLimitHit(
            status=status,
            url=url,
            repo=repo,
            message="Rate limit exceeded (HTTP 403, x-ratelimit-remaining: 0)",
        )

    if status in (401,):
        return NonRetryableGitHubError(
            status=status,
            url=url,
            repo=repo,
            message="Authentication failed",
        )

    if status == 403:
        return NonRetryableGitHubError(
            status=status,
            url=url,
            repo=repo,
            message="Forbidden — not rate-limit related",
        )

    return None


# -------------------------------------------------------------------
# Rate-limit retry helpers
# -------------------------------------------------------------------

def _parse_retry_after(
    headers: dict[str, str] | None,
) -> float | None:
    """Parse ``Retry-After`` header.

    Returns seconds as a float, or ``None`` when the header is absent
    or unparseable.
    """
    if headers is None:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        pass
    return None


def _parse_rate_limit_reset(
    headers: dict[str, str] | None,
) -> float | None:
    """Parse ``x-ratelimit-reset`` as a Unix epoch timestamp.

    Returns seconds as a float, or ``None``.
    """
    if headers is None:
        return None
    raw = headers.get("x-ratelimit-reset")
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def compute_wait_seconds(
    headers: dict[str, str] | None,
    max_wait: float = 60.0,
) -> float:
    """Compute how many seconds to wait before retrying.

    Priority:
    1. ``Retry-After`` header (seconds).
    2. ``x-ratelimit-reset`` header (Unix timestamp → seconds from now).
    3. Fallback to 10 seconds when neither header is present.

    The result is clamped to *max_wait* seconds to prevent a bogus value
    from parking the process for hours.
    """
    retry_after = _parse_retry_after(headers)
    if retry_after is not None:
        return min(retry_after, max_wait)

    reset_ts = _parse_rate_limit_reset(headers)
    if reset_ts is not None:
        from time import time
        wait = reset_ts - time()
        if wait < 0:
            wait = 0.0
        return min(wait, max_wait)

    return min(10.0, max_wait)
