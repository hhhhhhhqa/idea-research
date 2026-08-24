#!/usr/bin/env python3
"""Build a first-pass U.S. Internet / AI-software candidate universe.

The Yahoo Finance screener is used only to build a broad candidate pool.  It
can filter by U.S. listing, exchange, market capitalisation and sector, but
does not reliably return an industry field in the screener response.  The
result therefore still includes hardware and semiconductors in Technology,
and telecom/media in Communication Services.  Those are intentionally left
for the next FMP-profile enrichment and user-editable industry rules step.

Examples:
  python stock_select.py
  python stock_select.py --show 50 --output data/stock_universe/yahoo_candidates.json
  python stock_select.py --min-market-cap 1000000000 --sector Technology
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yfinance as yf

from idea_research.config import load_dotenv


DEFAULT_SECTORS = ("Technology", "Communication Services")
US_MAJOR_EXCHANGES = ("NMS", "NYQ")  # Nasdaq Global Select + NYSE in Yahoo's screener codes.
FMP_PROFILE_URL = "https://financialmodelingprep.com/stable/profile"


def make_query(sector: str, min_market_cap: int) -> yf.EquityQuery:
    return yf.EquityQuery(
        "and",
        [
            yf.EquityQuery("eq", ["region", "us"]),
            yf.EquityQuery("is-in", ["exchange", *US_MAJOR_EXCHANGES]),
            yf.EquityQuery("gte", ["intradaymarketcap", min_market_cap]),
            yf.EquityQuery("eq", ["sector", sector]),
        ],
    )


def fetch_sector(
    sector: str,
    min_market_cap: int,
    page_size: int,
    max_pages: int,
) -> list[dict[str, Any]]:
    """Fetch every available page for one broad Yahoo sector."""

    query = make_query(sector, min_market_cap)
    rows: list[dict[str, Any]] = []
    offset = 0

    for page in range(max_pages):
        response = yf.screen(
            query,
            offset=offset,
            size=page_size,
            sortField="intradaymarketcap",
            sortAsc=False,
        )
        quotes = response.get("quotes", [])
        if not isinstance(quotes, list):
            raise RuntimeError(f"Yahoo returned an unexpected quote payload for {sector!r}")

        rows.extend(quote for quote in quotes if isinstance(quote, dict))
        total = response.get("total")
        if not quotes or len(quotes) < page_size:
            break
        if isinstance(total, int) and len(rows) >= total:
            break

        offset += len(quotes)
        # Keep this small local research utility polite to Yahoo's public endpoints.
        if page + 1 < max_pages:
            time.sleep(0.6)
    else:
        print(
            f"Warning: stopped {sector} after --max-pages={max_pages}; "
            "increase it if Yahoo has more matching results.",
            file=sys.stderr,
        )

    return rows


def normalise(
    sector: str,
    quote: dict[str, Any],
    min_market_cap: int,
) -> dict[str, Any] | None:
    symbol = quote.get("symbol")
    market_cap = quote.get("marketCap")
    if not isinstance(symbol, str) or not symbol:
        return None
    if not isinstance(market_cap, (int, float)) or market_cap < min_market_cap:
        return None

    return {
        "symbol": symbol,
        "company_name": quote.get("shortName") or quote.get("longName") or symbol,
        "market_cap": int(market_cap),
        "yahoo_sector": sector,
        "exchange": quote.get("fullExchangeName") or quote.get("exchange"),
        "currency": quote.get("currency"),
    }


def build_candidates(args: argparse.Namespace) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for sector in args.sector:
        print(f"Fetching {sector} ...", file=sys.stderr)
        for quote in fetch_sector(sector, args.min_market_cap, args.page_size, args.max_pages):
            # Yahoo's screener-side market-cap value can lag the value included
            # in its quote payload, so enforce the threshold again locally.
            item = normalise(sector, quote, args.min_market_cap)
            if item is not None:
                candidates[item["symbol"]] = item

    return sorted(candidates.values(), key=lambda item: item["market_cap"], reverse=True)


def utc_now() -> datetime:
    return datetime.now(UTC)


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    """Atomically persist a checkpoint so Ctrl-C or quota failures are safe."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def is_fresh(profile: dict[str, Any], ttl_days: int) -> bool:
    fetched_at = profile.get("fetched_at")
    if not isinstance(fetched_at, str):
        return False
    try:
        fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return fetched >= utc_now() - timedelta(days=ttl_days)


def apply_fmp_profile(candidate: dict[str, Any], profile: dict[str, Any] | None) -> None:
    """Expose FMP's detailed industry under the user-facing granular_sector key."""

    if profile is None:
        candidate.update(
            {
                "fmp_sector": None,
                "fmp_industry": None,
                "granular_sector": None,
                "fmp_market_cap": None,
                "fmp_profile_status": "pending",
                "fmp_profile_updated_at": None,
            }
        )
        return

    candidate.update(
        {
            "fmp_sector": profile.get("sector"),
            "fmp_industry": profile.get("industry"),
            # FMP industry is the granular classification. It is deliberately
            # not transformed into an opaque model label, so users can edit the
            # later SaaS/Internet inclusion rules directly.
            "granular_sector": profile.get("industry"),
            "fmp_market_cap": profile.get("market_cap"),
            "fmp_profile_status": profile.get("status", "ok"),
            "fmp_profile_updated_at": profile.get("fetched_at"),
        }
    )


def is_global_fmp_limit(response: httpx.Response) -> bool:
    text = response.text.lower()
    return response.status_code in {402, 429} or "limit reach" in text or "rate limit" in text


def enrich_with_fmp(candidates: list[dict[str, Any]], args: argparse.Namespace) -> tuple[int, int]:
    """Enrich new or stale tickers and checkpoint each completed FMP request.

    FMP's free tier is daily-limited. The cache means a later invocation resumes
    from the first missing or stale ticker rather than starting over.
    """

    load_dotenv()
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        raise RuntimeError("FMP_API_KEY is missing from .env")

    cache = read_json(args.fmp_cache, {"version": 1, "profiles": {}})
    profiles = cache.get("profiles") if isinstance(cache, dict) else None
    if not isinstance(profiles, dict):
        raise RuntimeError(f"Unexpected FMP cache structure in {args.fmp_cache}")

    requests_made = 0
    stopped_for_limit = False
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for candidate in candidates:
            symbol = candidate["symbol"]
            cached = profiles.get(symbol)
            if isinstance(cached, dict) and is_fresh(cached, args.profile_ttl_days):
                apply_fmp_profile(candidate, cached)
                continue

            if stopped_for_limit or requests_made >= args.fmp_max_requests:
                stopped_for_limit = True
                apply_fmp_profile(candidate, None)
                continue

            response = client.get(FMP_PROFILE_URL, params={"symbol": symbol, "apikey": api_key})
            requests_made += 1
            if response.status_code != 200:
                if is_global_fmp_limit(response):
                    stopped_for_limit = True
                    apply_fmp_profile(candidate, None)
                    continue
                # A symbol-specific miss should not block the remaining pool.
                print(f"FMP profile failed for {symbol}: HTTP {response.status_code}", file=sys.stderr)
                apply_fmp_profile(candidate, None)
                continue

            try:
                payload = response.json()
            except ValueError:
                print(f"FMP profile failed for {symbol}: non-JSON response", file=sys.stderr)
                apply_fmp_profile(candidate, None)
                continue

            if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
                # Some US-listed instruments have no FMP profile. Keep them
                # pending so a later data refresh can retry them.
                apply_fmp_profile(candidate, None)
                continue

            item = payload[0]
            profile = {
                "status": "ok",
                "fetched_at": utc_now().isoformat(),
                "sector": item.get("sector"),
                "industry": item.get("industry"),
                "market_cap": item.get("marketCap"),
            }
            profiles[symbol] = profile
            # The cache is the resume checkpoint: persist immediately after
            # every successful response, not only when the full run finishes.
            write_json(args.fmp_cache, {"version": 1, "profiles": profiles})
            apply_fmp_profile(candidate, profile)
            time.sleep(0.2)

    # Ensure cached profiles also appear on candidates skipped after an FMP cap.
    for candidate in candidates:
        if candidate.get("fmp_profile_status") != "ok":
            cached = profiles.get(candidate["symbol"])
            if isinstance(cached, dict):
                apply_fmp_profile(candidate, cached)

    pending = sum(item.get("fmp_profile_status") != "ok" for item in candidates)
    if stopped_for_limit:
        print(
            "FMP request limit reached for this run; rerun later and cached profiles will be reused.",
            file=sys.stderr,
        )
    return requests_made, pending


def previous_symbols(output_path: Path) -> set[str]:
    previous = read_json(output_path, {})
    if not isinstance(previous, dict):
        return set()
    stocks = previous.get("stocks", [])
    if not isinstance(stocks, list):
        return set()
    return {
        stock["symbol"]
        for stock in stocks
        if isinstance(stock, dict) and isinstance(stock.get("symbol"), str)
    }


def run_once(args: argparse.Namespace) -> int:
    before = previous_symbols(args.output)
    rows = build_candidates(args)
    current = {item["symbol"] for item in rows}

    if args.skip_fmp:
        for item in rows:
            apply_fmp_profile(item, None)
        requests_made, pending = 0, len(rows)
    else:
        requests_made, pending = enrich_with_fmp(rows, args)

    added = sorted(current - before)
    removed = sorted(before - current)
    document = {
        "updated_at": utc_now().isoformat(),
        "selection": {
            "markets": ["NYSE", "Nasdaq"],
            "minimum_market_cap_usd": args.min_market_cap,
            "yahoo_sectors": args.sector,
            "country_filter": None,
        },
        "summary": {
            "current_candidates": len(rows),
            "fmp_profiles_complete": len(rows) - pending,
            "fmp_profiles_pending": pending,
            "fmp_requests_this_run": requests_made,
            "added_symbols": added,
            "removed_symbols": removed,
        },
        "stocks": rows,
    }
    write_json(args.output, document)

    print(f"\n{len(rows)} preliminary candidates (market cap >= ${args.min_market_cap:,.0f})")
    print(f"Added: {len(added)} | Removed: {len(removed)} | FMP complete: {len(rows) - pending}/{len(rows)}")
    print("Ticker  Market cap ($bn)  Granular sector                         Company")
    for item in rows[: args.show]:
        granular = item.get("granular_sector") or "FMP pending"
        print(
            f"{item['symbol']:<7} {item['market_cap'] / 1_000_000_000:>15,.1f}  "
            f"{granular:<40.40} {item['company_name']}"
        )
    print(f"\nSaved stock-pool state to {args.output}")
    if pending:
        print(f"FMP cache: {args.fmp_cache} ({pending} ticker(s) pending; rerun later to resume)")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-market-cap", type=int, default=500_000_000)
    parser.add_argument("--sector", action="append", choices=DEFAULT_SECTORS)
    parser.add_argument("--page-size", type=int, default=250)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--show", type=int, default=30, help="Number of rows to print.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/stock_universe/stock_pool.json"),
        help="Persistent stock-pool state (used to detect additions/removals).",
    )
    parser.add_argument(
        "--fmp-cache",
        type=Path,
        default=Path("data/stock_universe/fmp_profile_cache.json"),
        help="Per-ticker FMP cache and resume checkpoint.",
    )
    parser.add_argument(
        "--fmp-max-requests",
        type=int,
        default=225,
        help="Maximum new/expired FMP profile requests per run (free-tier safe).",
    )
    parser.add_argument(
        "--profile-ttl-days",
        type=int,
        default=7,
        help="Refresh a cached FMP profile after this many days.",
    )
    parser.add_argument("--skip-fmp", action="store_true", help="Only refresh the Yahoo candidate set.")
    parser.add_argument(
        "--recheck-hours",
        type=float,
        help="Keep running locally and repeat this check after the given interval.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.sector:
        args.sector = list(DEFAULT_SECTORS)
    if (
        args.min_market_cap < 0
        or args.page_size < 1
        or args.max_pages < 1
        or args.show < 0
        or args.fmp_max_requests < 0
        or args.profile_ttl_days < 0
        or (args.recheck_hours is not None and args.recheck_hours <= 0)
    ):
        raise SystemExit("Numeric arguments must be non-negative; page size and max pages must be positive.")

    while True:
        run_once(args)
        if args.recheck_hours is None:
            return 0
        print(f"Waiting {args.recheck_hours:g} hour(s) before the next local recheck ...", file=sys.stderr)
        time.sleep(args.recheck_hours * 3600)


if __name__ == "__main__":
    raise SystemExit(main())
