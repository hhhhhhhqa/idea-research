from __future__ import annotations

import json
import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yfinance as yf

from ..config import project_root
from ..models import PipelineResult, SignalItem
from .common import stable_id


YAHOO_GAINERS_URL = "https://finance.yahoo.com/markets/stocks/gainers/"
YAHOO_LOSERS_URL = "https://finance.yahoo.com/markets/stocks/losers/"
NEW_YORK = ZoneInfo("America/New_York")


def _resolve_project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root() / path


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _close_columns(frame: Any, tickers: list[str]) -> dict[str, Any]:
    """Return ticker -> close Series for yfinance's single/multi-ticker frames."""
    columns = getattr(frame, "columns", None)
    if columns is None:
        return {}
    try:
        if hasattr(columns, "nlevels") and columns.nlevels > 1:
            levels = [set(str(value) for value in columns.get_level_values(index)) for index in range(columns.nlevels)]
            if "Close" in levels[0]:
                close_frame = frame["Close"]
            elif "Close" in levels[-1]:
                close_frame = frame.xs("Close", axis=1, level=columns.nlevels - 1)
            else:
                return {}
            if hasattr(close_frame, "columns"):
                return {ticker: close_frame[ticker] for ticker in tickers if ticker in close_frame.columns}
        if "Close" in columns:
            close = frame["Close"]
            if hasattr(close, "columns"):
                return {ticker: close[ticker] for ticker in tickers if ticker in close.columns}
            if len(tickers) == 1:
                return {tickers[0]: close}
    except (KeyError, IndexError, TypeError):
        return {}
    return {}


def download_close_observations(
    tickers: list[str],
    *,
    history_days: int = 5,
    chunk_size: int = 50,
) -> tuple[dict[str, dict[str, float]], list[str]]:
    """Download unadjusted daily closes in chunks from Yahoo Finance."""
    observations: dict[str, dict[str, float]] = {}
    errors: list[str] = []
    unique_tickers = list(dict.fromkeys(tickers))
    for start in range(0, len(unique_tickers), chunk_size):
        chunk = unique_tickers[start : start + chunk_size]
        try:
            frame = yf.download(
                tickers=chunk,
                period=f"{max(history_days, 3)}d",
                interval="1d",
                auto_adjust=False,
                actions=False,
                group_by="column",
                threads=False,
                progress=False,
            )
            close_columns = _close_columns(frame, chunk)
            for ticker, series in close_columns.items():
                for raw_date, raw_close in series.dropna().items():
                    try:
                        close = float(raw_close)
                    except (TypeError, ValueError):
                        continue
                    observations.setdefault(_date_key(raw_date), {})[ticker] = close
            missing = [ticker for ticker in chunk if ticker not in close_columns]
            errors.extend(f"{ticker}: Yahoo Finance returned no close" for ticker in missing)
        except Exception as exc:
            errors.append(f"{','.join(chunk)}: {exc}")
    return observations, errors


def _date_key(value: Any) -> str:
    if hasattr(value, "date"):
        value = value.date()
    return str(value)[:10]


def merge_rolling_prices(
    existing: dict[str, Any],
    observations: dict[str, dict[str, float]],
    *,
    allowed_tickers: set[str],
    retention_days: int = 3,
    minimum_date_coverage_ratio: float = 0.0,
) -> dict[str, Any]:
    """Merge closes and retain the latest N sufficiently complete trading dates."""
    prices = existing.get("prices") if isinstance(existing, dict) else {}
    prices = dict(prices) if isinstance(prices, dict) else {}
    for date, values in observations.items():
        if not isinstance(values, dict):
            continue
        prices[date] = {
            ticker: float(close)
            for ticker, close in values.items()
            if ticker in allowed_tickers
        }
    prices = {
        date: {
            ticker: close
            for ticker, close in values.items()
            if ticker in allowed_tickers
        }
        for date, values in prices.items()
        if isinstance(values, dict)
    }
    coverage_ratio = min(1.0, max(0.0, float(minimum_date_coverage_ratio)))
    if allowed_tickers and coverage_ratio:
        minimum_count = max(1, math.ceil(len(allowed_tickers) * coverage_ratio))
        prices = {date: values for date, values in prices.items() if len(values) >= minimum_count}
    dates = sorted(prices)[-max(1, retention_days) :]
    prices = {date: prices[date] for date in dates}
    return {
        "schema_version": "1.0",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "retention_trading_days": max(1, retention_days),
        "dates": dates,
        "prices": prices,
    }


def calculate_daily_movers(
    rolling: dict[str, Any],
    *,
    top_n: int = 10,
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Compare the latest stored close with the preceding stored close."""
    prices = rolling.get("prices") if isinstance(rolling, dict) else {}
    if not isinstance(prices, dict):
        return None, [], []
    dates = sorted(date for date in prices if isinstance(prices[date], dict))
    if len(dates) < 2:
        return dates[-1] if dates else None, [], []
    current_date, previous_date = dates[-1], dates[-2]
    current = prices[current_date]
    previous = prices[previous_date]
    movers: list[dict[str, Any]] = []
    for ticker, raw_close in current.items():
        if ticker not in previous:
            continue
        try:
            close = float(raw_close)
            previous_close = float(previous[ticker])
        except (TypeError, ValueError):
            continue
        if previous_close == 0:
            continue
        change_pct = round((close / previous_close - 1) * 100, 4)
        movers.append(
            {
                "ticker": ticker,
                "price_date": current_date,
                "close": round(close, 6),
                "previous_close": round(previous_close, 6),
                "change_amount": round(close - previous_close, 6),
                "change_percentage": change_pct,
                "url": f"https://finance.yahoo.com/quote/{ticker}",
            }
        )
    gainers = sorted(movers, key=lambda item: (-item["change_percentage"], item["ticker"]))[: max(1, top_n)]
    losers = sorted(movers, key=lambda item: (item["change_percentage"], item["ticker"]))[: max(1, top_n)]
    return current_date, gainers, losers


def _close_timestamp(price_date: str) -> str:
    close = datetime.fromisoformat(f"{price_date}T16:00:00").replace(tzinfo=NEW_YORK)
    return close.astimezone(timezone.utc).isoformat()


def _mover_item(
    kind: str,
    records: list[dict[str, Any]],
    *,
    price_date: str,
    collected_at: str,
    rolling_path: Path,
    retention_days: int,
) -> SignalItem:
    label = "涨幅前十" if kind == "gainers" else "跌幅前十"
    lines = [
        f"{index}. {record['ticker']} close {record['close']}; "
        f"previous {record['previous_close']}; change {record['change_percentage']}%"
        for index, record in enumerate(records, start=1)
    ]
    return SignalItem(
        id=stable_id("price", f"yfinance:{kind}:{price_date}"),
        source_type="price",
        source_name="Yahoo Finance",
        title=f"SaaS / software / Internet {label} {price_date}",
        url=YAHOO_GAINERS_URL if kind == "gainers" else YAHOO_LOSERS_URL,
        published_at=_close_timestamp(price_date),
        collected_at=collected_at,
        body="\n".join(lines),
        symbols=[record["ticker"] for record in records],
        metadata={
            "section": "transaction_ideas",
            "market_mover_type": kind,
            "market_session": "regular_close_end_of_day",
            "price_date": price_date,
            "comparison": "current close vs previous stored trading-day close",
            "retention_trading_days": retention_days,
            "rolling_prices_path": str(rolling_path),
            "records": records,
        },
    )


def collect_prices(config: dict[str, Any], client: Any | None = None) -> PipelineResult:
    """Collect SaaS-pool closes from Yahoo and publish top-ten daily movers."""
    del client  # Kept for pipeline interface compatibility; yfinance is the transport.
    result = PipelineResult(pipeline="prices")
    pool_path = _resolve_project_path(config.get("saas_pool_path", "data/stock_universe/saas_pool.json"))
    rolling_path = _resolve_project_path(config.get("rolling_prices_path", "data/prices/rolling_prices.json"))
    pool = _load_json(pool_path, {})
    stocks = pool.get("stocks") if isinstance(pool, dict) else None
    if not isinstance(stocks, list):
        result.status = "error"
        result.errors.append(f"SaaS stock pool not found or invalid: {pool_path}")
        return result
    tickers = list(
        dict.fromkeys(
            str(stock.get("symbol")).upper()
            for stock in stocks
            if isinstance(stock, dict) and stock.get("symbol") and stock.get("fmp_profile_status") == "ok"
        )
    )
    if not tickers:
        result.status = "error"
        result.errors.append("SaaS stock pool contains no FMP-complete tickers")
        return result

    history_days = max(3, int(config.get("history_days", 5)))
    retention_days = max(1, int(config.get("retention_trading_days", 3)))
    minimum_date_coverage_ratio = min(
        1.0, max(0.0, float(config.get("minimum_date_coverage_ratio", 0.8)))
    )
    top_n = max(1, int(config.get("top_n", 10)))
    observations, errors = download_close_observations(
        tickers,
        history_days=history_days,
        chunk_size=max(1, int(config.get("download_chunk_size", 50))),
    )
    if not observations:
        result.status = "error"
        result.errors.extend(errors or ["Yahoo Finance returned no daily closes"])
        return result

    existing = _load_json(rolling_path, {})
    rolling = merge_rolling_prices(
        existing,
        observations,
        allowed_tickers=set(tickers),
        retention_days=retention_days,
        minimum_date_coverage_ratio=minimum_date_coverage_ratio,
    )
    _atomic_json(rolling_path, rolling)
    price_date, gainers, losers = calculate_daily_movers(rolling, top_n=top_n)
    collected_at = datetime.now(timezone.utc).isoformat()
    if price_date and gainers:
        result.items.append(
            _mover_item(
                "gainers",
                gainers,
                price_date=price_date,
                collected_at=collected_at,
                rolling_path=rolling_path,
                retention_days=retention_days,
            )
        )
    if price_date and losers:
        result.items.append(
            _mover_item(
                "losers", losers, price_date=price_date, collected_at=collected_at, rolling_path=rolling_path,
                retention_days=retention_days,
            )
        )
    result.notes.append(
        f"Yahoo Finance EOD closes collected for {len(tickers)} SaaS/software/Internet tickers; "
        f"rolling state keeps {len(rolling['dates'])} trading day(s) at {rolling_path}"
    )
    latest_observed_date = max(observations)
    latest_observed_count = len(observations[latest_observed_date])
    if price_date and latest_observed_date > price_date:
        result.status = "partial"
        result.notes.append(
            f"Ignored incomplete Yahoo EOD date {latest_observed_date}: "
            f"{latest_observed_count}/{len(tickers)} ticker closes available; "
            f"latest sufficiently complete date is {price_date}."
        )
    today_et = datetime.now(NEW_YORK).date().isoformat()
    if price_date and price_date < today_et:
        result.notes.append(
            f"Latest available Yahoo EOD date is {price_date}; run after the US close to collect {today_et}."
        )
    if price_date and not gainers:
        result.notes.append("Only one stored trading day is available; daily movers will appear after the next close.")
    if errors:
        result.status = "partial"
        result.errors.extend(errors[:20])
        if len(errors) > 20:
            result.notes.append(f"{len(errors) - 20} additional ticker errors omitted from pipeline status")
    return result
