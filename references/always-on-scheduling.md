# Always-on 07:00 feed publishing

The collector needs network access, the uncommitted `.env`, a persistent
`twscrape` account database, and permission to push generated feed files. A Mac
that is asleep with its lid closed cannot reliably provide that runtime. Use a
small always-on Linux host for the central-maintainer copy instead; this does
not require GitHub Actions.

## One-time host setup

The checked-in systemd unit assumes a dedicated `idea-research` user and a clone
at `/srv/idea-research`. Create the user and an SSH key first:

```bash
sudo useradd --create-home --shell /bin/bash idea-research
sudo -u idea-research mkdir -p /home/idea-research/.ssh
sudo -u idea-research ssh-keygen -t ed25519 -f /home/idea-research/.ssh/id_ed25519 -N ''
sudo -u idea-research cat /home/idea-research/.ssh/id_ed25519.pub
```

Add only that public key to this repository's GitHub **Deploy keys**, with write
access enabled. Never copy the private key off the host. After verifying
GitHub's SSH host fingerprint, clone and install the project:

```bash
sudo mkdir -p /srv/idea-research
sudo chown idea-research:idea-research /srv/idea-research
sudo -u idea-research git clone git@github.com:hhhhhhhqa/idea-research.git /srv/idea-research
sudo -u idea-research python3.11 -m venv /srv/idea-research/.venv
sudo -u idea-research /srv/idea-research/.venv/bin/pip install -e /srv/idea-research
```

Copy the central `.env` to `/srv/idea-research/.env` over SSH and restrict it;
never paste it into a shell history, systemd unit, commit, or chat:

```bash
sudo chown idea-research:idea-research /srv/idea-research/.env
sudo chmod 600 /srv/idea-research/.env
```

Set a commit identity for the dedicated publisher:

```bash
sudo -u idea-research git -C /srv/idea-research config user.name "Idea Research Bot"
sudo -u idea-research git -C /srv/idea-research config user.email "idea-research-bot@users.noreply.github.com"
```

Install and start the timer:

```bash
sudo cp /srv/idea-research/deploy/systemd/idea-research-feed.service /etc/systemd/system/
sudo cp /srv/idea-research/deploy/systemd/idea-research-feed.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now idea-research-feed.timer
```

## Verify before relying on it

Run one complete test, inspect the logs, and confirm the next Shanghai-time run:

```bash
sudo systemctl start idea-research-feed.service
sudo journalctl -u idea-research-feed.service -n 200 --no-pager
systemctl list-timers idea-research-feed.timer
```

The service uses a repository-local lock to prevent overlapping runs and retries
fatal failures up to three times through systemd. `Persistent=true` runs a missed
job when the host comes back. It first collects and pushes Newsletter, Reddit,
and US close data, then handles X separately; an X cooldown therefore cannot
delay publication of the other sources. Set `IDEA_RESEARCH_SKIP_X=1` in the
service only when X should be disabled entirely.

The scheduled run passes `--rolling-hours 24` for Newsletter and X. This avoids
the gap that calendar-day filtering would create at 07:00 while preserving the
normal three-day feed retention. Manual `idea-research collect` runs continue to
use the calendar-day behavior in `config/sources.yaml`.
