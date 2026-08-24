from datetime import datetime, timezone

from ai_signal.pipelines.x import parse_x_tweets


def test_parse_x_official_api_payload():
    payload = {
        "data": [
            {
                "id": "123",
                "text": "Inference cost dropped 50%.",
                "created_at": "2026-08-24T01:00:00Z",
                "public_metrics": {
                    "like_count": 10,
                    "retweet_count": 3,
                    "reply_count": 2,
                    "quote_count": 1,
                },
            }
        ]
    }
    items = parse_x_tweets(
        payload,
        {"handle": "builder", "name": "AI Builder"},
        now=datetime(2026, 8, 24, 2, tzinfo=timezone.utc),
    )
    assert items[0].url == "https://x.com/builder/status/123"
    assert items[0].engagement["reposts"] == 3
    assert items[0].metadata["transport"] == "official_api"
