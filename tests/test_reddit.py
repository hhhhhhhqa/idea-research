from datetime import datetime, timezone

from idea_research.pipelines.reddit import (
    _acquire_token,
    _download_reddit_images,
    _oauth_token,
    _refresh_token,
    extract_reddit_symbols,
    parse_reddit_json,
    parse_reddit_rss,
    _reddit_listing_request,
)


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    """Records token-grant POSTs; can simulate a failing grant type."""

    def __init__(self, access_token="tok", fail_grants=()):
        self.access_token = access_token
        self.fail_grants = set(fail_grants)
        self.requests: list[dict] = []

    def post(self, url, *, data, auth):
        self.requests.append({"url": url, "data": dict(data), "auth": auth})
        if data["grant_type"] in self.fail_grants:
            raise RuntimeError(f"{data['grant_type']} rejected")
        return _FakeResponse({"access_token": self.access_token})


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
    assert items[0].metadata["section"] == "transaction_ideas"


def test_parse_reddit_json_extracts_preview_image_urls():
    payload = {
        "data": {
            "children": [
                {
                    "data": {
                        "name": "t3_image",
                        "title": "Image post",
                        "created_utc": 1787486400,
                        "permalink": "/r/wallstreetbets/comments/image/post/",
                        "preview": {"images": [{"source": {"url": "https://i.redd.it/chart.png?width=1"}}]},
                    }
                }
            ]
        }
    }
    items = parse_reddit_json(
        payload,
        "wallstreetbets",
        now=datetime.fromtimestamp(1787486400, tz=timezone.utc),
        listing="hot",
        lookback_hours=None,
    )
    assert items[0].metadata["image_urls"] == ["https://i.redd.it/chart.png?width=1"]


def test_parse_reddit_json_filters_to_requested_dd_flair():
    created = 1787486400
    payload = {
        "data": {
            "children": [
                {"data": {
                    "name": "t3_dd",
                    "title": "$NVDA DD thesis",
                    "link_flair_text": "DD",
                    "created_utc": created,
                    "permalink": "/r/wallstreetbets/comments/dd/thesis/",
                }},
                {"data": {
                    "name": "t3_meme",
                    "title": "$GME meme",
                    "link_flair_text": "Meme",
                    "created_utc": created,
                    "permalink": "/r/wallstreetbets/comments/meme/post/",
                }},
            ]
        }
    }
    items = parse_reddit_json(
        payload,
        "wallstreetbets",
        now=datetime.fromtimestamp(created, tz=timezone.utc),
        listing="dd",
        flair="DD",
        lookback_hours=None,
    )
    assert len(items) == 1
    assert items[0].metadata["flair"] == "DD"
    assert items[0].metadata["listing"] == "dd"


def test_dd_listing_uses_daily_top_flair_search():
    endpoint, params, flair = _reddit_listing_request(
        {"listing": "dd", "flair": "DD", "sort": "top", "time_filter": "day"},
        10,
    )
    assert endpoint == "search"
    assert flair == ["DD"]
    assert params == {
        "q": 'flair:"DD"',
        "restrict_sr": "1",
        "sort": "top",
        "t": "day",
        "limit": 10,
        "raw_json": 1,
    }


def test_thesis_listing_queries_both_securityanalysis_flairs():
    endpoint, params, flairs = _reddit_listing_request(
        {"listing": "thesis", "flairs": ["Thesis", "Short Thesis"], "sort": "top", "time_filter": "day"},
        10,
    )
    assert endpoint == "search"
    assert flairs == ["Thesis", "Short Thesis"]
    assert params["q"] == 'flair:"Thesis" OR flair:"Short Thesis"'
    assert params["sort"] == "top"
    assert params["t"] == "day"


def test_reddit_rss_accepts_dd_when_subreddit_category_comes_first():
    xml = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>t3_dd_category</id><title>$NVDA DD</title>
    <link href="https://www.reddit.com/r/wallstreetbets/comments/dd_category/thesis/" />
    <updated>2026-08-24T01:00:00Z</updated>
    <category term="wallstreetbets"/><category term="DD"/>
    </entry></feed>"""
    items = parse_reddit_rss(
        xml,
        "wallstreetbets",
        now=datetime(2026, 8, 24, 2, tzinfo=timezone.utc),
        listing="dd",
        flair="DD",
        lookback_hours=None,
    )
    assert len(items) == 1
    assert items[0].metadata["flair"] == "DD"


class _FakeImageResponse:
    headers = {"content-type": "image/png"}

    def __init__(self, content=b"png"):
        self.content = content

    def raise_for_status(self):
        return None


class _FakeImageClient:
    def get(self, url):
        return _FakeImageResponse()


def test_download_reddit_images_writes_local_attachment(tmp_path):
    from idea_research.models import SignalItem

    item = SignalItem(
        id="reddit:abc",
        source_type="reddit",
        source_name="r/wallstreetbets",
        title="Chart",
        url="https://www.reddit.com/r/wallstreetbets/comments/abc/chart/",
        published_at="2026-08-24T00:00:00+00:00",
        collected_at="2026-08-24T00:00:00+00:00",
        metadata={"image_urls": ["https://i.redd.it/chart.png"]},
    )
    errors, _ = _download_reddit_images(
        [item],
        _FakeImageClient(),
        {"media_dir": str(tmp_path), "max_images_per_post": 4},
    )
    assert errors == []
    assert item.metadata["images"][0]["mime_type"] == "image/png"
    from pathlib import Path

    assert Path(item.metadata["images"][0]["path"]).exists()
    assert list(tmp_path.glob("reddit-image-*"))


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


def test_wsb_rss_extracts_tickers_and_records_dd_rank():
    xml = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>t3_wsb</id><title>$NVDA and Tesla rally while CEO speaks</title>
    <link href="https://www.reddit.com/r/wallstreetbets/comments/wsb/test/" />
    <updated>2026-08-24T01:00:00Z</updated><author><name>u/tester</name></author>
    <content type="html">&lt;p&gt;Watching NVDA and Tesla today&lt;/p&gt;</content>
    </entry></feed>"""
    items = parse_reddit_rss(
        xml,
        "wallstreetbets",
        now=datetime(2026, 8, 24, 2, tzinfo=timezone.utc),
        listing="hot",
        known_tickers=["NVDA", "CEO"],
        ticker_aliases={"TSLA": ["Tesla"]},
    )
    assert items[0].symbols == ["NVDA", "TSLA"]
    assert items[0].metadata["listing"] == "hot"
    assert items[0].metadata["feed_rank"] == 1


def test_hot_listing_can_keep_current_leaderboard_even_when_posts_are_older():
    xml = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>t3_hot</id><title>Still Hot</title>
    <link href="https://www.reddit.com/r/wallstreetbets/comments/hot/test/" />
    <updated>2026-08-01T01:00:00Z</updated><author><name>u/tester</name></author>
    </entry></feed>"""
    items = parse_reddit_rss(
        xml,
        "wallstreetbets",
        now=datetime(2026, 8, 24, 2, tzinfo=timezone.utc),
        listing="hot",
        lookback_hours=None,
    )
    assert len(items) == 1
    assert items[0].metadata["feed_rank"] == 1


def test_symbol_extraction_does_not_treat_every_acronym_as_ticker():
    assert extract_reddit_symbols("CEO sees AI demand at $PLTR", ["PLTR"]) == ["PLTR"]


def test_refresh_token_returns_empty_without_env(monkeypatch):
    for key in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_REFRESH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    assert _refresh_token(_FakeClient()) == ""
    assert _oauth_token(_FakeClient()) == ""


def test_refresh_token_uses_refresh_grant(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "r_tok")
    client = _FakeClient(access_token="access_1")
    assert _refresh_token(client) == "access_1"
    assert client.requests[0]["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "r_tok",
    }


def test_acquire_token_prefers_personal_oauth(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "r_tok")
    client = _FakeClient()
    notes: list[str] = []
    token = _acquire_token(client, notes)
    assert token == "tok"
    assert client.requests[0]["data"]["grant_type"] == "refresh_token"
    assert len(client.requests) == 1
    assert any("Personal-account OAuth" in note for note in notes)


def test_acquire_token_falls_back_when_refresh_fails(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("REDDIT_REFRESH_TOKEN", "stale")
    client = _FakeClient(access_token="app_tok", fail_grants={"refresh_token"})
    notes: list[str] = []
    token = _acquire_token(client, notes)
    assert token == "app_tok"
    assert client.requests[-1]["data"]["grant_type"] == "client_credentials"
    assert any("app-only" in note for note in notes)


def test_acquire_token_app_only_without_refresh(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    monkeypatch.delenv("REDDIT_REFRESH_TOKEN", raising=False)
    client = _FakeClient()
    notes: list[str] = []
    token = _acquire_token(client, notes)
    assert token == "tok"
    assert client.requests[0]["data"]["grant_type"] == "client_credentials"
    assert any("App-only OAuth" in note for note in notes)


def test_acquire_token_anonymous_when_no_creds(monkeypatch):
    for key in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_REFRESH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    notes: list[str] = []
    assert _acquire_token(_FakeClient(), notes) == ""
    assert not notes
