from __future__ import annotations

import logging
import re

from .config import Settings

log = logging.getLogger(__name__)


def _is_bot(user: dict | None) -> bool:
    if not user:
        return False
    login = (user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith("[bot]")


def _should_index(is_bot: bool, author: str | None, settings: Settings) -> bool:
    """Allow indexing even for bot comments if login is in allowlist (issue #25)."""
    if not is_bot:
        return True
    if author and author.lower() in settings.index_bot_logins:
        return True
    return False


# Regex for control char removal: matches control chars (0x00-0x1F) except newline (\n=0x0A) and tab (\t=0x09)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B-\x1F]")


def _clean_text(s: str | None) -> str:
    """Normalize control characters from GitHub API text (issue #73).
    """
    if not s:
        return ""
    return _CONTROL_CHARS_RE.sub("", s)
