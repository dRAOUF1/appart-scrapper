"""Validation for user-supplied search payloads (criteria, scrape_interval).

Criteria stay a loose, source-agnostic dict (different parser sources may use
different keys) — this only validates the types of the well-known fields
instead of persisting whatever shape the client sent straight to the DB and
the scraper.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationError


class SearchCriteria(BaseModel):
    model_config = ConfigDict(extra="allow")

    placeIds: list[str] | None = None
    priceMin: int | None = None
    priceMax: int | None = None
    spaceMin: int | None = None
    spaceMax: int | None = None
    rooms: list | None = None
    bedrooms: list | None = None
    distributionTypes: list[str] | None = None
    estateTypes: list[str] | None = None
    order: str | None = None


def validate_criteria(data) -> dict:
    """Validate and normalize a criteria payload. Raises ValueError on bad types."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("criteria doit être un objet JSON")
    try:
        model = SearchCriteria.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"Critères invalides: {e.errors()[0]['msg']}")
    return model.model_dump(exclude_none=True)


def validate_scrape_interval(value, minimum: int = 1, maximum: int = 1440) -> int:
    """Validate scrape_interval (minutes). Raises ValueError on bad input."""
    try:
        interval = int(value)
    except (TypeError, ValueError):
        raise ValueError("scrape_interval doit être un entier")
    if interval < minimum or interval > maximum:
        raise ValueError(f"scrape_interval doit être entre {minimum} et {maximum} minutes")
    return interval
