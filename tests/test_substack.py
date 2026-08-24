from datetime import datetime, timezone

from ai_signal.pipelines.substack import parse_substack_feed


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
