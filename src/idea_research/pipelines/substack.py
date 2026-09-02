from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import feedparser
import httpx

from ..models import PipelineResult, SignalItem
from .common import clean_html, iso_datetime, stable_id, within_lookback


JINA_LINK_RE = re.compile(r"^\[(https?://[^\]]+)\]\((https?://[^)]+)\)\s*$")


def _feed_url(publication: dict[str, Any]) -> str:
    if publication.get("feed_url"):
        return str(publication["feed_url"])
    return urljoin(str(publication["url"]).rstrip("/") + "/", "feed")


def _archive_url(publication: dict[str, Any], limit: int) -> str:
    base = str(publication.get("url") or publication.get("feed_url") or "")
    parsed = urlparse(base)
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    return f"{origin}/api/v1/archive?sort=new&search=&offset=0&limit={limit}"


def parse_substack_archive(
    payload: Any,
    publication: dict[str, Any],
    *,
    now: datetime | None = None,
    lookback_hours: int = 168,
    max_items: int | None = None,
    same_day: bool = False,
    day_timezone: ZoneInfo | timezone = timezone.utc,
) -> list[SignalItem]:
    """Parse Substack's public archive JSON when a cloud runner blocks RSS."""
    now = now or datetime.now(timezone.utc)
    collected_at = now.isoformat()
    source_type = str(publication.get("source_type") or "substack")
    records = payload if isinstance(payload, list) else []
    items: list[SignalItem] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        published = iso_datetime(record.get("post_date"), collected_at)
        if same_day and not _same_collection_day(published, now, day_timezone):
            continue
        if not same_day and not within_lookback(published, lookback_hours, now):
            continue
        bylines = record.get("publishedBylines") or []
        author = ""
        if bylines and isinstance(bylines[0], dict):
            author = str(bylines[0].get("name") or bylines[0].get("handle") or "")
        slug = str(record.get("slug") or "")
        link = str(record.get("canonical_url") or "")
        if not link and slug:
            link = urljoin(str(publication.get("url") or "").rstrip("/") + "/", f"p/{slug}")
        body = clean_html(
            record.get("body_html")
            or record.get("truncated_body_text")
            or record.get("subtitle")
            or record.get("description")
        )
        # Substack RSS uses canonical_url as its GUID. Prefer the same key so a
        # fallback run replaces, rather than duplicates, an RSS-collected post.
        item_key = str(link or record.get("id") or record.get("title"))
        items.append(
            SignalItem(
                id=stable_id(source_type, item_key),
                source_type=source_type,
                source_name=str(publication.get("name") or publication.get("url")),
                title=clean_html(record.get("title")) or "Untitled",
                url=link,
                published_at=published,
                collected_at=collected_at,
                body=body,
                author=author or str(publication.get("author") or ""),
                metadata={
                    "publication_url": publication.get("url", ""),
                    "publication_platform": publication.get("platform", "substack"),
                    "section": str(publication.get("section") or "transaction_ideas"),
                    "transport": "substack_archive_api",
                },
            )
        )
        if max_items is not None and len(items) >= max_items:
            break
    return items


def parse_reader_feed_index(markdown: str) -> list[tuple[str, str]]:
    """Extract original post URLs and RSS dates from a reader-rendered feed."""
    lines = markdown.splitlines()
    entries: list[tuple[str, str]] = []
    for index, raw_line in enumerate(lines):
        match = JINA_LINK_RE.match(raw_line.strip())
        if not match or match.group(1) != match.group(2) or "/p/" not in match.group(1):
            continue
        for following in lines[index + 1 : index + 4]:
            value = following.strip()
            if not value:
                continue
            try:
                published = parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                break
            entries.append((match.group(1), published))
            break
    return entries


def _reader_items(
    client: httpx.Client,
    publication: dict[str, Any],
    *,
    now: datetime,
    lookback_hours: int,
    max_items: int | None,
    same_day: bool,
    day_timezone: ZoneInfo | timezone,
) -> list[SignalItem]:
    feed_url = _feed_url(publication)
    index_response = client.get(f"https://r.jina.ai/{feed_url}")
    index_response.raise_for_status()
    entries = parse_reader_feed_index(index_response.text)
    if not entries:
        raise ValueError("public reader returned no feed entries")
    items: list[SignalItem] = []
    collected_at = now.isoformat()
    for link, published in entries:
        if same_day and not _same_collection_day(published, now, day_timezone):
            continue
        if not same_day and not within_lookback(published, lookback_hours, now):
            continue
        article_response = client.get(f"https://r.jina.ai/{link}")
        article_response.raise_for_status()
        header, _, body = article_response.text.partition("Markdown Content:")
        title = ""
        for line in header.splitlines():
            if line.startswith("Title:"):
                title = line.removeprefix("Title:").strip()
                break
        items.append(
            SignalItem(
                id=stable_id(str(publication.get("source_type") or "substack"), link),
                source_type=str(publication.get("source_type") or "substack"),
                source_name=str(publication.get("name") or publication.get("url")),
                title=title or link.rstrip("/").rsplit("/", 1)[-1],
                url=link,
                published_at=published,
                collected_at=collected_at,
                body=body.strip(),
                author=str(publication.get("author") or ""),
                metadata={
                    "publication_url": publication.get("url", ""),
                    "publication_platform": publication.get("platform", "substack"),
                    "section": str(publication.get("section") or "transaction_ideas"),
                    "transport": "public_reader_fallback",
                },
            )
        )
        if max_items is not None and len(items) >= max_items:
            break
    return items


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
                configured_max = publication.get("max_items", config.get("max_items_per_publication"))
                max_items = int(configured_max) if configured_max is not None else None
                lookback_hours = int(config.get("lookback_hours", 168))
                try:
                    response = client.get(_feed_url(publication))
                    response.raise_for_status()
                    items = parse_substack_feed(
                        response.text,
                        publication,
                        lookback_hours=lookback_hours,
                        max_items=max_items,
                        same_day=same_day,
                        day_timezone=day_timezone,
                    )
                except Exception as rss_exc:
                    if str(publication.get("platform") or "substack") != "substack":
                        raise
                    try:
                        archive_limit = max(20, max_items or 0)
                        archive_response = client.get(_archive_url(publication, archive_limit))
                        archive_response.raise_for_status()
                        items = parse_substack_archive(
                            archive_response.json(),
                            publication,
                            lookback_hours=lookback_hours,
                            max_items=max_items,
                            same_day=same_day,
                            day_timezone=day_timezone,
                        )
                        transport = "archive API"
                    except Exception:
                        items = _reader_items(
                            client,
                            publication,
                            now=datetime.now(timezone.utc),
                            lookback_hours=lookback_hours,
                            max_items=max_items,
                            same_day=same_day,
                            day_timezone=day_timezone,
                        )
                        transport = "public reader"
                    result.notes.append(f"{publication.get('name') or publication.get('url')}: {transport} fallback used")
                result.items.extend(items)
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
