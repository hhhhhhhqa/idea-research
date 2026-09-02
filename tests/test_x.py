from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from idea_research.models import SignalItem
from idea_research.pipelines.x import (
    _is_reply_tweet,
    _tweet_engagement_score,
    collect_x,
    enrich_linked_articles,
    extract_article_content,
    extract_link_urls,
    parse_twscrape_tweet,
    parse_x_tweets,
)


def _tweet(**kwargs):
    defaults = dict(
        id=123,
        rawContent="Inference cost dropped 50%. Investors should watch $NVDA.",
        date=datetime(2026, 8, 24, 1, tzinfo=timezone.utc),
        likeCount=10,
        retweetCount=3,
        replyCount=2,
        quoteCount=1,
        url="https://x.com/builder/status/123",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


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
    assert items[0].metadata["section"] == "transaction_ideas"
    assert items[0].metadata["transport"] == "official_api"


def test_parse_twscrape_tweet():
    account = {"handle": "builder", "name": "AI Builder"}
    item = parse_twscrape_tweet(
        _tweet(),
        account,
        now=datetime(2026, 8, 24, 2, tzinfo=timezone.utc),
    )
    assert item is not None
    assert item.source_type == "x"
    assert item.source_name == "@builder"
    assert item.url == "https://x.com/builder/status/123"
    assert item.engagement["likes"] == 10
    assert item.engagement["reposts"] == 3
    assert item.engagement["replies"] == 2
    assert item.engagement["quotes"] == 1
    assert item.metadata["transport"] == "twscrape"
    assert "Inference cost dropped 50%" in item.title


def test_parse_twscrape_tweet_url_fallback():
    item = parse_twscrape_tweet(
        _tweet(url=""),
        {"handle": "builder"},
        now=datetime(2026, 8, 24, 2, tzinfo=timezone.utc),
    )
    assert item is not None
    assert item.url == "https://x.com/builder/status/123"


def test_parse_twscrape_tweet_outside_lookback():
    old = _tweet(date=datetime(2026, 8, 1, tzinfo=timezone.utc))
    item = parse_twscrape_tweet(
        old,
        {"handle": "builder"},
        now=datetime(2026, 8, 24, tzinfo=timezone.utc),
        lookback_hours=72,
    )
    assert item is None


def test_parse_twscrape_tweet_same_calendar_day_uses_configured_timezone():
    # 16:00 UTC is midnight in Hong Kong; the previous 15:30 UTC post belongs
    # to the prior Hong Kong calendar day and must not enter today's feed.
    now = datetime(2026, 8, 24, 16, tzinfo=timezone.utc)
    old = _tweet(date=datetime(2026, 8, 24, 15, 30, tzinfo=timezone.utc))
    current = _tweet(date=datetime(2026, 8, 24, 16, 5, tzinfo=timezone.utc))
    assert parse_twscrape_tweet(old, {"handle": "builder"}, now=now, same_day=True, day_timezone=ZoneInfo("Asia/Hong_Kong")) is None
    assert parse_twscrape_tweet(current, {"handle": "builder"}, now=now, same_day=True, day_timezone=ZoneInfo("Asia/Hong_Kong")) is not None


def test_twscrape_engagement_score_weights_reposts():
    # likes + 2*reposts + replies = 10 + 6 + 2
    assert _tweet_engagement_score(_tweet()) == 18


def test_is_reply_tweet():
    assert _is_reply_tweet(_tweet(inReplyToTweetId=99)) is True
    assert _is_reply_tweet(_tweet(rawContent="@someone hi there")) is True
    assert _is_reply_tweet(_tweet(rawContent="just a thought")) is False


def test_collect_x_not_configured(monkeypatch):
    monkeypatch.delenv("TWITTER_COOKIES", raising=False)
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    result = collect_x({"accounts": []})
    assert result.status == "not_configured"


def test_load_twitter_cookies_falls_back_to_local_ignored_file(tmp_path, monkeypatch):
    import idea_research.pipelines.x as xmod

    monkeypatch.delenv("TWITTER_COOKIES", raising=False)
    (tmp_path / "credentials").mkdir()
    (tmp_path / "credentials" / "x_twitter_cookies.txt").write_text(
        "# comment\nauth_token=abc; ct0=def\n", encoding="utf-8"
    )
    monkeypatch.setattr(xmod, "project_root", lambda: tmp_path)
    assert xmod._load_twitter_cookies() == "auth_token=abc; ct0=def"


def test_load_twitter_cookie_values_supports_numbered_env_accounts(monkeypatch):
    import idea_research.pipelines.x as xmod

    for key in ("TWITTER_COOKIES", "TWITTER_COOKIES_2", "TWITTER_COOKIES_3"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TWITTER_COOKIES", "auth_token=one; ct0=one")
    monkeypatch.setenv("TWITTER_COOKIES_2", "auth_token=two; ct0=two")
    assert xmod._load_twitter_cookie_values() == [
        "auth_token=one; ct0=one",
        "auth_token=two; ct0=two",
    ]


def test_twscrape_wait_timeout_can_be_bounded_for_hosted_jobs(monkeypatch):
    import idea_research.pipelines.x as xmod

    monkeypatch.setenv("TWSCRAPE_WAIT_TIMEOUT_SECONDS", "300")
    assert xmod._twscrape_wait_timeout({"cooldown_wait_timeout_seconds": 60}) == 300.0
    monkeypatch.delenv("TWSCRAPE_WAIT_TIMEOUT_SECONDS")
    assert xmod._twscrape_wait_timeout({"cooldown_wait_timeout_seconds": 60}) == 60.0
    assert xmod._twscrape_wait_timeout({}) is None


def test_collect_x_needs_credentials(monkeypatch):
    monkeypatch.delenv("TWITTER_COOKIES", raising=False)
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    result = collect_x({"accounts": [{"handle": "karpathy"}]})
    assert result.status == "needs_credentials"


def test_collect_x_routes_cookie_accounts(monkeypatch):
    import idea_research.pipelines.x as xmod

    monkeypatch.setenv("TWITTER_COOKIES", "auth_token=a; ct0=b")
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    fake = SignalItem(
        id="x:fake",
        source_type="x",
        source_name="@karpathy",
        title="fake",
        url="https://x.com/karpathy/status/1",
        published_at="2026-08-24T00:00:00+00:00",
        collected_at="2026-08-24T00:00:00+00:00",
    )
    called: dict = {}

    def fake_collect(config, accounts, now):
        called["accounts"] = accounts
        return [fake], [], ["via twscrape"]

    monkeypatch.setattr(xmod, "_collect_twitter_via_twscrape", fake_collect)
    result = collect_x({"accounts": [{"handle": "karpathy", "name": "Andrej Karpathy"}]})
    assert result.status == "ok"
    assert result.items == [fake]
    assert called["accounts"] == [{"handle": "karpathy", "name": "Andrej Karpathy"}]


def test_collect_x_orders_transaction_ideas_before_industry_changes(monkeypatch):
    import idea_research.pipelines.x as xmod

    monkeypatch.setenv("TWITTER_COOKIES", "auth_token=a; ct0=b")
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    called: dict = {}

    def fake_collect(config, accounts, now):
        called["accounts"] = accounts
        return [], [], []

    monkeypatch.setattr(xmod, "_collect_twitter_via_twscrape", fake_collect)
    result = collect_x(
        {
            "accounts": [
                {"handle": "researcher", "section": "industry_changes"},
                {"handle": "investor", "section": "transaction_ideas"},
            ]
        }
    )
    assert result.status == "ok"
    assert [account["handle"] for account in called["accounts"]] == ["investor", "researcher"]


def test_link_extraction_and_html_article_text():
    assert extract_link_urls("Read https://t.co/abc and https://example.com/story.") == [
        "https://t.co/abc",
        "https://example.com/story",
    ]
    title, body = extract_article_content(
        "<html><head><title>Example</title></head><body><nav>Ignore</nav>"
        "<article><p>This is a sufficiently long first paragraph for the reader.</p>"
        "<p>This is a sufficiently long second paragraph for the reader.</p></article></body></html>",
        1000,
    )
    assert title == "Example"
    assert "first paragraph" in body


def test_linked_article_enrichment_attaches_text(monkeypatch):
    import idea_research.pipelines.x as xmod

    item = SignalItem(
        id="x:link",
        source_type="x",
        source_name="@builder",
        title="article",
        url="https://x.com/builder/status/1",
        published_at="2026-08-24T00:00:00+00:00",
        collected_at="2026-08-24T00:00:00+00:00",
        body="Read https://t.co/article",
    )
    monkeypatch.setattr(
        xmod,
        "fetch_linked_article",
        lambda url, client, **kwargs: {"original_url": url, "url": "https://example.com/a", "title": "A", "body": "Body"},
    )
    fetched, skipped = enrich_linked_articles(
        [item],
        object(),
        {"linked_articles": {"enabled": True}},
    )
    assert (fetched, skipped) == (1, 0)
    assert item.metadata["external_articles"][0]["body"] == "Body"
