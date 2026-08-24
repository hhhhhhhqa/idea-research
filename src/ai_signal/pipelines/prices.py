from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import yfinance as yf

from ..models import PipelineResult, SignalItem
from .common import stable_id


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


def collect_prices(config: dict[str, Any]) -> PipelineResult:
    result = PipelineResult(pipeline="prices")
    tickers = config.get("tickers") or []
    if not tickers:
        result.status = "not_configured"
        result.notes.append("No tickers configured")
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
    if result.errors and not result.items:
        result.status = "error"
    elif result.errors:
        result.status = "partial"
    return result
