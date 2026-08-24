from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .config import load_yaml, project_root
from .models import SignalItem
from .pipelines import collect_prices, collect_reddit, collect_substack, collect_x
from .report import prepare_report_context, save_report_context
from .storage import build_feed, read_json, save_feed


PIPELINES = {
    "substack": collect_substack,
    "reddit": collect_reddit,
    "x": collect_x,
    "prices": collect_prices,
}
SOURCE_TYPES = {"substack": "substack", "reddit": "reddit", "x": "x", "prices": "price"}


def _collect(args: argparse.Namespace) -> int:
    sources = load_yaml(args.sources)
    selected = args.pipeline or list(PIPELINES)
    results = []
    for name in selected:
        result = PIPELINES[name](sources.get(name, {}))
        results.append(result)
        print(json.dumps(result.to_dict(), ensure_ascii=False), file=sys.stderr)

    preserved: list[SignalItem] = []
    latest = Path(args.data_dir) / "feeds" / "latest.json"
    previous_feed: dict[str, Any] = {}
    if latest.exists() and set(selected) != set(PIPELINES):
        previous_feed = read_json(latest)
        replaced_types = {SOURCE_TYPES[name] for name in selected}
        preserved = [
            SignalItem.from_dict(item)
            for item in previous_feed.get("items", [])
            if item.get("source_type") not in replaced_types
        ]
    feed = build_feed(results, preserved)
    if previous_feed:
        current_statuses = {result.pipeline for result in results}
        feed["pipelines"].extend(
            status
            for status in previous_feed.get("pipelines", [])
            if status.get("pipeline") not in current_statuses
        )
    latest_path, snapshot_path = save_feed(args.data_dir, feed)
    print(json.dumps({"latest": str(latest_path), "snapshot": str(snapshot_path), "counts": feed["counts"]}, ensure_ascii=False))
    failed = any(result.status == "error" for result in results)
    return 1 if args.strict and failed else 0


def _prepare(args: argparse.Namespace) -> int:
    profile = load_yaml(args.profile)
    context = prepare_report_context(args.data_dir, profile, args.period)
    prompt_template = Path(args.prompt or project_root() / "prompts" / f"{args.period}.md")
    context_path, prompt_path = save_report_context(context, args.reports_dir, prompt_template)
    print(json.dumps({"context": str(context_path), "agent_prompt": str(prompt_path), "stats": context["stats"]}, ensure_ascii=False))
    return 0


def _doctor(args: argparse.Namespace) -> int:
    sources = load_yaml(args.sources)
    checks: dict[str, Any] = {
        "substack_publications": len(sources.get("substack", {}).get("publications") or []),
        "reddit_subreddits": len(sources.get("reddit", {}).get("subreddits") or []),
        "x_accounts": len(sources.get("x", {}).get("accounts") or []),
        "price_tickers": len(sources.get("prices", {}).get("tickers") or []),
        "reddit_oauth": bool(os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET")),
        "x_official_api": bool(os.environ.get("X_BEARER_TOKEN")),
        "x_rss_accounts": sum(1 for value in sources.get("x", {}).get("accounts") or [] if isinstance(value, dict) and value.get("rss_url")),
    }
    checks["ready_without_more_credentials"] = bool(
        checks["substack_publications"] and checks["reddit_subreddits"] and checks["price_tickers"]
    )
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    root = project_root()
    parser = argparse.ArgumentParser(prog="ai-signal", description="Collect research signals and prepare Agent report context")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="Run one or more collection pipelines")
    collect.add_argument("--pipeline", action="append", choices=PIPELINES, help="May be repeated; default is all")
    collect.add_argument("--sources", default=str(root / "config" / "sources.yaml"))
    collect.add_argument("--data-dir", default=str(root / "data"))
    collect.add_argument("--strict", action="store_true", help="Exit non-zero when a configured pipeline fails")
    collect.set_defaults(func=_collect)

    prepare = sub.add_parser("prepare", help="Build a prompt-ready daily or weekly context package")
    prepare.add_argument("--period", choices=("daily", "weekly"), required=True)
    prepare.add_argument("--profile", default=str(root / "config" / "profiles" / "default.yaml"))
    prepare.add_argument("--data-dir", default=str(root / "data"))
    prepare.add_argument("--reports-dir", default=str(root / "reports"))
    prepare.add_argument("--prompt")
    prepare.set_defaults(func=_prepare)

    doctor = sub.add_parser("doctor", help="Show configuration and credential readiness without collecting")
    doctor.add_argument("--sources", default=str(root / "config" / "sources.yaml"))
    doctor.set_defaults(func=_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
