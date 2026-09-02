from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import respx

from idea_research.pipelines.substack import collect_substack, parse_substack_archive, parse_substack_feed


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


def test_parse_substack_archive_normalizes_public_post():
    payload = [
        {
            "id": 123,
            "title": "SaaS margin thesis",
            "slug": "saas-margin-thesis",
            "post_date": "2026-08-24T12:00:00Z",
            "canonical_url": "https://example.com/p/inference",
            "body_html": "<p>Margins can expand.</p>",
            "publishedBylines": [{"name": "Analyst"}],
        }
    ]
    items = parse_substack_archive(
        payload,
        {"name": "Example", "url": "https://example.substack.com"},
        now=datetime(2026, 8, 24, 13, tzinfo=timezone.utc),
        lookback_hours=24,
    )
    assert len(items) == 1
    assert items[0].author == "Analyst"
    assert items[0].body == "Margins can expand."
    assert items[0].metadata["transport"] == "substack_archive_api"
    matching_feed = FEED.replace("<guid>post-1</guid>", "<guid>https://example.com/p/inference</guid>")
    assert items[0].id == parse_substack_feed(
        matching_feed,
        {"name": "Example", "url": "https://example.com"},
        now=datetime(2026, 8, 24, tzinfo=timezone.utc),
        lookback_hours=48,
    )[0].id


@respx.mock
def test_collect_substack_falls_back_to_public_archive_api():
    feed_url = "https://example.substack.com/feed"
    archive_url = "https://example.substack.com/api/v1/archive?sort=new&search=&offset=0&limit=20"
    respx.get(feed_url).mock(return_value=httpx.Response(403))
    respx.get(archive_url).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 123,
                    "title": "Fallback post",
                    "post_date": datetime.now(timezone.utc).isoformat(),
                    "canonical_url": "https://example.substack.com/p/fallback-post",
                    "body_html": "<p>Fallback body.</p>",
                }
            ],
        )
    )
    with httpx.Client() as client:
        result = collect_substack(
            {
                "lookback_hours": 24,
                "publications": [
                    {
                        "name": "Example",
                        "url": "https://example.substack.com",
                        "feed_url": feed_url,
                    }
                ],
            },
            client,
        )
    assert result.status == "ok"
    assert [item.title for item in result.items] == ["Fallback post"]
    assert "archive API fallback used" in result.notes[0]
