import json
from datetime import datetime, timedelta, timezone

from idea_research.delivery import SEEN_RETENTION_DAYS, load_seen


def test_seen_state_retains_at_most_three_days(tmp_path):
    now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    payload = {
        "newsletters": {
            "substack:recent": (now - timedelta(days=2, hours=23)).isoformat(),
            "substack:old": (now - timedelta(days=3, seconds=1)).isoformat(),
        },
        "x": {},
    }
    path = tmp_path / "seen.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    seen = load_seen(path, now=now)

    assert SEEN_RETENTION_DAYS == 3
    assert seen["newsletters"] == {"substack:recent": payload["newsletters"]["substack:recent"]}
