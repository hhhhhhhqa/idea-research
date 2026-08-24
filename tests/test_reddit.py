from datetime import datetime, timezone

from ai_signal.pipelines.reddit import parse_reddit_json, parse_reddit_rss


def test_parse_reddit_json_keeps_engagement_and_canonical_url():
    payload = {
        "data": {
            "children": [
                {
                    "data": {
                        "name": "t3_abc",
                        "title": "New open model benchmark",
                        "selftext": "Results and methodology",
                        "author": "researcher",
                        "created_utc": 1787486400,
                        "permalink": "/r/LocalLLaMA/comments/abc/test/",
                        "score": 42,
                        "num_comments": 9,
                        "upvote_ratio": 0.95,
                    }
                }
            ]
        }
    }
    items = parse_reddit_json(
        payload,
        "LocalLLaMA",
        now=datetime.fromtimestamp(1787486400, tz=timezone.utc),
    )
    assert len(items) == 1
    assert items[0].engagement["score"] == 42
    assert items[0].url.startswith("https://www.reddit.com/r/LocalLLaMA")


def test_combined_rss_infers_subreddit_from_link():
    xml = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>t3_xyz</id><title>Agent release</title>
    <link href="https://www.reddit.com/r/artificial/comments/xyz/agent_release/" />
    <updated>2026-08-24T01:00:00Z</updated><author><name>u/tester</name></author>
    <content type="html">&lt;p&gt;Details&lt;/p&gt;</content>
    </entry></feed>"""
    items = parse_reddit_rss(
        xml,
        None,
        now=datetime(2026, 8, 24, 2, tzinfo=timezone.utc),
    )
    assert items[0].source_name == "r/artificial"
