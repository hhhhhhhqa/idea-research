from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import load_dotenv, load_yaml, project_root
from .delivery import default_seen_path, mark_delivered
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
    rolling_hours = getattr(args, "rolling_hours", None)
    if rolling_hours is not None:
        if rolling_hours <= 0:
            print("--rolling-hours must be greater than zero", file=sys.stderr)
            return 2
        # A morning scheduler needs a rolling window: calendar-day filtering at
        # 07:00 would otherwise permanently miss everything published after the
        # previous day's run. Keep the configured/manual behavior unchanged.
        sources = copy.deepcopy(sources)
        for pipeline_name in ("substack", "x"):
            pipeline_config = sources.get(pipeline_name)
            if isinstance(pipeline_config, dict):
                pipeline_config["same_day"] = False
                pipeline_config["lookback_hours"] = rolling_hours
    selected = args.pipeline or list(PIPELINES)

    def run_phase(names: list[str]) -> list[Any]:
        phase_results = []
        for name in names:
            result = PIPELINES[name](sources.get(name, {}))
            phase_results.append(result)
            print(json.dumps(result.to_dict(), ensure_ascii=False), file=sys.stderr)
        return phase_results

    def save_phase(names: list[str], phase_results: list[Any]) -> Path:
        latest = Path(args.data_dir) / "feeds" / "latest.json"
        previous_feed: dict[str, Any] = read_json(latest) if latest.exists() else {}
        preserved: list[SignalItem] = []
        retention_days_by_type: dict[str, int] = {}
        for name in names:
            configured_days = sources.get(name, {}).get("retention_days", 0)
            try:
                retention_days = max(0, int(configured_days))
            except (TypeError, ValueError):
                retention_days = 0
            for source_type in SOURCE_TYPES[name]:
                retention_days_by_type[source_type] = retention_days
        if previous_feed:
            replaced_types = set().union(*(SOURCE_TYPES[name] for name in names))
            now = datetime.now(timezone.utc)
            preserved = [
                SignalItem.from_dict(item)
                for item in previous_feed.get("items", [])
                if item.get("source_type") not in replaced_types
                or (
                    retention_days_by_type.get(str(item.get("source_type") or ""), 0) > 0
                    and _item_within_retention(
                        item,
                        now,
                        retention_days_by_type[str(item.get("source_type") or "")],
                    )
                )
            ]
        feed = build_feed(phase_results, preserved)
        if previous_feed:
            current_statuses = {result.pipeline for result in phase_results}
            # A partial refresh retains status for the untouched pipelines, but an
            # older feed may contain duplicate statuses from earlier partial runs.
            # Keep only its newest status for each untouched pipeline.
            preserved_statuses: dict[str, dict[str, Any]] = {}
            for status in previous_feed.get("pipelines", []):
                name = str(status.get("pipeline") or "")
                if name and name not in current_statuses:
                    preserved_statuses[name] = status
            feed["pipelines"].extend(preserved_statuses.values())
        return save_feed(args.data_dir, feed)

    # X may deliberately wait for a twscrape SearchTimeline reset. Publish the
    # non-X sources first so their feed is usable while X is sleeping.
    if "x" in selected and len(selected) > 1:
        non_x = [name for name in selected if name != "x"]
        first_results = run_phase(non_x)
        save_phase(non_x, first_results)
        x_results = run_phase(["x"])
        results = first_results + x_results
        latest_path = save_phase(["x"], x_results)
    else:
        results = run_phase(selected)
        latest_path = save_phase(selected, results)
    final_feed = read_json(latest_path)
    print(json.dumps({"latest": str(latest_path), "counts": final_feed["counts"]}, ensure_ascii=False))
    failed = any(result.status == "error" for result in results)
    return 1 if args.strict and failed else 0


def _item_within_retention(item: dict[str, Any], now: datetime, retention_days: int) -> bool:
    try:
        published = datetime.fromisoformat(str(item.get("published_at") or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return published.astimezone(timezone.utc) >= now - timedelta(days=retention_days)


def _prepare(args: argparse.Namespace) -> int:
    profile = load_yaml(args.profile)
    context = prepare_report_context(
        args.data_dir,
        profile,
        args.period,
        include_seen=args.include_seen,
        seen_path=args.seen_path,
    )
    prompt_template = Path(args.prompt or project_root() / "prompts" / f"{args.period}.md")
    context_path, prompt_path = save_report_context(context, args.reports_dir, prompt_template)
    print(
        json.dumps(
            {
                "context": str(context_path),
                "agent_prompt": str(prompt_path),
                "delivery_mark": context.get("delivery_mark_path"),
                "stats": context["stats"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def _mark_delivered(args: argparse.Namespace) -> int:
    shown = [value for value in (args.shown or "").replace(";", ",").split(",") if value.strip()]
    if not args.all_items and not shown:
        print("Provide --shown N1,N2,X1 or --all after a successful digest delivery.", file=sys.stderr)
        return 2
    try:
        result = mark_delivered(
            args.file,
            shown,
            all_items=args.all_items,
            seen_path=args.seen_path,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _doctor(args: argparse.Namespace) -> int:
    sources = load_yaml(args.sources)
    price_pool_value = str(sources.get("prices", {}).get("saas_pool_path", "data/stock_universe/saas_pool.json"))
    price_pool_path = Path(price_pool_value)
    if not price_pool_path.is_absolute():
        price_pool_path = project_root() / price_pool_path
    price_pool: dict[str, Any] = {}
    if price_pool_path.exists():
        try:
            price_pool = json.loads(price_pool_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            price_pool = {}
    price_pool_stocks = price_pool.get("stocks") if isinstance(price_pool, dict) else []
    checks: dict[str, Any] = {
        "substack_publications": len(sources.get("substack", {}).get("publications") or []),
        "reddit_subreddits": len(sources.get("reddit", {}).get("subreddits") or []),
        "x_accounts": len(sources.get("x", {}).get("accounts") or []),
        "saas_price_pool": str(price_pool_path),
        "saas_price_pool_ready": bool(price_pool_stocks),
        "saas_price_pool_stocks": len(price_pool_stocks) if isinstance(price_pool_stocks, list) else 0,
        "reddit_oauth": bool(os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET")),
        "reddit_personal_oauth": bool(
            os.environ.get("REDDIT_REFRESH_TOKEN")
            and os.environ.get("REDDIT_CLIENT_ID")
            and os.environ.get("REDDIT_CLIENT_SECRET")
        ),
        "x_official_api": bool(os.environ.get("X_BEARER_TOKEN")),
        "x_twscrape_cookies": bool(os.environ.get("TWITTER_COOKIES")),
        "x_rss_accounts": sum(1 for value in sources.get("x", {}).get("accounts") or [] if isinstance(value, dict) and value.get("rss_url")),
    }
    checks["ready_without_more_credentials"] = bool(
        checks["substack_publications"] and checks["reddit_subreddits"] and checks["saas_price_pool_ready"]
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
    collect.add_argument(
        "--rolling-hours",
        type=int,
        help="Use a rolling Newsletter/X window (intended for unattended morning collection)",
    )
    collect.add_argument("--strict", action="store_true", help="Exit non-zero when a configured pipeline fails")
    collect.set_defaults(func=_collect)

    prepare = sub.add_parser("prepare", help="Build a prompt-ready context package for the current daily feed")
    prepare.add_argument("--period", choices=("daily",), default="daily")
    prepare.add_argument("--profile", default=str(root / "config" / "profiles" / "default.yaml"))
    prepare.add_argument("--data-dir", default=str(root / "data"))
    prepare.add_argument("--reports-dir", default=str(root / "reports"))
    prepare.add_argument("--prompt")
    prepare.add_argument("--include-seen", action="store_true", help="Include Newsletter/X items already marked as shown")
    prepare.add_argument("--seen-path", default=str(default_seen_path()))
    prepare.set_defaults(func=_prepare)

    delivered = sub.add_parser("mark-delivered", help="Mark the Newsletter/X IDs actually shown after successful delivery")
    delivered.add_argument(
        "--file",
        default=str(root / "reports" / "contexts" / "delivery-mark.json"),
        help="delivery-mark.json emitted by prepare",
    )
    delivered.add_argument("--shown", help="Comma-separated IDs, e.g. N1,N3,X1-X4")
    delivered.add_argument("--all", dest="all_items", action="store_true", help="Mark every pending Newsletter/X item")
    delivered.add_argument("--seen-path", default=str(default_seen_path()))
    delivered.add_argument("--dry-run", action="store_true")
    delivered.set_defaults(func=_mark_delivered)

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
