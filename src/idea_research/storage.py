from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .models import PipelineResult, SignalItem, utc_now


SCHEMA_VERSION = "1.0"


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


def build_feed(
    results: Iterable[PipelineResult],
    preserved_items: Iterable[SignalItem] = (),
) -> dict[str, Any]:
    result_list = list(results)
    items_by_id: dict[str, SignalItem] = {item.id: item for item in preserved_items}
    for result in result_list:
        for item in result.items:
            items_by_id[item.id] = item
    items = sorted(
        items_by_id.values(),
        key=lambda item: (item.published_at or item.collected_at, item.id),
        reverse=True,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "pipelines": [result.to_dict() for result in result_list],
        "counts": {
            "total": len(items),
            **{
                name: sum(1 for item in items if item.source_type == name)
                for name in ("substack", "rss", "reddit", "x", "price")
            },
        },
        "items": [item.to_dict() for item in items],
    }


def save_feed(data_dir: str | Path, feed: dict[str, Any]) -> Path:
    """Atomically replace the single centrally published rolling feed.

    The repository intentionally has no daily archive.  Pipelines that need a
    short retry window (currently Newsletter/X) retain their configured recent
    items in ``latest.json``; longer historical retention belongs elsewhere.
    """
    root = Path(data_dir)
    latest = root / "feeds" / "latest.json"
    _atomic_json(latest, feed)
    return latest


def read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
