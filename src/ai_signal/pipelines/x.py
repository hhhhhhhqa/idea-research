from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import httpx

from ..models import PipelineResult, SignalItem
from .common import clean_html, iso_datetime, stable_id, within_lookback


API_BASE = "https://api.x.com/2"


def parse_x_tweets(payload: dict[str, Any], account: dict[str, Any], *, now: datetime | None = None) -> list[SignalItem]:
    now = now or datetime.now(timezone.utc)
    collected_at = now.isoformat()
    handle = str(account["handle"]).lstrip("@")
    items: list[SignalItem] = []
    for tweet in payload.get("data") or []:
        tweet_id = str(tweet["id"])
        metrics = tweet.get("public_metrics") or {}
        items.append(
            SignalItem(
                id=stable_id("x", tweet_id),
                source_type="x",
                source_name=f"@{handle}",
                title=str(tweet.get("text") or "")[:180],
                url=f"https://x.com/{handle}/status/{tweet_id}",
                published_at=iso_datetime(tweet.get("created_at"), collected_at),
                collected_at=collected_at,
                body=str(tweet.get("text") or ""),
                author=str(account.get("name") or handle),
                engagement={
                    "likes": int(metrics.get("like_count") or 0),
                    "reposts": int(metrics.get("retweet_count") or 0),
                    "replies": int(metrics.get("reply_count") or 0),
                    "quotes": int(metrics.get("quote_count") or 0),
                },
                metadata={"handle": handle, "transport": "official_api"},
            )
        )
    return items


def parse_x_rss(xml: str, account: dict[str, Any], *, now: datetime | None = None, lookback_hours: int = 72) -> list[SignalItem]:
    now = now or datetime.now(timezone.utc)
    collected_at = now.isoformat()
    handle = str(account["handle"]).lstrip("@")
    parsed = feedparser.parse(xml)
    items: list[SignalItem] = []
    for entry in parsed.entries:
        published = iso_datetime(entry.get("published") or entry.get("updated"), collected_at)
        if not within_lookback(published, lookback_hours, now):
            continue
        link = str(entry.get("link") or "")
        body = clean_html(entry.get("summary") or entry.get("description") or entry.get("title"))
        items.append(
            SignalItem(
                id=stable_id("x", str(entry.get("id") or link or body)),
                source_type="x",
                source_name=f"@{handle}",
                title=body[:180],
                url=link,
                published_at=published,
                collected_at=collected_at,
                body=body,
                author=str(account.get("name") or handle),
                metadata={"handle": handle, "transport": "rss"},
            )
        )
    return items


def collect_x(config: dict[str, Any], client: httpx.Client | None = None) -> PipelineResult:
    result = PipelineResult(pipeline="x")
    accounts = config.get("accounts") or []
    if not accounts:
        result.status = "not_configured"
        result.notes.append("No X accounts configured yet")
        return result
    token = os.environ.get("X_BEARER_TOKEN", "")
    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "ai-signal-research/0.1"})
    now = datetime.now(timezone.utc)
    lookback = int(config.get("lookback_hours", 72))
    max_results = max(5, min(100, int(config.get("max_items_per_account", 10))))
    missing_credential_accounts = 0
    try:
        for raw in accounts:
            account = {"handle": raw} if isinstance(raw, str) else raw
            handle = str(account["handle"]).lstrip("@")
            if account.get("rss_url"):
                try:
                    response = client.get(str(account["rss_url"]))
                    response.raise_for_status()
                    result.items.extend(parse_x_rss(response.text, account, now=now, lookback_hours=lookback)[:max_results])
                except Exception as exc:
                    result.errors.append(f"@{handle} RSS: {exc}")
                continue
            if not token:
                missing_credential_accounts += 1
                continue
            headers = {"Authorization": f"Bearer {token}"}
            try:
                user_response = client.get(
                    f"{API_BASE}/users/by/username/{handle}",
                    params={"user.fields": "name,username"},
                    headers=headers,
                )
                user_response.raise_for_status()
                user = user_response.json()["data"]
                start_time = (now - timedelta(hours=lookback)).isoformat().replace("+00:00", "Z")
                tweet_response = client.get(
                    f"{API_BASE}/users/{user['id']}/tweets",
                    params={
                        "max_results": max_results,
                        "start_time": start_time,
                        "exclude": "retweets,replies" if not config.get("include_replies") else "retweets",
                        "tweet.fields": "created_at,public_metrics,lang",
                    },
                    headers=headers,
                )
                tweet_response.raise_for_status()
                account = {**account, "name": account.get("name") or user.get("name") or handle}
                result.items.extend(parse_x_tweets(tweet_response.json(), account, now=now))
            except Exception as exc:
                result.errors.append(f"@{handle}: {exc}")
    finally:
        if owns_client:
            client.close()
    if missing_credential_accounts:
        result.notes.append(
            f"{missing_credential_accounts} X account(s) await X_BEARER_TOKEN or per-account rss_url"
        )
    if missing_credential_accounts == len(accounts) and not result.items and not result.errors:
        result.status = "needs_credentials"
    elif result.errors and not result.items:
        result.status = "error"
    elif result.errors or missing_credential_accounts:
        result.status = "partial"
    return result
