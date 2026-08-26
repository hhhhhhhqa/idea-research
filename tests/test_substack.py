from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from idea_research.pipelines.substack import parse_substack_feed


FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test Letter</title><item>
<guid>post-1</guid><title>Inference costs fall again</title>
<link>https://example.com/p/inference</link>
<pubDate>Sun, 23 Aug 2026 12:00:00 GMT</pubDate>
<description><![CDATA[<p>Tokens are getting cheaper.</p>]]></description>
</item></channel></rss>"""


def test_parse_substack_feed_normalizes_item():
    items = parse_substack_feed(
        FEED,
        {"name": "Test Letter", "url": "https://example.com"},
        now=datetime(2026, 8, 24, tzinfo=timezone.utc),
        lookback_hours=48,
    )
    assert len(items) == 1
    assert items[0].source_type == "substack"
    assert items[0].body == "Tokens are getting cheaper."
    assert items[0].url.endswith("/p/inference")
    assert items[0].metadata["section"] == "transaction_ideas"


def test_parse_generic_rss_feed_keeps_its_own_source_type():
    items = parse_substack_feed(
        FEED,
        {"name": "Software Stack Investing", "url": "https://example.com", "source_type": "rss"},
        now=datetime(2026, 8, 24, tzinfo=timezone.utc),
        lookback_hours=48,
    )
    assert items[0].source_type == "rss"
    assert items[0].id.startswith("rss:")


def test_parse_substack_feed_same_calendar_day_uses_configured_timezone():
    feed = FEED.replace("Sun, 23 Aug 2026 12:00:00 GMT", "Mon, 24 Aug 2026 15:30:00 GMT")
    now = datetime(2026, 8, 24, 16, tzinfo=timezone.utc)
    assert parse_substack_feed(
        feed,
        {"name": "Test Letter", "url": "https://example.com"},
        now=now,
        same_day=True,
        day_timezone=ZoneInfo("Asia/Hong_Kong"),
    ) == []

    feed = FEED.replace("Sun, 23 Aug 2026 12:00:00 GMT", "Mon, 24 Aug 2026 16:30:00 GMT")
    items = parse_substack_feed(
        feed,
        {"name": "Test Letter", "url": "https://example.com"},
        now=now,
        same_day=True,
        day_timezone=ZoneInfo("Asia/Hong_Kong"),
    )
    assert len(items) == 1
