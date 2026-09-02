# cron-job.org 07:00 external trigger

This is the default unattended central-maintainer runtime when no always-on
computer is available. cron-job.org owns the schedule and calls GitHub's
`workflow_dispatch` API; the GitHub-hosted runner only executes the collection.
The maintainer's Mac does not need to be awake.

The cron job runs every day at 07:00 in `Asia/Hong_Kong` (the same UTC offset as
`Asia/Shanghai`). The workflow at `.github/workflows/daily-feed.yml` contains no
GitHub `schedule`; it starts only through `workflow_dispatch` from cron-job.org
or a manual invocation.

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

Some Substack domains reject GitHub datacenter IPs. The collector first tries
the original RSS and Substack archive API, then may use a public reader as a
transport-only fallback. Only the public source URL is sent; credentials are
never sent, and every item retains its original Substack link plus a transport
marker in metadata.

Required repository Actions Secrets:

- `TWITTER_COOKIES`
- `TWITTER_COOKIES_2`

Create the cron-job.org job with:

- URL: `https://api.github.com/repos/hhhhhhhqa/idea-research/actions/workflows/daily-feed.yml/dispatches`
- Method: `POST`
- Time zone: `Asia/Hong_Kong`
- Schedule: every day at `07:00`
- Body: `{"ref":"main","inputs":{"run_x":true}}`
- Headers: `Accept: application/vnd.github+json`, `Content-Type: application/json`,
  `Authorization: Bearer <FINE_GRAINED_PAT>`, and
  `X-GitHub-Api-Version: 2026-03-10`

Use a fine-grained GitHub token restricted to this repository with repository
`Actions: Read and write`. Keep the token only in cron-job.org, never in this
repository. A successful test returns HTTP 200 and creates a workflow run.

Run it manually from GitHub's Actions page or with:

```bash
gh workflow run daily-feed.yml -f run_x=true
gh run list --workflow daily-feed.yml --limit 5
```

To validate only the fast/public phase, set `run_x=false`. The configured cron
request includes X. Cookie sessions expire periodically; replace the two Actions
Secrets from a currently logged-in browser when the X job reports that no
cookie account is active.
