from datetime import datetime, timedelta, timezone

from idea_research.models import PipelineResult, SignalItem
from idea_research.report import prepare_report_context
from idea_research.storage import build_feed, save_feed


def test_report_enriches_theme_and_watchlist(tmp_path):
    now = datetime(2026, 8, 24, 8, tzinfo=timezone.utc)
    item = SignalItem(
        id="substack:1",
        source_type="substack",
        source_name="Test",
        title="Blackwell inference demand accelerates",
        url="https://example.com/1",
        published_at=now.isoformat(),
        collected_at=now.isoformat(),
        body="NVIDIA customers report more GPU orders.",
    )
    stale_market_close = SignalItem(
        id="price:nvda",
        source_type="price",
        source_name="Yahoo Finance",
        title="NVDA close 100",
        url="https://finance.yahoo.com/quote/NVDA",
        published_at=(now - timedelta(days=3)).isoformat(),
        collected_at=now.isoformat(),
        symbols=["NVDA"],
        metadata={"ticker": "NVDA", "close": 100},
    )
    feed = build_feed([PipelineResult(pipeline="substack", items=[item, stale_market_close])])
    feed["generated_at"] = now.isoformat()
    save_feed(tmp_path, feed)
    profile = {
        "themes": {"AI 基础设施": ["inference", "gpu"]},
        "watchlist": [{"ticker": "NVDA", "name": "NVIDIA", "aliases": ["Blackwell"]}],
    }
    context = prepare_report_context(tmp_path, profile, "daily", now=now)
    assert context["items"][0]["matched_symbols"] == ["NVDA"]
    assert context["market_context"][0]["metadata"]["ticker"] == "NVDA"
    assert "signal_score" not in context["items"][0]


def test_report_rolls_up_wsb_posts_into_ticker_heat(tmp_path):
    now = datetime(2026, 8, 24, 8, tzinfo=timezone.utc)

    def wsb_item(item_id, title, symbols, rank, score=0, comments=0):
        return SignalItem(
            id=item_id,
            source_type="reddit",
            source_name="r/wallstreetbets",
            title=title,
            url=f"https://reddit.com/{item_id}",
            published_at=now.isoformat(),
            collected_at=now.isoformat(),
            symbols=symbols,
            engagement={"score": score, "comments": comments},
            metadata={"listing": "hot", "feed_rank": rank, "transport": "oauth_json"},
        )

    posts = [
        wsb_item("reddit:1", "NVDA earnings", ["NVDA"], 1, 100, 40),
        wsb_item("reddit:2", "NVDA options", ["NVDA"], 4, 20, 8),
        wsb_item("reddit:3", "Tesla delivery", ["TSLA"], 2, 60, 20),
    ]
    feed = build_feed([PipelineResult(pipeline="reddit", items=posts)])
    feed["generated_at"] = now.isoformat()
    save_feed(tmp_path, feed)
    profile = {
            "reddit_discussions": {
            "enabled": True,
            "subreddit": "wallstreetbets",
            "max_symbols": 10,
            "rollup_only": True,
        }
    }
    context = prepare_report_context(tmp_path, profile, "daily", now=now)
    assert context["reddit_discussions"]["top_tickers"][0]["ticker"] == "NVDA"
    assert context["reddit_discussions"]["top_tickers"][0]["mention_count"] == 2
    assert context["reddit_discussions"]["top_hot_posts"][0]["tickers"] == ["NVDA"]
    assert "heat_score" not in context["reddit_discussions"]["top_tickers"][0]
    assert context["stats"]["wsb_posts"] == 3
    assert context["items"] == []


def test_report_keeps_alpha_vantage_movers_out_of_watchlist_prices(tmp_path):
    now = datetime(2026, 8, 24, 21, tzinfo=timezone.utc)
    mover = SignalItem(
        id="price:alpha:gainers",
        source_type="price",
        source_name="Alpha Vantage",
        title="US close top gainers",
        url="https://example.com/movers",
        published_at=now.isoformat(),
        collected_at=now.isoformat(),
        metadata={
            "market_mover_type": "gainers",
            "market_session": "regular_close_end_of_day",
            "last_updated": "2026-08-24 16:15:00 US/Eastern",
            "records": [{"ticker": "NET", "price": "120", "change_percentage": "9%"}],
        },
    )
    feed = build_feed([PipelineResult(pipeline="prices", items=[mover])])
    feed["generated_at"] = now.isoformat()
    save_feed(tmp_path, feed)
    context = prepare_report_context(tmp_path, {}, "daily", now=now)
    assert context["market_context"] == []
    assert context["market_movers"]["latest"]["top_gainers"][0]["ticker"] == "NET"
