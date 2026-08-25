import json
from datetime import datetime, timedelta, timezone

from idea_research.models import PipelineResult, SignalItem
from idea_research.delivery import mark_delivered
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
        wsb_item("reddit:4", "AMD discussion", ["AMD"], 4, 5, 2),
        wsb_item("reddit:5", "Microsoft discussion", ["MSFT"], 5, 4, 1),
        wsb_item("reddit:6", "Old rank", ["OLD"], 6, 1, 1),
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
    assert [post["hot_rank"] for post in context["reddit_discussions"]["top_hot_posts"]] == [1, 2, 3, 4, 5]
    assert [post["display_id"] for post in context["wsb_posts"]] == ["R1", "R2", "R3", "R4", "R5"]
    assert "heat_score" not in context["reddit_discussions"]["top_tickers"][0]
    assert context["stats"]["wsb_posts"] == 5
    assert context["items"] == []


def test_report_exposes_yfinance_movers_without_individual_price_records(tmp_path):
    now = datetime(2026, 8, 24, 21, tzinfo=timezone.utc)
    mover = SignalItem(
        id="price:yfinance:gainers",
        source_type="price",
        source_name="Yahoo Finance",
        title="SaaS / software / Internet 涨幅前十 2026-08-25",
        url="https://finance.yahoo.com/markets/stocks/gainers/",
        published_at=now.isoformat(),
        collected_at=now.isoformat(),
        metadata={
            "market_mover_type": "gainers",
            "market_session": "regular_close_end_of_day",
            "price_date": "2026-08-25",
            "records": [{"ticker": "NET", "close": 120, "change_percentage": 9.0}],
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


def test_report_deduplicates_only_newsletters_and_x_after_delivery(tmp_path):
    now = datetime(2026, 8, 24, 21, tzinfo=timezone.utc)
    newsletter = SignalItem(
        id="substack:one",
        source_type="substack",
        source_name="Research",
        title="NVDA software demand",
        url="https://example.com/n1",
        published_at=now.isoformat(),
        collected_at=now.isoformat(),
        symbols=["NVDA"],
    )
    rss = SignalItem(
        id="rss:two",
        source_type="rss",
        source_name="Research RSS",
        title="MSFT cloud growth",
        url="https://example.com/n2",
        published_at=(now - timedelta(minutes=1)).isoformat(),
        collected_at=now.isoformat(),
        symbols=["MSFT"],
    )
    x_post = SignalItem(
        id="x:three",
        source_type="x",
        source_name="@researcher",
        title="AI capex",
        url="https://x.com/researcher/status/3",
        published_at=(now - timedelta(minutes=2)).isoformat(),
        collected_at=now.isoformat(),
        symbols=["AMD"],
    )
    reddit = SignalItem(
        id="reddit:four",
        source_type="reddit",
        source_name="r/wallstreetbets",
        title="NVDA options",
        url="https://reddit.com/four",
        published_at=now.isoformat(),
        collected_at=now.isoformat(),
        symbols=["NVDA"],
        metadata={"listing": "hot", "feed_rank": 1, "transport": "rss"},
    )
    price = SignalItem(
        id="price:yfinance:gainers",
        source_type="price",
        source_name="Yahoo Finance",
        title="movers",
        url="https://finance.yahoo.com/markets/stocks/gainers/",
        published_at=now.isoformat(),
        collected_at=now.isoformat(),
        metadata={"market_mover_type": "gainers", "records": [{"ticker": "NVDA"}]},
    )
    save_feed(tmp_path, build_feed([PipelineResult(pipeline="mixed", items=[newsletter, rss, x_post, reddit, price])]))
    first = prepare_report_context(tmp_path, {}, "daily", now=now, seen_path=tmp_path / "seen.json")
    assert {item["id"] for item in first["items"]} == {"substack:one", "rss:two", "x:three"}
    assert first["delivery_mark"]["counts"] == {"newsletters": 2, "x": 1}
    assert [item["display_id"] for item in first["items"] if item["source_type"] in {"substack", "rss"}] == ["N1", "N2"]
    # The mark file is normally emitted by save_report_context; create the same
    # file here to keep this unit test focused on state behavior.
    (tmp_path / "delivery-mark.json").write_text(
        json.dumps(first["delivery_mark"]), encoding="utf-8"
    )
    mark_delivered(tmp_path / "delivery-mark.json", ["N1", "X1"], seen_path=tmp_path / "seen.json", now=now)
    second = prepare_report_context(tmp_path, {}, "daily", now=now, seen_path=tmp_path / "seen.json")
    assert {item["id"] for item in second["items"]} == {"rss:two"}
    assert second["stats"]["wsb_posts"] == 1
    assert second["market_movers"]["latest"] is not None


def test_include_seen_regenerates_newsletter_and_x_context(tmp_path):
    now = datetime(2026, 8, 24, 21, tzinfo=timezone.utc)
    item = SignalItem(
        id="x:seen",
        source_type="x",
        source_name="@researcher",
        title="NVDA",
        url="https://x.com/researcher/status/seen",
        published_at=now.isoformat(),
        collected_at=now.isoformat(),
        symbols=["NVDA"],
    )
    save_feed(tmp_path, build_feed([PipelineResult(pipeline="x", items=[item])]))
    seen_path = tmp_path / "seen.json"
    first = prepare_report_context(tmp_path, {}, "daily", now=now, seen_path=seen_path)
    (tmp_path / "delivery-mark.json").write_text(json.dumps(first["delivery_mark"]), encoding="utf-8")
    mark_delivered(tmp_path / "delivery-mark.json", ["X1"], seen_path=seen_path, now=now)
    assert prepare_report_context(tmp_path, {}, "daily", now=now, seen_path=seen_path)["items"] == []
    restored = prepare_report_context(tmp_path, {}, "daily", now=now, seen_path=seen_path, include_seen=True)
    assert [item["id"] for item in restored["items"]] == ["x:seen"]
