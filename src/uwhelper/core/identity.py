"""Who is acting. Single-user app: the Windows (OS) account name, or ``UWH_ACTOR_NAME`` if set.

This is attribution, not authentication: anyone using the same Windows account appears as the same actor.
Typed "authorized by" / "decided by" names are still recorded separately where a human decision is required.
"""

from __future__ import annotations

import getpass
import re
from functools import lru_cache

FALLBACK = "local_user"
_ALLOWED = re.compile(r"[^A-Za-z0-9._@\\ -]")
_override: str | None = None


def _clean(name: str | None) -> str | None:
    if not name:
        return None
    cleaned = _ALLOWED.sub("", name).strip()[:64]
    return cleaned or None


@lru_cache(maxsize=1)
def os_user() -> str:
    try:
        return _clean(getpass.getuser()) or FALLBACK
    except Exception:  # noqa: BLE001 - no usable user name (e.g. service account without env vars)
        return FALLBACK


def set_actor(name: str | None) -> None:
    """Set an explicit actor name (from settings); ``None`` reverts to the OS user."""
    global _override
    _override = _clean(name)


def current_actor() -> str:
    return _override or os_user()
