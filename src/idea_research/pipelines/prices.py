from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yfinance as yf

from ..models import PipelineResult, SignalItem
from .common import stable_id


ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"


def _return_pct(values: list[float], sessions: int) -> float | None:
    if len(values) <= sessions or not values[-sessions - 1]:
        return None
    return round((values[-1] / values[-sessions - 1] - 1) * 100, 4)


def price_item(ticker: str, history: Any, name: str = "", now: datetime | None = None) -> SignalItem:
    now = now or datetime.now(timezone.utc)
    frame = history.dropna(subset=["Close"])
    if frame.empty:
        raise ValueError("empty price history")
    closes = [float(value) for value in frame["Close"].tolist()]
    latest = frame.iloc[-1]
    latest_index = frame.index[-1]
    if hasattr(latest_index, "to_pydatetime"):
        latest_dt = latest_index.to_pydatetime()
    else:
        latest_dt = latest_index
    if latest_dt.tzinfo is None:
        latest_dt = latest_dt.replace(tzinfo=timezone.utc)
    published = latest_dt.astimezone(timezone.utc).isoformat()
    metrics = {
        "currency": "",
        "close": round(float(latest["Close"]), 6),
        "open": round(float(latest["Open"]), 6),
        "high": round(float(latest["High"]), 6),
        "low": round(float(latest["Low"]), 6),
        "volume": int(latest["Volume"]),
        "return_1d_pct": _return_pct(closes, 1),
        "return_5d_pct": _return_pct(closes, 5),
        "return_21d_pct": _return_pct(closes, 21),
        "distance_from_252d_high_pct": round((closes[-1] / max(closes) - 1) * 100, 4),
    }
    title = f"{ticker} close {metrics['close']}"
    body = (
        f"1D {metrics['return_1d_pct']}%; 5D {metrics['return_5d_pct']}%; "
        f"21D {metrics['return_21d_pct']}%; vs 252D high {metrics['distance_from_252d_high_pct']}%."
    )
    return SignalItem(
        id=stable_id("price", f"{ticker}:{published[:10]}"),
        source_type="price",
        source_name="Yahoo Finance",
        title=title,
        url=f"https://finance.yahoo.com/quote/{ticker}",
        published_at=published,
        collected_at=now.isoformat(),
        body=body,
        symbols=[ticker],
        metadata={"ticker": ticker, "company": name, **metrics},
    )


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
    tickers = config.get("tickers") or []
    movers_config = config.get("market_movers") or {}
    movers_enabled = bool(movers_config.get("enabled", False))
    if not tickers and not movers_enabled:
        result.status = "not_configured"
        result.notes.append("No price tickers or market-movers source configured")
        return result
    for raw in tickers:
        entry = {"ticker": raw} if isinstance(raw, str) else raw
        ticker = str(entry["ticker"]).upper()
        try:
            history = yf.Ticker(ticker).history(
                period=str(config.get("history_period", "1y")),
                interval="1d",
                auto_adjust=False,
                actions=False,
                timeout=30,
            )
            result.items.append(price_item(ticker, history, str(entry.get("name") or "")))
        except Exception as exc:
            result.errors.append(f"{ticker}: {exc}")

    if movers_enabled:
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
