# GitHub Actions 07:00 feed publishing

This is the default unattended central-maintainer runtime when no always-on
computer is available. It uses a GitHub-hosted runner and does not require the
maintainer's Mac to be awake.

The workflow at `.github/workflows/daily-feed.yml` starts at 06:30
Asia/Shanghai (`22:30 UTC`). The non-X feed normally publishes within a few
minutes, while the lead time gives the separately rate-limited X phase room to
finish near 07:00. Scheduled runs are not guaranteed to start at the exact
second.

Each run has two ordered jobs:

1. Newsletter, Reddit, and Yahoo close data are collected with a rolling
   24-hour Newsletter window, validated, committed, and pushed first.
2. X runs separately with transaction-idea accounts first. Its two cookies come
   from repository Actions Secrets, and each twscrape cooldown wait is capped so
   the hosted job cannot wait forever.

Newsletter and X items still use the configured three-day retention window.
The workflow never commits `.env`, the twscrape database, FMP checkpoint, report
contexts, or delivery state. Its built-in `GITHUB_TOKEN` has only repository
contents write permission.

Required repository Actions Secrets:

- `TWITTER_COOKIES`
- `TWITTER_COOKIES_2`

Run it manually from GitHub's Actions page or with:

```bash
gh workflow run daily-feed.yml -f run_x=true
gh run list --workflow daily-feed.yml --limit 5
```

To validate only the fast/public phase, set `run_x=false`. The scheduled run
always includes X. Cookie sessions expire periodically; replace the two Actions
Secrets from a currently logged-in browser when the X job reports that no
cookie account is active.
