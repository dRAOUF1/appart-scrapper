"""ScrapeLog model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass
class ScrapeLog:
    """Represents a single scrape execution log."""

    id: int | None = None
    search_id: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status: str = ""
    listings_found: int = 0
    new_listings: int = 0
    error_message: str = ""
    details: dict = field(default_factory=dict)
    duration_sec: float = 0.0
    raw_logs: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        return d
