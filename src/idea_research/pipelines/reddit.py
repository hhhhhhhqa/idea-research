from __future__ import annotations

import os
import re
import html
import mimetypes
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import feedparser
import httpx

from ..config import project_root
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

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}


def _unique_image_urls(urls: list[Any]) -> list[str]:
    result: list[str] = []
    for raw in urls:
        url = html.unescape(str(raw or "")).strip()
        if not url.startswith(("http://", "https://")) or url in result:
            continue
        result.append(url)
    return result


def _json_image_urls(post: dict[str, Any]) -> list[str]:
    """Extract Reddit preview/gallery/direct-image URLs without downloading them."""
    urls: list[Any] = []
    preview = post.get("preview") or {}
    for image in preview.get("images") or []:
        source = image.get("source") if isinstance(image, dict) else None
        if isinstance(source, dict):
            urls.append(source.get("url"))
    media_metadata = post.get("media_metadata") or {}
    if isinstance(media_metadata, dict):
        for media in media_metadata.values():
            if not isinstance(media, dict):
                continue
            source = media.get("s") or {}
            if isinstance(source, dict):
                urls.extend([source.get("u"), source.get("gif")])
    direct = post.get("url_overridden_by_dest") or post.get("url") or ""
    direct_text = str(direct)
    parsed = urlparse(direct_text)
    if parsed.scheme in {"http", "https"} and (
        str(post.get("post_hint") or "") == "image"
        or Path(parsed.path).suffix.casefold() in IMAGE_SUFFIXES
        or parsed.hostname in {"i.redd.it", "preview.redd.it"}
    ):
        urls.append(direct_text)
    return _unique_image_urls(urls)


def _rss_image_urls(entry: Any, raw_html: str) -> list[str]:
    urls: list[Any] = re.findall(r"<img[^>]+src=[\"']([^\"']+)[\"']", raw_html or "", flags=re.IGNORECASE)
    for link in entry.get("links") or []:
        if not isinstance(link, dict):
            continue
        link_type = str(link.get("type") or "").casefold()
        if link.get("rel") in {"enclosure", "image"} or link_type.startswith("image/"):
            urls.append(link.get("href"))
    media_content = entry.get("media_content") or entry.get("media_thumbnail") or []
    for media in media_content:
        if isinstance(media, dict):
            urls.append(media.get("url"))
    return _unique_image_urls(urls)


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
    flair: str | None = None,
    flairs: list[str] | None = None,
    known_tickers: list[str] | None = None,
    ticker_aliases: dict[str, list[str]] | None = None,
) -> list[SignalItem]:
    now = now or datetime.now(timezone.utc)
    collected_at = now.isoformat()
    items: list[SignalItem] = []
    children = payload.get("data", {}).get("children", [])
    for feed_rank, child in enumerate(children, start=1):
        post = child.get("data", {})
        actual_flair = str(post.get("link_flair_text") or "").strip()
        requested_flairs = [str(value).strip() for value in (flairs or []) if str(value).strip()]
        if flair and flair.strip():
            requested_flairs.append(flair.strip())
        if requested_flairs and actual_flair and not any(
            actual_flair.casefold() == value.casefold() for value in requested_flairs
        ):
            continue
        created = datetime.fromtimestamp(float(post.get("created_utc") or now.timestamp()), tz=timezone.utc).isoformat()
        if lookback_hours is not None and not within_lookback(created, lookback_hours, now):
            continue
        permalink = str(post.get("permalink") or "")
        url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
        reddit_id = str(post.get("name") or post.get("id") or url)
        title = str(post.get("title") or "Untitled")
        body = clean_html(post.get("selftext"))[:12000]
        image_urls = _json_image_urls(post)
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
                    "section": "transaction_ideas",
                    "external_url": post.get("url_overridden_by_dest") or post.get("url") or "",
                    "flair": post.get("link_flair_text") or "",
                    "is_self": bool(post.get("is_self")),
                    "transport": "oauth_json",
                    "listing": listing,
                    "feed_rank": feed_rank,
                    "requested_flair": flair or "",
                    "requested_flairs": requested_flairs,
                    "image_urls": image_urls,
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
    flair: str | None = None,
    flairs: list[str] | None = None,
    sort: str | None = None,
    time_filter: str | None = None,
    known_tickers: list[str] | None = None,
    ticker_aliases: dict[str, list[str]] | None = None,
) -> list[SignalItem]:
    now = now or datetime.now(timezone.utc)
    collected_at = now.isoformat()
    parsed = feedparser.parse(xml)
    items: list[SignalItem] = []
    for feed_rank, entry in enumerate(parsed.entries, start=1):
        entry_flair = ""
        tags = entry.get("tags") or []
        tag_terms: list[str] = []
        for tag in tags:
            if isinstance(tag, dict):
                term = str(tag.get("term") or "").strip()
                if term:
                    tag_terms.append(term)
        # Reddit RSS can expose both the subreddit name and the post flair as
        # Atom categories. Prefer the requested flair wherever it appears;
        # never treat the subreddit category itself as a mismatching flair.
        requested_flairs = [str(value).strip() for value in (flairs or []) if str(value).strip()]
        if flair and flair.strip():
            requested_flairs.append(flair.strip())
        if requested_flairs:
            entry_flair = next(
                (
                    term
                    for term in tag_terms
                    if any(term.casefold() == value.casefold() for value in requested_flairs)
                ),
                next(
                    (
                        term
                        for term in tag_terms
                        if term.casefold() != str(subreddit or "").removeprefix("r/").casefold()
                    ),
                    "",
                ),
            )
        elif tag_terms:
            entry_flair = tag_terms[0]
        if requested_flairs and entry_flair and not any(
            entry_flair.casefold() == value.casefold() for value in requested_flairs
        ):
            continue
        published = iso_datetime(entry.get("published") or entry.get("updated"), collected_at)
        if lookback_hours is not None and not within_lookback(published, lookback_hours, now):
            continue
        link = str(entry.get("link") or "")
        matched = re.search(r"/r/([^/]+)/", link, re.IGNORECASE)
        resolved_subreddit = matched.group(1) if matched else (subreddit or "unknown")
        title = clean_html(entry.get("title")) or "Untitled"
        raw_html = entry.get("content", [{}])[0].get("value") if entry.get("content") else entry.get("summary")
        body = clean_html(raw_html)[:12000]
        image_urls = _rss_image_urls(entry, str(raw_html or ""))
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
                metadata={
                    "section": "transaction_ideas",
                    "transport": "rss",
                    "listing": listing,
                    "flair": entry_flair or (flair or ""),
                    "requested_flair": flair or "",
                    "requested_flairs": requested_flairs,
                    "sort": sort or "",
                    "time_filter": time_filter or "",
                    "feed_rank": feed_rank,
                    "image_urls": image_urls,
                },
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


def _reddit_listing_request(
    entry: dict[str, Any], limit: int
) -> tuple[str, dict[str, Any], list[str]]:
    """Return endpoint, query params and requested flair for a configured listing."""
    listing = str(entry.get("listing", "new")).lower()
    configured_flairs = [str(value).strip() for value in (entry.get("flairs") or []) if str(value).strip()]
    flair = str(entry.get("flair") or "").strip()
    if flair and flair not in configured_flairs:
        configured_flairs.append(flair)
    if listing not in {"dd", "thesis"}:
        return listing, {"limit": limit, "raw_json": 1}, configured_flairs
    # Reddit has no /dd or /thesis listing. These are subreddit searches
    # constrained to one or more flairs, sorted by Reddit's daily top ranking.
    query_flairs = configured_flairs or (["DD"] if listing == "dd" else ["Thesis", "Short Thesis"])
    query = " OR ".join(
        f'flair:"{value.replace(chr(34), "")}"' for value in query_flairs
    )
    params = {
        "q": query,
        "restrict_sr": "1",
        "sort": str(entry.get("sort") or "top"),
        "t": str(entry.get("time_filter") or "day"),
        "limit": limit,
        "raw_json": 1,
    }
    return "search", params, query_flairs


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


def _image_extension(url: str, content_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.casefold()
    if suffix in IMAGE_SUFFIXES:
        return suffix
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) if content_type else None
    return guessed if guessed in IMAGE_SUFFIXES else ".jpg"


def _write_image(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def _download_reddit_images(
    items: list[SignalItem],
    client: httpx.Client,
    config: dict[str, Any],
) -> tuple[list[str], set[str]]:
    """Download post images into the current feed's media directory."""
    if not bool(config.get("download_images", True)):
        return [], set()
    media_root = Path(config.get("media_dir", "data/media/reddit"))
    if not media_root.is_absolute():
        media_root = project_root() / media_root
    max_images = max(1, int(config.get("max_images_per_post", 4)))
    max_bytes = max(100_000, int(config.get("max_image_bytes", 5_000_000)))
    errors: list[str] = []
    keep_paths: set[str] = set()
    for item in items:
        raw_urls = item.metadata.get("image_urls") or []
        images: list[dict[str, Any]] = []
        for index, url in enumerate(_unique_image_urls(raw_urls)[:max_images], start=1):
            try:
                response = client.get(url)
                response.raise_for_status()
                content = response.content
                if len(content) > max_bytes:
                    raise ValueError(f"image is {len(content)} bytes, limit is {max_bytes}")
                content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].casefold()
                if content_type and not content_type.startswith("image/"):
                    raise ValueError(f"content-type is {content_type}")
                filename = f"reddit-image-{item.id.replace(':', '-')}-{index}{_image_extension(url, content_type)}"
                path = media_root / filename
                _write_image(path, content)
                try:
                    relative_path = str(path.relative_to(project_root()))
                except ValueError:
                    relative_path = str(path)
                keep_paths.add(str(path.resolve()))
                images.append(
                    {
                        "url": url,
                        "path": relative_path,
                        "mime_type": content_type or mimetypes.guess_type(str(path))[0] or "image/jpeg",
                        "bytes": len(content),
                    }
                )
            except Exception as exc:
                errors.append(f"{item.id}: image {index}={exc}")
        item.metadata["images"] = images

    if media_root.exists():
        for path in media_root.glob("reddit-image-*"):
            if path.is_file() and str(path.resolve()) not in keep_paths:
                try:
                    path.unlink()
                except OSError as exc:
                    errors.append(f"{path}: could not prune stale image: {exc}")
    return errors, keep_paths


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
            per_entry_entries: list[dict[str, Any]] = []
            for entry in normalized:
                listing = str(entry.get("listing", "new")).lower()
                # DD and thesis are flair-filtered searches, not subreddit
                # listings; the combined RSS endpoint cannot express either.
                if listing in {"dd", "thesis"}:
                    per_entry_entries.append(entry)
                    continue
                grouped.setdefault(listing, []).append(entry)
            failed_entries: list[dict[str, Any]] = list(per_entry_entries)
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
            normalized = failed_entries
        for index, entry in enumerate(normalized):
            name = str(entry["name"]).removeprefix("r/")
            limit = int(entry.get("limit", config.get("max_items_per_subreddit", 25)))
            configured_lookback = entry.get("lookback_hours", config.get("lookback_hours", 72))
            lookback = int(configured_lookback) if configured_lookback is not None else None
            listing = str(entry.get("listing", "new")).lower()
            if listing not in {"new", "hot", "top", "dd", "thesis"}:
                result.errors.append(f"r/{name}: unsupported listing={listing}")
                continue
            endpoint_listing, request_params, requested_flairs = _reddit_listing_request(entry, limit)
            # A DD search is already constrained to Reddit's daily top window;
            # an additional publication lookback would incorrectly discard
            # posts that remain in that ranking.
            effective_lookback = None if listing in {"dd", "thesis"} else lookback
            if index and not token:
                time.sleep(float(config.get("anonymous_request_delay_seconds", 2.0)))
            try:
                if token:
                    response = client.get(
                        f"https://oauth.reddit.com/r/{name}/{endpoint_listing}.json",
                        params=request_params,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    response.raise_for_status()
                    result.items.extend(
                        parse_reddit_json(
                            response.json(),
                            name,
                            lookback_hours=effective_lookback,
                            listing=listing,
                            flairs=requested_flairs,
                            known_tickers=config.get("known_tickers") or [],
                            ticker_aliases=config.get("ticker_aliases") or {},
                        )[:limit]
                    )
                else:
                    rss_params = dict(request_params)
                    rss_params.pop("raw_json", None)
                    response = client.get(
                        f"https://www.reddit.com/r/{name}/{endpoint_listing}.rss",
                        params=rss_params,
                    )
                    response.raise_for_status()
                    result.items.extend(
                        parse_reddit_rss(
                            response.text,
                            name,
                            lookback_hours=effective_lookback,
                            listing=listing,
                            flairs=requested_flairs,
                            sort=str(entry.get("sort") or "") or None,
                            time_filter=str(entry.get("time_filter") or "") or None,
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
                        f"https://www.reddit.com/r/{name}/{endpoint_listing}.json",
                        params=request_params,
                    )
                    response.raise_for_status()
                    result.items.extend(
                        parse_reddit_json(
                            response.json(),
                            name,
                            lookback_hours=effective_lookback,
                            listing=listing,
                            flairs=requested_flairs,
                            known_tickers=config.get("known_tickers") or [],
                            ticker_aliases=config.get("ticker_aliases") or {},
                        )[:limit]
                    )
                    result.notes.append(f"r/{name}: public {listing} JSON fallback used")
                except Exception as fallback_exc:
                    result.errors.append(f"r/{name}: RSS={primary_exc}; JSON={fallback_exc}")
        if result.items:
            image_errors, _ = _download_reddit_images(result.items, client, config)
            image_count = sum(len(item.metadata.get("images") or []) for item in result.items)
            result.notes.append(f"Downloaded {image_count} Reddit image(s) into the current feed media directory")
            result.errors.extend(image_errors[:20])
            if len(image_errors) > 20:
                result.notes.append(f"{len(image_errors) - 20} additional Reddit image errors omitted")
    finally:
        if owns_client:
            client.close()
    if result.errors and not result.items:
        result.status = "error"
    elif result.errors:
        result.status = "partial"
    return result
