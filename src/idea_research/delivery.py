from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SEEN_RETENTION_DAYS = 3
DEFAULT_SEEN_PATH = Path.home() / ".idea-research" / "seen.json"
_LABEL_RE = re.compile(r"^(?P<prefix>N|X)(?P<number>[1-9][0-9]*)$", re.IGNORECASE)
_RANGE_RE = re.compile(r"^(?P<prefix>N|X)(?P<start>[1-9][0-9]*)-(?:(?:N|X))?(?P<end>[1-9][0-9]*)$", re.IGNORECASE)


def default_seen_path() -> Path:
    return DEFAULT_SEEN_PATH


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _empty_seen() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "retention_days": SEEN_RETENTION_DAYS,
        "newsletters": {},
        "x": {},
    }


def load_seen(path: str | Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Load the subscriber-local delivery state and prune old entries.

    The state is deliberately outside the repository so different Agents can
    maintain independent reading queues without changing the central feed.
    """
    target = Path(path) if path else default_seen_path()
    result = _empty_seen()
    if target.exists():
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            payload = {}
        if isinstance(payload, dict):
            for kind in ("newsletters", "x"):
                values = payload.get(kind)
                if isinstance(values, dict):
                    result[kind] = {str(item_id): value for item_id, value in values.items()}
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=SEEN_RETENTION_DAYS)
    for kind in ("newsletters", "x"):
        result[kind] = {
            item_id: timestamp
            for item_id, timestamp in result[kind].items()
            if (parsed := _parse_timestamp(timestamp)) is not None and parsed >= cutoff
        }
    return result


def save_seen(seen: dict[str, Any], path: str | Path | None = None, *, now: datetime | None = None) -> Path:
    target = Path(path) if path else default_seen_path()
    current = load_seen(target, now=now)
    for kind in ("newsletters", "x"):
        values = seen.get(kind) if isinstance(seen, dict) else {}
        if isinstance(values, dict):
            current[kind].update({str(item_id): timestamp for item_id, timestamp in values.items()})
    current["updated_at"] = (now or datetime.now(timezone.utc)).isoformat()
    _atomic_json(target, current)
    return target


def tracked_kind(source_type: str) -> str | None:
    if source_type in {"substack", "rss"}:
        return "newsletters"
    if source_type == "x":
        return "x"
    return None


def build_delivery_mark(items: Iterable[dict[str, Any]], *, prepared_at: str) -> dict[str, Any]:
    labels: dict[str, dict[str, str]] = {}
    counts = {"newsletters": 0, "x": 0}
    for item in items:
        kind = tracked_kind(str(item.get("source_type") or ""))
        label = str(item.get("display_id") or "")
        item_id = str(item.get("id") or "")
        if not kind or not label or not item_id:
            continue
        labels[label] = {"kind": kind, "id": item_id}
        counts[kind] += 1
    return {
        "schema_version": "1.0",
        "prepared_at": prepared_at,
        "retention_days": SEEN_RETENTION_DAYS,
        "labels": labels,
        "counts": counts,
        "instructions": "Mark only Newsletter/X labels actually shown after successful delivery; WSB and price IDs are not tracked.",
    }


def _expand_labels(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for raw in values:
        for token in re.split(r"[\s,;]+", str(raw).strip().upper()):
            if not token:
                continue
            match = _RANGE_RE.match(token)
            if match:
                start, end = int(match.group("start")), int(match.group("end"))
                if start > end:
                    start, end = end, start
                result.extend(f"{match.group('prefix').upper()}{number}" for number in range(start, end + 1))
                continue
            if _LABEL_RE.match(token):
                result.append(token)
    return list(dict.fromkeys(result))


def mark_delivered(
    mark_path: str | Path,
    shown: Iterable[str] = (),
    *,
    all_items: bool = False,
    seen_path: str | Path | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mark only labels actually shown in a successfully delivered digest."""
    target = Path(mark_path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"delivery mark not readable: {target}") from exc
    labels = payload.get("labels") if isinstance(payload, dict) else {}
    if not isinstance(labels, dict):
        labels = {}
    available = {str(label).upper(): value for label, value in labels.items()}
    requested = list(available) if all_items else _expand_labels(shown)
    selected: dict[str, dict[str, str]] = {}
    unknown: list[str] = []
    for label in requested:
        value = available.get(label)
        if isinstance(value, dict) and value.get("kind") in {"newsletters", "x"} and value.get("id"):
            selected[label] = {"kind": str(value["kind"]), "id": str(value["id"])}
        else:
            unknown.append(label)
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    if not dry_run and selected:
        seen = load_seen(seen_path, now=now)
        for value in selected.values():
            seen[value["kind"]][value["id"]] = timestamp
        save_seen(seen, seen_path, now=now)
    return {
        "mark_file": str(target),
        "marked": list(selected),
        "unknown": unknown,
        "dry_run": dry_run,
        "seen_path": str(Path(seen_path) if seen_path else default_seen_path()),
    }
