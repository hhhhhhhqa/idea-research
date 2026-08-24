from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .config import load_dotenv, load_yaml, project_root
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
SOURCE_TYPES = {
    "substack": {"substack", "rss"},
    "reddit": {"reddit"},
    "x": {"x"},
    "prices": {"price"},
}


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
        replaced_types = set().union(*(SOURCE_TYPES[name] for name in selected))
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
    latest_path = save_feed(args.data_dir, feed)
    print(json.dumps({"latest": str(latest_path), "counts": feed["counts"]}, ensure_ascii=False))
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
        "reddit_personal_oauth": bool(
            os.environ.get("REDDIT_REFRESH_TOKEN")
            and os.environ.get("REDDIT_CLIENT_ID")
            and os.environ.get("REDDIT_CLIENT_SECRET")
        ),
        "x_official_api": bool(os.environ.get("X_BEARER_TOKEN")),
        "x_twscrape_cookies": bool(os.environ.get("TWITTER_COOKIES")),
        "x_rss_accounts": sum(1 for value in sources.get("x", {}).get("accounts") or [] if isinstance(value, dict) and value.get("rss_url")),
        "alpha_vantage_market_movers": bool(
            sources.get("prices", {}).get("market_movers", {}).get("enabled")
            and os.environ.get(str(sources.get("prices", {}).get("market_movers", {}).get("api_key_env", "ALPHAVANTAGE_API_KEY")))
        ),
    }
    checks["ready_without_more_credentials"] = bool(
        checks["substack_publications"] and checks["reddit_subreddits"] and checks["price_tickers"]
    )
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0


def _reddit_auth(args: argparse.Namespace) -> int:
    """One-time personal-account OAuth: authorize in a browser, capture the
    permanent refresh token, print it for .env. Run interactively on the
    account you want the collector to act as."""
    import secrets
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, quote, urlparse

    import httpx

    client_id = args.client_id or os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = args.client_secret or os.environ.get("REDDIT_CLIENT_SECRET", "")
    redirect_uri = args.redirect_uri or "http://127.0.0.1:8080"
    if not client_id or not client_secret:
        print("Reddit personal OAuth needs a free 'script' app.", file=sys.stderr)
        print("1) Create one at https://www.reddit.com/prefs/apps (type: script).", file=sys.stderr)
        print(f"   Use '{redirect_uri}' (or your own --redirect-uri) as the app's redirect URI.", file=sys.stderr)
        print("2) Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET in .env, or pass --client-id/--client-secret.", file=sys.stderr)
        return 1

    state = secrets.token_urlsafe(16)
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8080
    authorize_url = (
        "https://www.reddit.com/api/v1/authorize"
        f"?client_id={quote(client_id)}"
        "&response_type=code"
        f"&state={state}"
        f"&redirect_uri={quote(redirect_uri)}"
        "&duration=permanent"
        "&scope=read"
    )
    print("Open this URL while logged into the Reddit account you want to collect as:")
    print("  " + authorize_url)
    print(f"\nAuthorize in the browser; the redirect to {redirect_uri} is captured here.")

    captured: dict[str, list[str]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            captured.update(parse_qs(urlparse(self.path).query))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorization captured. You can close this tab.")

        def log_message(self, *args):  # keep the local server quiet
            pass

    server = HTTPServer((host, port), Handler)
    server.timeout = 60
    try:
        while not (captured.get("code") or captured.get("error")):
            server.handle_request()
    finally:
        server.server_close()
    if captured.get("error"):
        print(f"\nAuthorization failed: {captured['error']}")
        return 1
    if captured.get("state", [""])[0] != state:
        print("\nState mismatch; restart and try again.")
        return 1

    code = captured["code"][0]
    with httpx.Client(timeout=30) as http:
        response = http.post(
            "https://www.reddit.com/api/v1/access_token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            auth=(client_id, client_secret),
        )
        response.raise_for_status()
        payload = response.json()
    refresh_token = payload.get("refresh_token", "")
    if not refresh_token:
        print(f"\nNo refresh_token in response: {payload}")
        return 1
    print("\nSuccess. Add to .env:")
    print(f"REDDIT_REFRESH_TOKEN={refresh_token}")
    print("\nKeep it secret; it grants read access to that account. (scope: read)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    root = project_root()
    parser = argparse.ArgumentParser(prog="idea-research", description="Collect research signals and prepare Agent report context")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="Run one or more collection pipelines")
    collect.add_argument("--pipeline", action="append", choices=PIPELINES, help="May be repeated; default is all")
    collect.add_argument("--sources", default=str(root / "config" / "sources.yaml"))
    collect.add_argument("--data-dir", default=str(root / "data"))
    collect.add_argument("--strict", action="store_true", help="Exit non-zero when a configured pipeline fails")
    collect.set_defaults(func=_collect)

    prepare = sub.add_parser("prepare", help="Build a prompt-ready context package for the current daily feed")
    prepare.add_argument("--period", choices=("daily",), default="daily")
    prepare.add_argument("--profile", default=str(root / "config" / "profiles" / "default.yaml"))
    prepare.add_argument("--data-dir", default=str(root / "data"))
    prepare.add_argument("--reports-dir", default=str(root / "reports"))
    prepare.add_argument("--prompt")
    prepare.set_defaults(func=_prepare)

    doctor = sub.add_parser("doctor", help="Show configuration and credential readiness without collecting")
    doctor.add_argument("--sources", default=str(root / "config" / "sources.yaml"))
    doctor.set_defaults(func=_doctor)

    auth = sub.add_parser("reddit-auth", help="One-time OAuth to mint a personal REDDIT_REFRESH_TOKEN")
    auth.add_argument("--client-id", default="")
    auth.add_argument("--client-secret", default="")
    auth.add_argument("--redirect-uri", default="http://127.0.0.1:8080")
    auth.set_defaults(func=_reddit_auth)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
