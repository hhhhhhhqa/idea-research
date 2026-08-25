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


TICKER_STOPWORDS = {
    "AI",
    "ATH",
    "CEO",
    "CFO",
    "DD",
    "ETF",
    "FOMO",
    "GDP",
    "IPO",
    "LOL",
    "NYSE",
    "OTM",
    "SEC",
    "USA",
    "WSB",
    "YOLO",
}


def extract_reddit_symbols(
    text: str,
    known_tickers: list[str] | None = None,
    ticker_aliases: dict[str, list[str]] | None = None,
) -> list[str]:
    """Extract conservative ticker mentions without treating every acronym as a stock."""
    known = {str(value).upper() for value in known_tickers or []}
    aliases = ticker_aliases or {}
    symbols: list[str] = []

    def add(value: str) -> None:
        symbol = value.upper()
        if symbol not in TICKER_STOPWORDS and symbol not in symbols:
            symbols.append(symbol)

    for match in re.finditer(r"(?<![A-Za-z0-9])\$([A-Za-z][A-Za-z0-9.-]{0,9})\b", text):
        add(match.group(1))
    for token in re.findall(r"(?<![A-Za-z0-9$])([A-Z][A-Z0-9.-]{0,9})(?![A-Za-z0-9])", text):
        if token.upper() in known:
            add(token)
    lowered = text.casefold()
    for ticker, values in aliases.items():
        for alias in values or []:
            alias_text = str(alias).strip().casefold()
            if alias_text and re.search(rf"(?<![a-z0-9]){re.escape(alias_text)}(?![a-z0-9])", lowered):
                add(str(ticker))
                break
    return symbols


def parse_reddit_json(
    payload: dict[str, Any],
    subreddit: str,
    *,
    now: datetime | None = None,
    lookback_hours: int | None = 72,
    listing: str = "new",
    known_tickers: list[str] | None = None,
    ticker_aliases: dict[str, list[str]] | None = None,
) -> list[SignalItem]:
    now = now or datetime.now(timezone.utc)
    collected_at = now.isoformat()
    items: list[SignalItem] = []
    children = payload.get("data", {}).get("children", [])
    for feed_rank, child in enumerate(children, start=1):
        post = child.get("data", {})
        created = datetime.fromtimestamp(float(post.get("created_utc") or now.timestamp()), tz=timezone.utc).isoformat()
        if lookback_hours is not None and not within_lookback(created, lookback_hours, now):
            continue
        permalink = str(post.get("permalink") or "")
        url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
        reddit_id = str(post.get("name") or post.get("id") or url)
        title = str(post.get("title") or "Untitled")
        body = clean_html(post.get("selftext"))[:12000]
        items.append(
            SignalItem(
                id=stable_id("reddit", reddit_id),
                source_type="reddit",
                source_name=f"r/{subreddit}",
                title=title,
                url=url,
                published_at=created,
                collected_at=collected_at,
                body=body,
                author=str(post.get("author") or ""),
                symbols=extract_reddit_symbols(
                    f"{title}\n{body}",
                    known_tickers=known_tickers,
                    ticker_aliases=ticker_aliases,
                ),
                engagement={
                    "score": int(post.get("score") or 0),
                    "comments": int(post.get("num_comments") or 0),
                    "upvote_ratio": float(post.get("upvote_ratio") or 0),
                },
                metadata={
                    "external_url": post.get("url_overridden_by_dest") or post.get("url") or "",
                    "flair": post.get("link_flair_text") or "",
                    "is_self": bool(post.get("is_self")),
                    "transport": "oauth_json",
                    "listing": listing,
                    "feed_rank": feed_rank,
                },
            )
        )
    return items


def parse_reddit_rss(
    xml: str,
    subreddit: str | None,
    *,
    now: datetime | None = None,
    lookback_hours: int | None = 72,
    listing: str = "new",
    known_tickers: list[str] | None = None,
    ticker_aliases: dict[str, list[str]] | None = None,
) -> list[SignalItem]:
    now = now or datetime.now(timezone.utc)
    collected_at = now.isoformat()
    parsed = feedparser.parse(xml)
    items: list[SignalItem] = []
    for feed_rank, entry in enumerate(parsed.entries, start=1):
        published = iso_datetime(entry.get("published") or entry.get("updated"), collected_at)
        if lookback_hours is not None and not within_lookback(published, lookback_hours, now):
            continue
        link = str(entry.get("link") or "")
        matched = re.search(r"/r/([^/]+)/", link, re.IGNORECASE)
        resolved_subreddit = matched.group(1) if matched else (subreddit or "unknown")
        title = clean_html(entry.get("title")) or "Untitled"
        body = clean_html(entry.get("content", [{}])[0].get("value") if entry.get("content") else entry.get("summary"))[:12000]
        items.append(
            SignalItem(
                id=stable_id("reddit", str(entry.get("id") or link)),
                source_type="reddit",
                source_name=f"r/{resolved_subreddit}",
                title=title,
                url=link,
                published_at=published,
                collected_at=collected_at,
                body=body,
                author=str(entry.get("author") or ""),
                symbols=extract_reddit_symbols(
                    f"{title}\n{body}",
                    known_tickers=known_tickers,
                    ticker_aliases=ticker_aliases,
                ),
                metadata={"transport": "rss", "listing": listing, "feed_rank": feed_rank},
            )
        )
    return items


def _anonymous_combined_rss(
    client: httpx.Client,
    subreddits: list[dict[str, Any]],
    config: dict[str, Any],
    listing: str,
) -> list[SignalItem]:
    names = [str(entry["name"]).removeprefix("r/") for entry in subreddits]
    total_limit = min(100, sum(int(entry.get("limit", config.get("max_items_per_subreddit", 25))) for entry in subreddits))
    response = client.get(
        f"https://www.reddit.com/r/{'+'.join(names)}/{listing}.rss",
        params={"limit": total_limit},
    )
    response.raise_for_status()
    configured_lookbacks = [entry.get("lookback_hours", config.get("lookback_hours", 72)) for entry in subreddits]
    lookbacks = [int(value) for value in configured_lookbacks if value is not None]
    parsed = parse_reddit_rss(
        response.text,
        None,
        lookback_hours=max(lookbacks) if lookbacks else None,
        listing=listing,
        known_tickers=config.get("known_tickers") or [],
        ticker_aliases=config.get("ticker_aliases") or {},
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


def _refresh_token(client: httpx.Client) -> str:
    """Exchange a long-lived REDDIT_REFRESH_TOKEN for a short-lived access token.

    Personal-account flow: the refresh token was minted during a one-time
    authorize step (see ``idea-research reddit-auth``) and lets the collector act
    as that Reddit account under the ``read`` scope. Returns "" when the
    environment lacks the required variables, so callers fall back cleanly.
    """
    client_id = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    refresh_token = os.environ.get("REDDIT_REFRESH_TOKEN", "")
    if not client_id or not client_secret or not refresh_token:
        return ""
    response = client.post(
        "https://www.reddit.com/api/v1/access_token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(client_id, client_secret),
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def _acquire_token(client: httpx.Client, notes: list[str]) -> str:
    """Pick the strongest available Reddit OAuth transport.

    Personal-account OAuth (``REDDIT_REFRESH_TOKEN``) is preferred over
    app-only ``client_credentials``; either is preferred over the anonymous
    RSS/JSON fallbacks used by the caller when this returns "".
    """
    token = ""
    try:
        token = _refresh_token(client)
        if token:
            notes.append("Personal-account OAuth used (REDDIT_REFRESH_TOKEN)")
    except Exception as exc:
        notes.append(f"Personal-account OAuth unavailable; trying app-only OAuth: {exc}")
    if not token:
        try:
            token = _oauth_token(client)
            if token:
                notes.append("App-only OAuth used (client_credentials)")
        except Exception as exc:
            notes.append(f"Reddit OAuth unavailable; using public endpoints: {exc}")
    return token


def collect_reddit(config: dict[str, Any], client: httpx.Client | None = None) -> PipelineResult:
    result = PipelineResult(pipeline="reddit")
    subreddits = config.get("subreddits") or []
    if not subreddits:
        result.status = "not_configured"
        result.notes.append("No subreddits configured")
        return result
    owns_client = client is None
    user_agent = os.environ.get("REDDIT_USER_AGENT") or "idea-research/0.1 (research feed collector)"
    client = client or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": user_agent})
    token = ""
    try:
        token = _acquire_token(client, result.notes)
        normalized = [{"name": raw} if isinstance(raw, str) else raw for raw in subreddits]
        if not token:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for entry in normalized:
                listing = str(entry.get("listing", "new")).lower()
                grouped.setdefault(listing, []).append(entry)
            failed_entries: list[dict[str, Any]] = []
            for index, (listing, entries) in enumerate(grouped.items()):
                if index:
                    time.sleep(float(config.get("anonymous_request_delay_seconds", 2.0)))
                try:
                    collected = _anonymous_combined_rss(client, entries, config, listing)
                    if not collected:
                        raise RuntimeError("combined RSS returned no in-window posts")
                    result.items.extend(collected)
                    names = ", ".join(f"r/{entry['name']}" for entry in entries)
                    result.notes.append(f"Anonymous {listing} RSS transport used for {names}")
                except Exception as exc:
                    failed_entries.extend(entries)
                    result.notes.append(f"Combined {listing} RSS unavailable; trying per-subreddit fallbacks: {exc}")
            if not failed_entries:
                return result
            normalized = failed_entries
        for index, entry in enumerate(normalized):
            name = str(entry["name"]).removeprefix("r/")
            limit = int(entry.get("limit", config.get("max_items_per_subreddit", 25)))
            configured_lookback = entry.get("lookback_hours", config.get("lookback_hours", 72))
            lookback = int(configured_lookback) if configured_lookback is not None else None
            listing = str(entry.get("listing", "new")).lower()
            if listing not in {"new", "hot", "top"}:
                result.errors.append(f"r/{name}: unsupported listing={listing}")
                continue
            if index and not token:
                time.sleep(float(config.get("anonymous_request_delay_seconds", 2.0)))
            try:
                if token:
                    response = client.get(
                        f"https://oauth.reddit.com/r/{name}/{listing}.json",
                        params={"limit": limit, "raw_json": 1},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    response.raise_for_status()
                    result.items.extend(
                        parse_reddit_json(
                            response.json(),
                            name,
                            lookback_hours=lookback,
                            listing=listing,
                            known_tickers=config.get("known_tickers") or [],
                            ticker_aliases=config.get("ticker_aliases") or {},
                        )[:limit]
                    )
                else:
                    response = client.get(f"https://www.reddit.com/r/{name}/{listing}.rss", params={"limit": limit})
                    response.raise_for_status()
                    result.items.extend(
                        parse_reddit_rss(
                            response.text,
                            name,
                            lookback_hours=lookback,
                            listing=listing,
                            known_tickers=config.get("known_tickers") or [],
                            ticker_aliases=config.get("ticker_aliases") or {},
                        )[:limit]
                    )
                    result.notes.append(f"r/{name}: anonymous {listing} RSS transport used")
            except Exception as primary_exc:
                if token:
                    result.errors.append(f"r/{name}: OAuth JSON={primary_exc}")
                    continue
                try:
                    # Some networks block RSS while leaving the public listing available.
                    response = client.get(
                        f"https://www.reddit.com/r/{name}/{listing}.json",
                        params={"limit": limit, "raw_json": 1},
                    )
                    response.raise_for_status()
                    result.items.extend(
                        parse_reddit_json(
                            response.json(),
                            name,
                            lookback_hours=lookback,
                            listing=listing,
                            known_tickers=config.get("known_tickers") or [],
                            ticker_aliases=config.get("ticker_aliases") or {},
                        )[:limit]
                    )
                    result.notes.append(f"r/{name}: public {listing} JSON fallback used")
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
