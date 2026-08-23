"""User model."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass
class User:
    """Represents an application user."""

    id: int
    username: str
    created_at: datetime | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d.get("created_at") and isinstance(d["created_at"], datetime):
            d["created_at"] = d["created_at"].isoformat()
        return d
