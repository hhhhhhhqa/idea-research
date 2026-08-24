from datetime import datetime, timezone

from idea_research.pipelines.reddit import (
    _acquire_token,
    _oauth_token,
    _refresh_token,
    extract_reddit_symbols,
    parse_reddit_json,
    parse_reddit_rss,
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


def test_wsb_rss_extracts_tickers_and_records_hot_rank():
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
