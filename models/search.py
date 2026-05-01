"""Search model — saved search configuration."""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta


@dataclass
class Search:
    """Represents a saved search configuration for a user."""

    id: int
    user_id: int
    label: str
    ntfy_topic: str
    source: str = "seloger"
    criteria: dict = field(default_factory=dict)
    scrape_interval: int = 5
    last_scraped: datetime | None = None
    created_at: datetime | None = None
    is_active: bool = True
    blacklisted_agencies: list[str] = field(default_factory=list)
    blacklist_mode: str = "exclude"

    @staticmethod
    def valid_blacklist_modes() -> list[str]:
        return ["exclude", "no_notify"]

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        return d

    def has_valid_criteria(self) -> bool:
        return bool(
            self.criteria
            and isinstance(self.criteria, dict)
            and self.criteria.get("placeIds")
        )

    def should_scrape(self, now: datetime) -> bool:
        if not self.is_active:
            return False
        if not self.has_valid_criteria():
            return False
        if self.last_scraped:
            threshold = now - timedelta(minutes=self.scrape_interval)
            if self.last_scraped > threshold:
                return False
        return True
