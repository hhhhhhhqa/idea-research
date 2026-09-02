from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import feedparser
import httpx
from bs4 import BeautifulSoup

from ..config import project_root
from ..models import PipelineResult, SignalItem
from .common import clean_html, iso_datetime, stable_id, within_lookback


API_BASE = "https://api.x.com/2"
# twscrape account name under which the TWITTER_COOKIES session is registered.
TWSCRAPE_ACCOUNT = "feed_bot"
ACCOUNT_CONTEXT_FIELDS = ("investor_type", "coverage", "evidence_note")
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
DEFAULT_X_TIMEZONE = "Asia/Hong_Kong"
TWITTER_COOKIE_FILE = "credentials/x_twitter_cookies.txt"


def _load_twitter_cookie_values() -> list[str]:
    """Load one or more X cookie strings from env vars or the ignored local file."""
    env_values: list[tuple[int, str]] = []
    for key, value in os.environ.items():
        match = re.fullmatch(r"TWITTER_COOKIES(?:_(\d+))?", key)
        if match and value.strip():
            env_values.append((int(match.group(1) or 1), value.strip()))
    if env_values:
        return [value for _, value in sorted(env_values)]
    path = project_root() / TWITTER_COOKIE_FILE
    try:
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except FileNotFoundError:
        return []
    return lines


def _load_twitter_cookies() -> str:
    """Backward-compatible helper returning the first configured cookie string."""
    values = _load_twitter_cookie_values()
    return values[0] if values else ""


def _zone(value: str | None) -> ZoneInfo | timezone:
    """Resolve the collection timezone without making a bad config fatal."""
    name = str(value or DEFAULT_X_TIMEZONE)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _parsed_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _same_collection_day(timestamp: str, now: datetime, day_timezone: ZoneInfo | timezone) -> bool:
    """Return whether a timestamp falls on today's calendar day in the configured timezone."""
    parsed = _parsed_datetime(timestamp)
    if parsed is None:
        return True
    return parsed.astimezone(day_timezone).date() == now.astimezone(day_timezone).date()


def _before_collection_day(value: Any, now: datetime, day_timezone: ZoneInfo | timezone) -> bool:
    parsed = _parsed_datetime(value)
    if parsed is None:
        return False
    return parsed.astimezone(day_timezone).date() < now.astimezone(day_timezone).date()


def _before_lookback(value: Any, now: datetime, lookback_hours: int) -> bool:
    parsed = _parsed_datetime(value)
    if parsed is None:
        return False
    return parsed.astimezone(timezone.utc) < now.astimezone(timezone.utc) - timedelta(hours=lookback_hours)


def _day_start(now: datetime, day_timezone: ZoneInfo | timezone) -> datetime:
    local_now = now.astimezone(day_timezone)
    return datetime.combine(local_now.date(), datetime.min.time(), tzinfo=day_timezone).astimezone(timezone.utc)


def _twscrape_wait_timeout(config: dict[str, Any]) -> float | None:
    """Return a finite hosted-runner wait without changing local defaults."""
    raw_value = os.environ.get("TWSCRAPE_WAIT_TIMEOUT_SECONDS")
    if raw_value is None:
        raw_value = config.get("cooldown_wait_timeout_seconds")
    if raw_value in (None, ""):
        return None
    return max(0.0, float(raw_value))


def _account_metadata(account: dict[str, Any], transport: str) -> dict[str, Any]:
    """Keep the configured research context attached to every collected post."""
    handle = str(account["handle"]).lstrip("@")
    return {
        "handle": handle,
        "transport": transport,
        "section": str(account.get("section") or "transaction_ideas"),
        **{field: account[field] for field in ACCOUNT_CONTEXT_FIELDS if account.get(field)},
    }


def extract_link_urls(text: str) -> list[str]:
    """Return de-duplicated HTTP(S) links appearing in a post's visible text."""
    urls: list[str] = []
    for raw_url in URL_PATTERN.findall(text):
        url = raw_url.rstrip(".,;:!?)]}\"")
        if url not in urls:
            urls.append(url)
    return urls


def _is_public_http_url(url: str) -> bool:
    """Reject non-web and local/private destinations before fetching linked content."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.rstrip(".").casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    # In a proxied development environment DNS resolves every public hostname
    # to the benchmark network 198.18.0.0/15. Literal private hosts remain
    # rejected, while the proxy itself applies the outbound connection policy.
    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        try:
            return not ipaddress.ip_address(host).is_private
        except ValueError:
            return True
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        benchmark_network = ipaddress.ip_network("198.18.0.0/15")
        if addresses and all(ipaddress.ip_address(address) in benchmark_network for address in addresses):
            return True
        return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        return False


def extract_article_content(html: str, max_body_chars: int) -> tuple[str, str]:
    """Extract a readable title and paragraph text from a linked HTML document."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form"]):
        node.decompose()
    title_node = soup.select_one('meta[property="og:title"]') or soup.title
    title = ""
    if title_node:
        title = str(title_node.get("content") or title_node.get_text(" ", strip=True)).strip()
    root = soup.select_one("article") or soup.select_one("main") or soup.body or soup
    paragraphs = [node.get_text(" ", strip=True) for node in root.select("p")]
    body = "\n\n".join(part for part in paragraphs if len(part) >= 40)
    return title[:500], body[:max_body_chars]


def fetch_linked_article(
    url: str,
    client: httpx.Client,
    *,
    max_body_chars: int,
    max_download_bytes: int,
    max_redirects: int = 5,
) -> dict[str, str] | None:
    """Resolve a public URL and retain text from its final HTML destination.

    Redirects are handled one hop at a time so a tweet cannot redirect this
    collector to a private network address.
    """
    current_url = url
    for _ in range(max_redirects + 1):
        if urlparse(current_url).hostname in X_HOSTS:
            return None
        if not _is_public_http_url(current_url):
            return None
        with client.stream(
            "GET",
            current_url,
            follow_redirects=False,
            headers={"Accept": "text/html,application/xhtml+xml"},
        ) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    return None
                current_url = urljoin(current_url, location)
                continue
            if response.status_code != 200:
                return None
            content_type = response.headers.get("content-type", "").casefold()
            if "html" not in content_type:
                return None
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) >= max_download_bytes:
                    break
        title, body = extract_article_content(content.decode("utf-8", errors="replace"), max_body_chars)
        if not body:
            return None
        return {"original_url": url, "url": current_url, "title": title, "body": body}
    return None


def enrich_linked_articles(items: list[SignalItem], client: httpx.Client, config: dict[str, Any]) -> tuple[int, int]:
    """Add fetched linked-article text to X posts without affecting post ranking."""
    settings = config.get("linked_articles") or {}
    if not settings.get("enabled", False):
        return 0, 0
    configured_links_per_post = settings.get("max_links_per_post")
    configured_articles_per_run = settings.get("max_articles_per_run")
    max_links_per_post = max(1, int(configured_links_per_post)) if configured_links_per_post is not None else None
    max_articles_per_run = max(1, int(configured_articles_per_run)) if configured_articles_per_run is not None else None
    max_body_chars = max(500, int(settings.get("max_body_chars", 12000)))
    max_download_bytes = max(100_000, int(settings.get("max_download_bytes", 1_000_000)))
    cache: dict[str, dict[str, str] | None] = {}
    attempted = 0
    stored = 0
    skipped = 0

    for item in items:
        candidates = [url for url in extract_link_urls(item.body) if urlparse(url).hostname not in X_HOSTS]
        articles: list[dict[str, str]] = []
        for url in candidates if max_links_per_post is None else candidates[:max_links_per_post]:
            if url not in cache:
                if max_articles_per_run is not None and attempted >= max_articles_per_run:
                    skipped += 1
                    continue
                attempted += 1
                try:
                    cache[url] = fetch_linked_article(
                        url,
                        client,
                        max_body_chars=max_body_chars,
                        max_download_bytes=max_download_bytes,
                    )
                except Exception:
                    # A linked article is enrichment only. Certificate or
                    # network failures must not discard the source X post.
                    cache[url] = None
                if cache[url]:
                    stored += 1
            article = cache[url]
            if article:
                articles.append(article)
            else:
                skipped += 1
        if articles:
            item.metadata["external_articles"] = articles
    return stored, skipped


def parse_x_tweets(
    payload: dict[str, Any],
    account: dict[str, Any],
    *,
    now: datetime | None = None,
    same_day: bool = False,
    day_timezone: ZoneInfo | timezone = timezone.utc,
) -> list[SignalItem]:
    now = now or datetime.now(timezone.utc)
    collected_at = now.isoformat()
    handle = str(account["handle"]).lstrip("@")
    items: list[SignalItem] = []
    for tweet in payload.get("data") or []:
        if same_day and not _same_collection_day(str(tweet.get("created_at") or ""), now, day_timezone):
            continue
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
                metadata=_account_metadata(account, "official_api"),
            )
        )
    return items


def parse_x_rss(
    xml: str,
    account: dict[str, Any],
    *,
    now: datetime | None = None,
    lookback_hours: int = 72,
    same_day: bool = False,
    day_timezone: ZoneInfo | timezone = timezone.utc,
) -> list[SignalItem]:
    now = now or datetime.now(timezone.utc)
    collected_at = now.isoformat()
    handle = str(account["handle"]).lstrip("@")
    parsed = feedparser.parse(xml)
    items: list[SignalItem] = []
    for entry in parsed.entries:
        published = iso_datetime(entry.get("published") or entry.get("updated"), collected_at)
        if same_day and not _same_collection_day(published, now, day_timezone):
            continue
        if not same_day and not within_lookback(published, lookback_hours, now):
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
                metadata=_account_metadata(account, "rss"),
            )
        )
    return items


def _tweet_author_handle(tweet: Any) -> str:
    """Return the canonical author handle for a twscrape Tweet when available.

    Search can return the original Tweet object for a repost even for a
    ``from:<handle>`` query, in which case ``rawContent`` no longer starts with
    ``RT @``. Author identity (or the canonical URL) is the reliable guard.
    """
    user = getattr(tweet, "user", None)
    for attr in ("username", "screenName"):
        value = getattr(user, attr, None) if user is not None else None
        if value:
            return str(value).lstrip("@")
    url = str(getattr(tweet, "url", "") or "")
    try:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.netloc.lower() in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            if len(parts) >= 3 and parts[1].lower() == "status":
                return parts[0].lstrip("@")
    except Exception:
        pass
    return ""


def _tweet_engagement_score(tweet: Any) -> int:
    return (
        int(getattr(tweet, "likeCount", 0) or 0)
        + int(getattr(tweet, "retweetCount", 0) or 0) * 2
        + int(getattr(tweet, "replyCount", 0) or 0)
    )


def _is_reply_tweet(tweet: Any) -> bool:
    if getattr(tweet, "inReplyToTweetId", None):
        return True
    return str(getattr(tweet, "rawContent", "") or "").lstrip().startswith("@")


def parse_twscrape_tweet(
    tweet: Any,
    account: dict[str, Any],
    *,
    now: datetime | None = None,
    lookback_hours: int = 72,
    same_day: bool = False,
    day_timezone: ZoneInfo | timezone = timezone.utc,
) -> SignalItem | None:
    """Map a twscrape Tweet (or duck-typed equivalent) to a SignalItem.

    Returns ``None`` when the tweet falls outside the lookback window. The
    object is duck-typed (``id``/``rawContent``/``date``/``likeCount``/...),
    so tests can pass lightweight stand-ins without importing twscrape.
    """
    now = now or datetime.now(timezone.utc)
    collected_at = now.isoformat()
    handle = str(account["handle"]).lstrip("@")
    published = iso_datetime(getattr(tweet, "date", None), collected_at)
    if same_day and not _same_collection_day(published, now, day_timezone):
        return None
    if not same_day and not within_lookback(published, lookback_hours, now):
        return None
    tweet_id = str(getattr(tweet, "id", "") or "")
    body = clean_html(str(getattr(tweet, "rawContent", "") or ""))[:12000]
    url = str(getattr(tweet, "url", "") or "") or f"https://x.com/{handle}/status/{tweet_id}"
    return SignalItem(
        id=stable_id("x", tweet_id or body or url),
        source_type="x",
        source_name=f"@{handle}",
        title=body[:180],
        url=url,
        published_at=published,
        collected_at=collected_at,
        body=body,
        author=str(account.get("name") or handle),
        engagement={
            "likes": int(getattr(tweet, "likeCount", None) or 0),
            "reposts": int(getattr(tweet, "retweetCount", None) or 0),
            "replies": int(getattr(tweet, "replyCount", None) or 0),
            "quotes": int(getattr(tweet, "quoteCount", None) or 0),
        },
        metadata=_account_metadata(account, "twscrape"),
    )


def _collect_twitter_via_twscrape(
    config: dict[str, Any],
    accounts: list[dict[str, Any]],
    now: datetime,
) -> tuple[list[SignalItem], list[str], list[str]]:
    """Fetch each account's recent tweets through twscrape + TWITTER_COOKIES.

    Mirrors the ai-signal reference: a single twscrape account is registered
    from the cookie string, then each handle is pulled from the Latest search
    product via ``from:<handle>``. Requires ``twscrape`` (a project dependency)
    and a valid, non-expired cookie string. twscrape is async, so this helper
    drives one short-lived event loop and returns plain results.
    """
    cookie_values = _load_twitter_cookie_values()
    if not cookie_values:
        return [], ["TWITTER_COOKIES not set"], []
    lookback = int(config.get("lookback_hours", 72))
    same_day = bool(config.get("same_day", False))
    day_timezone = _zone(config.get("timezone"))
    search_limit = max(10, min(1000, int(config.get("search_limit_per_account", 300))))
    configured_max = config.get("max_items_per_account")
    max_per_user = max(1, min(100, int(configured_max))) if configured_max is not None else None
    default_min_engagement = int(config.get("min_engagement", 0))
    default_include_replies = bool(config.get("include_replies", False))
    db_path = str(project_root() / "data" / "twitter_accounts.db")
    wait_timeout = _twscrape_wait_timeout(config)

    async def _run() -> tuple[list[SignalItem], list[str], list[str]]:
        # Lazy import keeps this module importable when twscrape is not installed.
        from contextlib import aclosing

        from twscrape import API

        # twscrape's default wait_timeout=None intentionally waits for a
        # SearchTimeline bucket to reset instead of abandoning lower-priority
        # accounts. This wait is confined to the X collector; the CLI publishes
        # the other pipeline results before entering this phase.
        api = API(
            db_path,
            wait_timeout=wait_timeout,
            wait_interval=max(1.0, float(config.get("cooldown_poll_seconds", 5.0))),
        )
        # The upstream pool defaults to username ordering, which would reuse
        # feed_bot whenever it unlocks successfully. Least-recently-used order
        # makes multiple local cookie slots take turns even before a cooldown.
        api.pool._order_by = "last_used ASC, username"
        cookie_accounts: list[str] = []
        for index, cookies in enumerate(cookie_values, start=1):
            account_name = TWSCRAPE_ACCOUNT if index == 1 else f"{TWSCRAPE_ACCOUNT}_{index}"
            await api.pool.add_account_cookies(account_name, cookies)
            cookie_accounts.append(account_name)
        # Do not keep removed local cookie slots active in the persistent pool.
        for account in await api.pool.get_all():
            if account.username.startswith(TWSCRAPE_ACCOUNT) and account.username not in cookie_accounts:
                await api.pool.set_active(account.username, False)

        items: list[SignalItem] = []
        errors: list[str] = []
        notes: list[str] = []
        seen_ids: set[str] = set()
        ordered_accounts = sorted(
            enumerate(accounts),
            key=lambda pair: (
                0 if str(pair[1].get("section") or config.get("section") or "transaction_ideas") == "transaction_ideas" else 1,
                pair[0],
            ),
        )
        notes.append("X account priority: transaction_ideas before industry_changes")
        notes.append(f"X cooldown waiting enabled across {len(cookie_accounts)} cookie account(s)")
        if wait_timeout is not None:
            notes.append(f"X per-request cooldown wait capped at {wait_timeout:g} seconds")
        for _, raw_account in ordered_accounts:
            handle = str(raw_account["handle"]).lstrip("@")
            account_include_replies = bool(raw_account.get("include_replies", default_include_replies))
            try:
                tweets = []
                # Latest search is newest-first. Stop as soon as the first
                # prior-day post is encountered, avoiding needless pagination.
                async with aclosing(
                    api.search(f"from:{handle}", limit=search_limit, kv={"product": "Latest"})
                ) as stream:
                    async for tweet in stream:
                        tweets.append(tweet)
                        tweet_date = getattr(tweet, "date", None)
                        if same_day:
                            if _before_collection_day(tweet_date, now, day_timezone):
                                break
                        elif _before_lookback(tweet_date, now, lookback):
                            break
            except Exception as exc:
                errors.append(f"@{handle}: {exc}")
                continue
            kept: list[SignalItem] = []
            for tweet in tweets:
                item = parse_twscrape_tweet(
                    tweet,
                    raw_account,
                    now=now,
                    lookback_hours=lookback,
                    same_day=same_day,
                    day_timezone=day_timezone,
                )
                if item is None:
                    continue
                if str(getattr(tweet, "rawContent", "") or "").startswith("RT @"):
                    continue
                author = _tweet_author_handle(tweet)
                if author and author.casefold() != handle.casefold():
                    continue
                if not account_include_replies and _is_reply_tweet(tweet):
                    continue
                if _tweet_engagement_score(tweet) < default_min_engagement:
                    continue
                if item.id in seen_ids:
                    continue
                seen_ids.add(item.id)
                item.metadata["engagement_score"] = _tweet_engagement_score(tweet)
                kept.append(item)
            kept.sort(
                key=lambda it: sum(float(value) for value in it.engagement.values()),
                reverse=True,
            )
            if max_per_user is not None:
                kept = kept[:max_per_user]
            items.extend(kept)
            notes.append(f"@{handle}: {len(kept)} tweet(s) via twscrape")
        return items, errors, notes

    return asyncio.run(_run())


def collect_x(config: dict[str, Any], client: httpx.Client | None = None) -> PipelineResult:
    result = PipelineResult(pipeline="x")
    accounts = config.get("accounts") or []
    if not accounts:
        result.status = "not_configured"
        result.notes.append("No X accounts configured yet")
        return result
    normalized = [{"handle": raw} if isinstance(raw, str) else raw for raw in accounts]
    now = datetime.now(timezone.utc)
    lookback = int(config.get("lookback_hours", 72))
    same_day = bool(config.get("same_day", False))
    day_timezone = _zone(config.get("timezone"))
    configured_max = config.get("max_items_per_account")
    max_results = max(1, min(100, int(configured_max))) if configured_max is not None else 100
    cookie_values = _load_twitter_cookie_values()
    token = os.environ.get("X_BEARER_TOKEN", "")

    # Per account the preferred transport is: rss_url → TWITTER_COOKIES
    # (twscrape) → X_BEARER_TOKEN (official API). Accounts with none of these
    # are reported as needing credentials rather than silently dropped.
    ordered = sorted(
        enumerate(normalized),
        key=lambda pair: (
            0 if str(pair[1].get("section") or config.get("section") or "transaction_ideas") == "transaction_ideas" else 1,
            pair[0],
        ),
    )
    normalized = [account for _, account in ordered]
    rss_accounts = [a for a in normalized if a.get("rss_url")]
    cookie_accounts = [a for a in normalized if not a.get("rss_url") and cookie_values]
    oauth_accounts = [a for a in normalized if not a.get("rss_url") and not cookie_values and token]
    missing_accounts = [a for a in normalized if not a.get("rss_url") and not cookie_values and not token]

    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "idea-research/0.1"})
    try:
        for account in rss_accounts:
            handle = str(account["handle"]).lstrip("@")
            try:
                response = client.get(str(account["rss_url"]))
                response.raise_for_status()
                rss_items = parse_x_rss(
                    response.text,
                    account,
                    now=now,
                    lookback_hours=lookback,
                    same_day=same_day,
                    day_timezone=day_timezone,
                )
                result.items.extend(rss_items if configured_max is None else rss_items[:max_results])
            except Exception as exc:
                result.errors.append(f"@{handle} RSS: {exc}")

        if cookie_accounts:
            items, errors, notes = _collect_twitter_via_twscrape(config, cookie_accounts, now)
            result.items.extend(items)
            result.errors.extend(errors)
            result.notes.extend(notes)

        for account in oauth_accounts:
            handle = str(account["handle"]).lstrip("@")
            headers = {"Authorization": f"Bearer {token}"}
            try:
                user_response = client.get(
                    f"{API_BASE}/users/by/username/{handle}",
                    params={"user.fields": "name,username"},
                    headers=headers,
                )
                user_response.raise_for_status()
                user = user_response.json()["data"]
                start_time = (
                    _day_start(now, day_timezone) if same_day else now - timedelta(hours=lookback)
                ).isoformat().replace("+00:00", "Z")
                tweet_response = client.get(
                    f"{API_BASE}/users/{user['id']}/tweets",
                    params={
                        "max_results": max_results,
                        "start_time": start_time,
                        "exclude": "retweets,replies"
                        if not account.get("include_replies", config.get("include_replies"))
                        else "retweets",
                        "tweet.fields": "created_at,public_metrics,lang",
                    },
                    headers=headers,
                )
                tweet_response.raise_for_status()
                account = {**account, "name": account.get("name") or user.get("name") or handle}
                result.items.extend(
                    parse_x_tweets(
                        tweet_response.json(),
                        account,
                        now=now,
                        same_day=same_day,
                        day_timezone=day_timezone,
                    )
                )
            except Exception as exc:
                result.errors.append(f"@{handle}: {exc}")
        stored, skipped = enrich_linked_articles(result.items, client, config)
        if stored or skipped:
            result.notes.append(f"Linked articles: {stored} article(s) stored; {skipped} unavailable or skipped")
    finally:
        if owns_client:
            client.close()

    if missing_accounts:
        result.notes.append(
            f"{len(missing_accounts)} X account(s) await TWITTER_COOKIES, X_BEARER_TOKEN, or per-account rss_url"
        )
    if missing_accounts and len(missing_accounts) == len(normalized) and not result.items and not result.errors:
        result.status = "needs_credentials"
    elif result.errors and not result.items:
        result.status = "error"
    elif result.errors or missing_accounts:
        result.status = "partial"
    return result
