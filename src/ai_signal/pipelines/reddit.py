from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from ..models import PipelineResult, SignalItem
from .common import clean_html, iso_datetime, stable_id, within_lookback


def parse_reddit_json(
    payload: dict[str, Any],
    subreddit: str,
    *,
    now: datetime | None = None,
    lookback_hours: int = 72,
) -> list[SignalItem]:
    now = now or datetime.now(timezone.utc)
    collected_at = now.isoformat()
    items: list[SignalItem] = []
    children = payload.get("data", {}).get("children", [])
    for child in children:
        post = child.get("data", {})
        created = datetime.fromtimestamp(float(post.get("created_utc") or now.timestamp()), tz=timezone.utc).isoformat()
        if not within_lookback(created, lookback_hours, now):
            continue
        permalink = str(post.get("permalink") or "")
        url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
        reddit_id = str(post.get("name") or post.get("id") or url)
        items.append(
            SignalItem(
                id=stable_id("reddit", reddit_id),
                source_type="reddit",
                source_name=f"r/{subreddit}",
                title=str(post.get("title") or "Untitled"),
                url=url,
                published_at=created,
                collected_at=collected_at,
                body=clean_html(post.get("selftext"))[:12000],
                author=str(post.get("author") or ""),
                engagement={
                    "score": int(post.get("score") or 0),
                    "comments": int(post.get("num_comments") or 0),
                    "upvote_ratio": float(post.get("upvote_ratio") or 0),
                },
                metadata={
                    "external_url": post.get("url_overridden_by_dest") or post.get("url") or "",
                    "flair": post.get("link_flair_text") or "",
                    "is_self": bool(post.get("is_self")),
                },
            )
        )
    return items


def parse_reddit_rss(
    xml: str,
    subreddit: str | None,
    *,
    now: datetime | None = None,
    lookback_hours: int = 72,
) -> list[SignalItem]:
    now = now or datetime.now(timezone.utc)
    collected_at = now.isoformat()
    parsed = feedparser.parse(xml)
    items: list[SignalItem] = []
    for entry in parsed.entries:
        published = iso_datetime(entry.get("published") or entry.get("updated"), collected_at)
        if not within_lookback(published, lookback_hours, now):
            continue
        link = str(entry.get("link") or "")
        matched = re.search(r"/r/([^/]+)/", link, re.IGNORECASE)
        resolved_subreddit = matched.group(1) if matched else (subreddit or "unknown")
        items.append(
            SignalItem(
                id=stable_id("reddit", str(entry.get("id") or link)),
                source_type="reddit",
                source_name=f"r/{resolved_subreddit}",
                title=clean_html(entry.get("title")) or "Untitled",
                url=link,
                published_at=published,
                collected_at=collected_at,
                body=clean_html(entry.get("content", [{}])[0].get("value") if entry.get("content") else entry.get("summary"))[:12000],
                author=str(entry.get("author") or ""),
                metadata={"transport": "rss"},
            )
        )
    return items


def _anonymous_combined_rss(
    client: httpx.Client,
    subreddits: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[SignalItem]:
    names = [str(entry["name"]).removeprefix("r/") for entry in subreddits]
    total_limit = min(100, sum(int(entry.get("limit", config.get("max_items_per_subreddit", 25))) for entry in subreddits))
    response = client.get(
        f"https://www.reddit.com/r/{'+'.join(names)}/new.rss",
        params={"limit": total_limit},
    )
    response.raise_for_status()
    parsed = parse_reddit_rss(
        response.text,
        None,
        lookback_hours=max(int(entry.get("lookback_hours", config.get("lookback_hours", 72))) for entry in subreddits),
    )
    caps = {
        str(entry["name"]).removeprefix("r/").casefold(): int(
            entry.get("limit", config.get("max_items_per_subreddit", 25))
        )
        for entry in subreddits
    }
    counts = {name: 0 for name in caps}
    kept: list[SignalItem] = []
    for item in parsed:
        name = item.source_name.removeprefix("r/").casefold()
        if name not in caps or counts[name] >= caps[name]:
            continue
        counts[name] += 1
        kept.append(item)
    return kept


def _oauth_token(client: httpx.Client) -> str:
    client_id = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return ""
    response = client.post(
        "https://www.reddit.com/api/v1/access_token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def collect_reddit(config: dict[str, Any], client: httpx.Client | None = None) -> PipelineResult:
    result = PipelineResult(pipeline="reddit")
    subreddits = config.get("subreddits") or []
    if not subreddits:
        result.status = "not_configured"
        result.notes.append("No subreddits configured")
        return result
    owns_client = client is None
    user_agent = os.environ.get("REDDIT_USER_AGENT") or "ai-signal-research/0.1 (research feed collector)"
    client = client or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": user_agent})
    token = ""
    try:
        try:
            token = _oauth_token(client)
        except Exception as exc:
            result.notes.append(f"Reddit OAuth unavailable; using public endpoints: {exc}")
        normalized = [{"name": raw} if isinstance(raw, str) else raw for raw in subreddits]
        if not token:
            try:
                result.items.extend(_anonymous_combined_rss(client, normalized, config))
                if not result.items:
                    raise RuntimeError("combined RSS returned no in-window posts")
                result.notes.append("Anonymous combined RSS transport used (one request for all subreddits)")
                return result
            except Exception as exc:
                result.notes.append(f"Combined RSS unavailable; trying per-subreddit fallbacks: {exc}")
        for index, entry in enumerate(normalized):
            name = str(entry["name"]).removeprefix("r/")
            limit = int(entry.get("limit", config.get("max_items_per_subreddit", 25)))
            lookback = int(entry.get("lookback_hours", config.get("lookback_hours", 72)))
            if index and not token:
                time.sleep(float(config.get("anonymous_request_delay_seconds", 2.0)))
            try:
                if token:
                    response = client.get(
                        f"https://oauth.reddit.com/r/{name}/new.json",
                        params={"limit": limit, "raw_json": 1},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    response.raise_for_status()
                    result.items.extend(parse_reddit_json(response.json(), name, lookback_hours=lookback)[:limit])
                else:
                    response = client.get(f"https://www.reddit.com/r/{name}/new.rss", params={"limit": limit})
                    response.raise_for_status()
                    result.items.extend(parse_reddit_rss(response.text, name, lookback_hours=lookback)[:limit])
                    result.notes.append(f"r/{name}: anonymous RSS transport used")
            except Exception as primary_exc:
                if token:
                    result.errors.append(f"r/{name}: OAuth JSON={primary_exc}")
                    continue
                try:
                    # Some networks block RSS while leaving the public listing available.
                    response = client.get(
                        f"https://www.reddit.com/r/{name}/new.json",
                        params={"limit": limit, "raw_json": 1},
                    )
                    response.raise_for_status()
                    result.items.extend(parse_reddit_json(response.json(), name, lookback_hours=lookback)[:limit])
                    result.notes.append(f"r/{name}: public JSON fallback used")
                except Exception as fallback_exc:
                    result.errors.append(f"r/{name}: RSS={primary_exc}; JSON={fallback_exc}")
    finally:
        if owns_client:
            client.close()
    if result.errors and not result.items:
        result.status = "error"
    elif result.errors:
        result.status = "partial"
    return result
