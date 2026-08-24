from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Config file does not exist: {source}")
    value = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError(f"Config root must be a mapping: {source}")
    return value


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]
