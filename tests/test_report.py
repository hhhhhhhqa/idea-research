from datetime import datetime, timedelta, timezone

from idea_research.models import PipelineResult, SignalItem
from idea_research.report import prepare_report_context
from idea_research.storage import build_feed, save_feed


def test_report_keeps_source_declared_symbols_without_user_symbol_enrichment(tmp_path):
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
        symbols=["NVDA"],
    )
    feed = build_feed([PipelineResult(pipeline="substack", items=[item])])
    feed["generated_at"] = now.isoformat()
    save_feed(tmp_path, feed)
    profile = {
        "themes": {"AI 基础设施": ["inference", "gpu"]},
    }
    context = prepare_report_context(tmp_path, profile, "daily", now=now)
    assert context["items"][0]["matched_symbols"] == ["NVDA"]
    assert "signal_score" not in context["items"][0]


def test_report_adds_stock_mentions_from_published_pool(tmp_path):
    now = datetime(2026, 8, 24, 8, tzinfo=timezone.utc)
    stock_dir = tmp_path / "stock_universe"
    stock_dir.mkdir()
    (stock_dir / "stock_pool.json").write_text(
        '{"stocks": [{"symbol": "MSFT", "company_name": "Microsoft Corporation"}]}\n',
        encoding="utf-8",
    )
    item = SignalItem(
        id="x:1",
        source_type="x",
        source_name="@researcher",
        title="Microsoft expands Copilot contracts",
        url="https://x.com/researcher/status/1",
        published_at=now.isoformat(),
        collected_at=now.isoformat(),
        body="Microsoft disclosed stronger enterprise demand.",
    )
    feed = build_feed([PipelineResult(pipeline="x", items=[item])])
    feed["generated_at"] = now.isoformat()
    save_feed(tmp_path, feed)

    context = prepare_report_context(tmp_path, {}, "daily", now=now)

    assert context["items"][0]["stock_mentions"] == [
        {"ticker": "MSFT", "company_name": "Microsoft Corporation", "match_type": "company_name"}
    ]
    assert context["items"][0]["matched_symbols"] == ["MSFT"]


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
        wsb_item("reddit:2", "NVDA options", ["NVDA"], 2, 20, 8),
        wsb_item("reddit:3", "Tesla delivery", ["TSLA"], 3, 60, 20),
        wsb_item("reddit:4", "Old rank", ["OLD"], 4, 1, 1),
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
    assert [post["hot_rank"] for post in context["reddit_discussions"]["top_hot_posts"]] == [1, 2, 3]
    assert [post["display_id"] for post in context["wsb_posts"]] == ["R1", "R2", "R3"]
    assert "heat_score" not in context["reddit_discussions"]["top_tickers"][0]
    assert context["stats"]["wsb_posts"] == 3
    assert context["items"] == []


def test_report_exposes_alpha_vantage_movers_without_individual_price_records(tmp_path):
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
    assert context["market_movers"]["latest"]["top_gainers"][0]["ticker"] == "NET"
    assert "market_context" not in context


def test_report_keeps_every_item_in_current_feed_for_subscriber_relevance_check(tmp_path):
    now = datetime(2026, 8, 24, 21, tzinfo=timezone.utc)
    old_item = SignalItem(
        id="substack:old",
        source_type="substack",
        source_name="Old",
        title="old item",
        url="https://example.com/old",
        published_at=now.isoformat(),
        collected_at=now.isoformat(),
    )
    current_item = SignalItem(
        id="substack:current",
        source_type="substack",
        source_name="Current",
        title="unrelated-to-keywords item",
        url="https://example.com/current",
        published_at=(now - timedelta(days=30)).isoformat(),
        collected_at=now.isoformat(),
    )
    save_feed(tmp_path, build_feed([PipelineResult(pipeline="substack", items=[old_item])]))
    save_feed(tmp_path, build_feed([PipelineResult(pipeline="substack", items=[current_item])]))

    context = prepare_report_context(
        tmp_path,
        {
            "enabled_sources": ["x"],
            "include_keywords": ["must-not-match"],
            "exclude_keywords": ["unrelated"],
        },
        "daily",
        now=now,
    )

    assert [item["id"] for item in context["items"]] == ["substack:current"]
