from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class SignalItem:
    id: str
    source_type: str
    source_name: str
    title: str
    url: str
    published_at: str
    collected_at: str
    body: str = ""
    author: str = ""
    symbols: list[str] = field(default_factory=list)
    engagement: dict[str, int | float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SignalItem":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value[key] for key in allowed if key in value})


@dataclass(slots=True)
class PipelineResult:
    pipeline: str
    status: str = "ok"
    items: list[SignalItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "status": self.status,
            "item_count": len(self.items),
            "errors": self.errors,
            "notes": self.notes,
        }
