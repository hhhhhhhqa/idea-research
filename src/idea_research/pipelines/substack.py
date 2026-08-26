from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import feedparser
import httpx

from ..models import PipelineResult, SignalItem
from .common import clean_html, iso_datetime, stable_id, within_lookback


def _feed_url(publication: dict[str, Any]) -> str:
    if publication.get("feed_url"):
        return str(publication["feed_url"])
    return urljoin(str(publication["url"]).rstrip("/") + "/", "feed")


def _same_collection_day(timestamp: str, now: datetime, day_timezone: ZoneInfo | timezone) -> bool:
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(day_timezone).date() == now.astimezone(day_timezone).date()


def parse_substack_feed(
    xml: str,
    publication: dict[str, Any],
    *,
    now: datetime | None = None,
    lookback_hours: int = 168,
    max_items: int | None = None,
    same_day: bool = False,
    day_timezone: ZoneInfo | timezone = timezone.utc,
) -> list[SignalItem]:
    now = now or datetime.now(timezone.utc)
    collected_at = now.isoformat()
    parsed = feedparser.parse(xml)
    items: list[SignalItem] = []
    source_type = str(publication.get("source_type") or "substack")
    for entry in parsed.entries:
        link = str(entry.get("link") or "")
        published = iso_datetime(entry.get("published") or entry.get("updated"), collected_at)
        if same_day and not _same_collection_day(published, now, day_timezone):
            continue
        if not same_day and not within_lookback(published, lookback_hours, now):
            continue
        content = ""
        if entry.get("content"):
            content = entry["content"][0].get("value", "")
        body = clean_html(content or entry.get("summary") or entry.get("description"))
        item_key = str(entry.get("id") or entry.get("guid") or link or entry.get("title"))
        items.append(
            SignalItem(
                id=stable_id(source_type, item_key),
                source_type=source_type,
                source_name=str(publication.get("name") or parsed.feed.get("title") or publication.get("url")),
                title=clean_html(entry.get("title")) or "Untitled",
                url=link,
                published_at=published,
                collected_at=collected_at,
                body=body,
                author=str(entry.get("author") or publication.get("author") or ""),
                metadata={
                    "publication_url": publication.get("url", ""),
                    "publication_platform": publication.get("platform", "substack"),
                    "section": str(publication.get("section") or "transaction_ideas"),
                },
            )
        )
        if max_items is not None and len(items) >= max_items:
            break
    return items


def collect_substack(config: dict[str, Any], client: httpx.Client | None = None) -> PipelineResult:
    result = PipelineResult(pipeline="substack")
    publications = config.get("publications") or []
    if not publications:
        result.status = "not_configured"
        result.notes.append("No Substack publications configured")
        return result
    owns_client = client is None
    same_day = bool(config.get("same_day", False))
    timezone_name = str(config.get("timezone") or "Asia/Hong_Kong")
    try:
        day_timezone: ZoneInfo | timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        day_timezone = timezone.utc
    client = client or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "idea-research/0.1"})
    try:
        for raw in publications:
            publication = {"url": raw} if isinstance(raw, str) else raw
            if config.get("section") and "section" not in publication:
                publication = {**publication, "section": config["section"]}
            try:
                response = client.get(_feed_url(publication))
                response.raise_for_status()
                configured_max = publication.get("max_items", config.get("max_items_per_publication"))
                result.items.extend(
                    parse_substack_feed(
                        response.text,
                        publication,
                        lookback_hours=int(config.get("lookback_hours", 168)),
                        max_items=int(configured_max) if configured_max is not None else None,
                        same_day=same_day,
                        day_timezone=day_timezone,
                    )
                )
            except Exception as exc:  # one publication must not erase the others
                result.errors.append(f"{publication.get('name') or publication.get('url')}: {exc}")
    finally:
        if owns_client:
            client.close()
    if result.errors and not result.items:
        result.status = "error"
    elif result.errors:
        result.status = "partial"
    return result
