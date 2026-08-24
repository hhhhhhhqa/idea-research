from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any


TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def stable_id(source_type: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"{source_type}:{digest}"


def clean_html(value: str | None) -> str:
    text = TAG_RE.sub(" ", value or "")
    return SPACE_RE.sub(" ", html.unescape(text)).strip()


def iso_datetime(value: Any, fallback: str = "") -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def within_lookback(timestamp: str, hours: int, now: datetime) -> bool:
    if not timestamp:
        return True
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed.astimezone(timezone.utc)).total_seconds() <= hours * 3600
