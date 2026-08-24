# Auto Install — Zero Command Line for the User

The user should not have to operate a terminal. Detect whether this repository is already a complete checkout; if it is, use it directly. Otherwise install a runtime checkout into the current Agent platform's skill location:

| Platform | Suggested skill directory |
|---|---|
| Codex | `$CODEX_HOME/skills/idea-research` |
| Claude Code | `~/.claude/skills/idea-research` |
| OpenClaw | `~/skills/idea-research` |
| Other local Agent | `~/idea-research` |

Clone `https://github.com/hhhhhhhqa/idea-research.git` into that directory and run `python -m pip install -e ".[dev]"` from the checkout. If the repository is private, use the user's existing authenticated GitHub connection; do not ask them to paste a GitHub token into chat.

Then pull the current centrally published data:

```bash
git pull --ff-only
```

Do not create or request `.env` for a subscriber. X, Reddit, FMP and pricing credentials belong only to the central maintainer. Continue to onboarding and generate the first report from the published feed.
