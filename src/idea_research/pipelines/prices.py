from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..models import PipelineResult, SignalItem
from .common import stable_id


ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"


def _alpha_timestamp(value: str, fallback: datetime) -> str:
    try:
        parsed = datetime.strptime(value.removesuffix(" US/Eastern"), "%Y-%m-%d %H:%M:%S")
        return parsed.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return fallback.isoformat()


def alpha_vantage_mover_items(payload: dict[str, Any], now: datetime | None = None) -> list[SignalItem]:
    """Map Alpha Vantage's end-of-day US mover lists into raw feed records."""
    now = now or datetime.now(timezone.utc)
    if payload.get("Error Message") or payload.get("Information") or payload.get("Note"):
        raise ValueError(payload.get("Error Message") or payload.get("Information") or payload.get("Note"))
    last_updated = str(payload.get("last_updated") or "")
    published_at = _alpha_timestamp(last_updated, now)
    items: list[SignalItem] = []
    for kind, field, label in (
        ("gainers", "top_gainers", "US close top gainers"),
        ("losers", "top_losers", "US close top losers"),
    ):
        records = list(payload.get(field) or [])[:20]
        if not records:
            continue
        normalized = [
            {
                "ticker": str(entry.get("ticker") or ""),
                "price": str(entry.get("price") or ""),
                "change_amount": str(entry.get("change_amount") or ""),
                "change_percentage": str(entry.get("change_percentage") or ""),
                "volume": str(entry.get("volume") or ""),
            }
            for entry in records
        ]
        body = "\n".join(
            f"{entry['ticker']}: close {entry['price']}; change {entry['change_percentage']}; volume {entry['volume']}"
            for entry in normalized
        )
        items.append(
            SignalItem(
                id=stable_id("price", f"alpha_vantage:{kind}:{last_updated or published_at[:10]}"),
                source_type="price",
                source_name="Alpha Vantage",
                title=label,
                url="https://www.alphavantage.co/documentation/#top-gainers-losers",
                published_at=published_at,
                collected_at=now.isoformat(),
                body=body,
                symbols=[entry["ticker"] for entry in normalized if entry["ticker"]],
                metadata={
                    "market_mover_type": kind,
                    "market_session": "regular_close_end_of_day",
                    "last_updated": last_updated,
                    "records": normalized,
                },
            )
        )
    if not items:
        raise ValueError("TOP_GAINERS_LOSERS returned no top_gainers or top_losers records")
    return items


def collect_prices(config: dict[str, Any], client: httpx.Client | None = None) -> PipelineResult:
    result = PipelineResult(pipeline="prices")
    movers_config = config.get("market_movers") or {}
    movers_enabled = bool(movers_config.get("enabled", False))
    if not movers_enabled:
        result.status = "not_configured"
        result.notes.append("No market-movers source configured")
        return result
    api_key = os.environ.get(str(movers_config.get("api_key_env", "ALPHAVANTAGE_API_KEY")), "")
    if not api_key:
        result.errors.append("Alpha Vantage: ALPHAVANTAGE_API_KEY is not set")
    else:
        owns_client = client is None
        alpha_client = client or httpx.Client(timeout=30, follow_redirects=True)
        try:
            response = alpha_client.get(
                str(movers_config.get("endpoint", ALPHA_VANTAGE_URL)),
                params={"function": "TOP_GAINERS_LOSERS", "apikey": api_key},
            )
            response.raise_for_status()
            result.items.extend(alpha_vantage_mover_items(response.json()))
            result.notes.append("Alpha Vantage end-of-day US gainers/losers collected")
        except Exception as exc:
            result.errors.append(f"Alpha Vantage: {exc}")
        finally:
            if owns_client:
                alpha_client.close()
    if result.errors and not result.items:
        result.status = "error"
    elif result.errors:
        result.status = "partial"
    return result
