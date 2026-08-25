from datetime import datetime, timezone

from idea_research.pipelines.prices import alpha_vantage_mover_items


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
