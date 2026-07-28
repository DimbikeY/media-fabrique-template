"""Shared helpers used across the pipeline.

Keep this module free of project-specific side effects: only pure functions
and tiny dataclass-y helpers. Anything that touches config, DB, or network
lives in its own module.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urlparse


def to_iso(value) -> Optional[str]:
    """Normalize a date-ish value to UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SS+00:00`).

    Accepts:
      - ``None`` / empty string → ``None``
      - ``time.struct_time`` → parsed directly
      - ISO-8601 string (with or without trailing `Z`)
      - RFC-2822 string (e.g. ``"Sat, 4 Jul 2026 19:28:34 +0000"``)

    Falls back to the raw string when nothing matches, so we never silently
    drop data — the downstream consumer can still inspect what we got.
    """
    if not value:
        return None

    if hasattr(value, "tm_year"):
        return datetime(*value[:6], tzinfo=timezone.utc).isoformat(timespec="seconds")

    s = str(value).strip()
    if not s:
        return None

    # 1) Already ISO-ish.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        dt = None

    # 2) RFC-2822 fallback.
    if dt is None:
        try:
            dt = parsedate_to_datetime(s)
        except (TypeError, ValueError):
            dt = None

    if dt is None:
        return s

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="seconds")


def is_http_url(url: Optional[str]) -> bool:
    """True iff ``url`` is a syntactically valid http(s) URL."""
    if not url:
        return False
    try:
        scheme = urlparse(url).scheme.lower()
        return scheme in ("http", "https")
    except Exception:
        return False