---
name: ai-signal-research
description: Read normalized Substack, Reddit, X, and Yahoo Finance feeds from this repository and generate a user-profiled daily or weekly public-equity AI research digest.
---

# AI Signal Research

This repository separates collection from judgment. Collection scripts publish normalized JSON; the Agent reads a prepared context package and writes the final report according to the selected user profile.

## Generate a report

From the repository root, run:

```bash
python -m ai_signal.cli prepare --period daily
# or
python -m ai_signal.cli prepare --period weekly
```

Read the generated `reports/contexts/<period>-agent-prompt.md`, then read the JSON path named inside it. Follow both the prompt and the JSON `report_contract`. Do not silently browse around missing pipeline data or infer missing price observations. If a pipeline is unavailable, disclose that limitation in the report.

## Customize

User preferences live in `config/profiles/*.yaml`. When the user asks to change focus, language, detail, themes, watchlist, exclusions, or item limits, edit or create a profile and pass it with `--profile`. Source subscriptions live separately in `config/sources.yaml`; changing report taste must not mutate the central source list.

## Collect

Run all sources with `python -m ai_signal.cli collect`. Run one source with `--pipeline substack`, `reddit`, `x`, or `prices`; an isolated refresh preserves the other source types already present in `data/feeds/latest.json`. X official API collection needs `X_BEARER_TOKEN`, unless an account has its own `rss_url`. Reddit can run anonymously and upgrades to OAuth when its environment variables are present.
