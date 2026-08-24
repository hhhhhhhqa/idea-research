from datetime import datetime, timedelta, timezone

from ai_signal.models import PipelineResult, SignalItem
from ai_signal.report import prepare_report_context
from ai_signal.storage import build_feed, save_feed


def test_report_enriches_theme_and_watchlist(tmp_path):
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
    )
    stale_market_close = SignalItem(
        id="price:nvda",
        source_type="price",
        source_name="Yahoo Finance",
        title="NVDA close 100",
        url="https://finance.yahoo.com/quote/NVDA",
        published_at=(now - timedelta(days=3)).isoformat(),
        collected_at=now.isoformat(),
        symbols=["NVDA"],
        metadata={"ticker": "NVDA", "close": 100},
    )
    feed = build_feed([PipelineResult(pipeline="substack", items=[item, stale_market_close])])
    feed["generated_at"] = now.isoformat()
    save_feed(tmp_path, feed)
    profile = {
        "themes": {"AI 基础设施": ["inference", "gpu"]},
        "watchlist": [{"ticker": "NVDA", "name": "NVIDIA", "aliases": ["Blackwell"]}],
    }
    context = prepare_report_context(tmp_path, profile, "daily", now=now)
    assert context["items"][0]["matched_themes"] == ["AI 基础设施"]
    assert context["items"][0]["matched_symbols"] == ["NVDA"]
    assert context["market_context"][0]["metadata"]["ticker"] == "NVDA"
    assert context["report_contract"]["required_reasoning_chain"][-1] == "what would falsify the idea"
