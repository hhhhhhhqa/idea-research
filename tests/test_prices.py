from datetime import datetime, timezone

import pandas as pd

from idea_research.pipelines.prices import alpha_vantage_mover_items, price_item


def test_price_item_calculates_returns():
    index = pd.date_range("2026-07-01", periods=30, freq="B", tz="UTC")
    frame = pd.DataFrame(
        {
            "Open": range(100, 130),
            "High": range(102, 132),
            "Low": range(99, 129),
            "Close": range(101, 131),
            "Volume": [1000] * 30,
        },
        index=index,
    )
    item = price_item("TEST", frame, now=datetime(2026, 8, 24, tzinfo=timezone.utc))
    assert item.symbols == ["TEST"]
    assert item.metadata["return_1d_pct"] == round((130 / 129 - 1) * 100, 4)
    assert item.metadata["return_21d_pct"] is not None


def test_alpha_vantage_movers_keep_raw_close_lists():
    payload = {
        "last_updated": "2026-08-24 16:15:00 US/Eastern",
        "top_gainers": [
            {"ticker": "NET", "price": "120.00", "change_amount": "10.00", "change_percentage": "9.09%", "volume": "100"}
        ],
        "top_losers": [
            {"ticker": "SNOW", "price": "150.00", "change_amount": "-12.00", "change_percentage": "-7.41%", "volume": "200"}
        ],
    }
    items = alpha_vantage_mover_items(payload, now=datetime(2026, 8, 25, tzinfo=timezone.utc))
    assert [item.metadata["market_mover_type"] for item in items] == ["gainers", "losers"]
    assert items[0].metadata["records"][0]["ticker"] == "NET"
    assert items[1].published_at == "2026-08-24T20:15:00+00:00"
