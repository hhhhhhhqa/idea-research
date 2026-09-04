from idea_research.pipelines.prices import calculate_daily_movers, merge_rolling_prices


def test_rolling_prices_keep_only_three_trading_days_and_pool_symbols():
    existing = {
        "prices": {
            "2026-08-20": {"AAA": 10},
            "2026-08-21": {"AAA": 11, "OLD": 99},
        }
    }
    rolling = merge_rolling_prices(
        existing,
        {
            "2026-08-24": {"AAA": 12, "BBB": 20},
            "2026-08-25": {"AAA": 10, "BBB": 22},
        },
        allowed_tickers={"AAA", "BBB"},
        retention_days=3,
    )
    assert rolling["dates"] == ["2026-08-21", "2026-08-24", "2026-08-25"]
    assert "OLD" not in rolling["prices"]["2026-08-21"]


def test_daily_movers_compare_latest_close_with_previous_close():
    rolling = {
        "prices": {
            "2026-08-24": {"AAA": 100, "BBB": 200, "CCC": 50},
            "2026-08-25": {"AAA": 110, "BBB": 180, "CCC": 50},
        }
    }
    price_date, gainers, losers = calculate_daily_movers(rolling, top_n=10)
    assert price_date == "2026-08-25"
    assert gainers[0]["ticker"] == "AAA"
    assert gainers[0]["change_percentage"] == 10.0
    assert losers[0]["ticker"] == "BBB"
    assert losers[0]["change_percentage"] == -10.0


def test_rolling_prices_ignores_incomplete_latest_yahoo_date():
    rolling = merge_rolling_prices(
        {
            "prices": {
                "2026-09-01": {"AAA": 100, "BBB": 200, "CCC": 50},
                "2026-09-02": {"AAA": 110, "BBB": 180, "CCC": 55},
                "2026-09-03": {"AAA": 111},
            }
        },
        {},
        allowed_tickers={"AAA", "BBB", "CCC"},
        retention_days=3,
        minimum_date_coverage_ratio=0.8,
    )

    assert rolling["dates"] == ["2026-09-01", "2026-09-02"]
    assert "2026-09-03" not in rolling["prices"]
