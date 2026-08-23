"""Search model — saved search configuration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta


@dataclass
class Search:
    """Represents a saved search configuration for a user."""

    id: int
    user_id: int
    label: str
    ntfy_topic: str
    source: str = "seloger"
    sources: list[str] = field(default_factory=list)
    criteria: dict = field(default_factory=dict)
    scrape_interval: int = 5
    last_scraped: datetime | None = None
    created_at: datetime | None = None
    is_active: bool = True
    # Issue #10 : notifications ntfy activées par défaut ; à False, le scrape
    # continue mais aucun push ne part (les annonces restent enregistrées).
    notify_enabled: bool = True
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
        """True if at least one of this search's sources can run with `criteria`.

        Each source encodes location differently (opaque placeIds vs.
        city/postal code, ...), so validity is delegated to the parsers
        rather than checking a single hardcoded key here.
        """
        if not self.criteria or not isinstance(self.criteria, dict):
            return False
        from parsers import get_parser

        for src in (self.sources or [self.source]):
            try:
                parser = get_parser(src)
            except ValueError:
                continue
            if parser.has_valid_criteria(self.criteria):
                return True
        return False

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
