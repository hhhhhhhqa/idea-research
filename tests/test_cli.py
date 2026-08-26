from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from idea_research import cli
from idea_research.models import PipelineResult, SignalItem
from idea_research.storage import read_json


def _item(source_type: str, value: str) -> SignalItem:
    return SignalItem(
        id=f"{source_type}:{value}",
        source_type=source_type,
        source_name=value,
        title=value,
        url=f"https://example.com/{value}",
        published_at="2026-08-26T00:00:00+00:00",
        collected_at="2026-08-26T00:00:00+00:00",
        body=value,
    )


def test_collect_publishes_non_x_sources_before_x_wait(tmp_path, monkeypatch):
    sources = tmp_path / "sources.yaml"
    sources.write_text("substack: {}\nreddit: {}\nx: {}\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    observed: dict[str, bool] = {}

    def fake_substack(config):
        return PipelineResult("substack", items=[_item("substack", "newsletter")])

    def fake_reddit(config):
        return PipelineResult("reddit", items=[_item("reddit", "wsb")])

    def fake_x(config):
        published = read_json(data_dir / "feeds" / "latest.json")
        observed["non_x_published"] = {item["source_type"] for item in published["items"]} == {"substack", "reddit"}
        return PipelineResult("x", items=[_item("x", "idea")])

    monkeypatch.setitem(cli.PIPELINES, "substack", fake_substack)
    monkeypatch.setitem(cli.PIPELINES, "reddit", fake_reddit)
    monkeypatch.setitem(cli.PIPELINES, "x", fake_x)
    args = SimpleNamespace(
        sources=str(sources),
        pipeline=["substack", "reddit", "x"],
        data_dir=str(data_dir),
        strict=False,
    )

    assert cli._collect(args) == 0
    assert observed["non_x_published"] is True
    final_feed = read_json(data_dir / "feeds" / "latest.json")
    assert {item["source_type"] for item in final_feed["items"]} == {"substack", "reddit", "x"}


def test_collect_retains_only_recent_newsletter_items(tmp_path, monkeypatch):
    sources = tmp_path / "sources.yaml"
    sources.write_text("substack:\n  retention_days: 3\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    now = datetime.now(timezone.utc)
    recent = _item("substack", "recent")
    recent.published_at = (now - timedelta(days=2)).isoformat()
    expired = _item("substack", "expired")
    expired.published_at = (now - timedelta(days=4)).isoformat()
    from idea_research.storage import save_feed, build_feed

    save_feed(data_dir, build_feed([PipelineResult("substack", items=[recent, expired])]))

    def fake_substack(config):
        return PipelineResult("substack", items=[_item("substack", "today")])

    monkeypatch.setitem(cli.PIPELINES, "substack", fake_substack)
    args = SimpleNamespace(
        sources=str(sources),
        pipeline=["substack"],
        data_dir=str(data_dir),
        strict=False,
    )
    assert cli._collect(args) == 0
    ids = {item["id"] for item in read_json(data_dir / "feeds" / "latest.json")["items"]}
    assert "substack:recent" in ids
    assert "substack:expired" not in ids
