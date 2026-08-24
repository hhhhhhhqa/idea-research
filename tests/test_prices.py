from datetime import datetime, timezone

import pandas as pd

from ai_signal.pipelines.prices import price_item


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
