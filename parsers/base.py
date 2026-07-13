"""Base parser interface and registry for listing sources."""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.listing import Listing


def get_locations(criteria: dict) -> list[dict]:
    """Normalize the location(s) in `criteria` into a list of
    {"city": ..., "postalCode": ...} dicts.

    Supports both the plural `locations` list (several cities/postal codes
    in a single search) and the legacy flat `city`/`postalCode` pair (one
    location). Every location-based parser should read locations through
    this helper instead of the flat keys directly, so multi-location
    support — and any future normalization — stays uniform across sources.
    """
    locations = criteria.get("locations")
    if locations:
        return [
            {"city": loc["city"], "postalCode": loc["postalCode"]}
            for loc in locations
            if loc.get("city") and loc.get("postalCode")
        ]
    city = criteria.get("city")
    postal_code = criteria.get("postalCode")
    if city and postal_code:
        return [{"city": city, "postalCode": postal_code}]
    return []


class ParserRegistry:
    """Auto-registry of all parser subclasses."""

    _parsers: dict[str, type["BaseParser"]] = {}

    @classmethod
    def register(cls, parser_cls: type["BaseParser"]) -> type["BaseParser"]:
        """Register a parser class by its SOURCE_ID."""
        source_id = parser_cls.SOURCE_ID
        if source_id:
            cls._parsers[source_id] = parser_cls
        return parser_cls

    @classmethod
    def get(cls, source: str) -> "BaseParser":
        """Instantiate and return a parser by source slug."""
        parser_cls = cls._parsers.get(source)
        if not parser_cls:
            available = ", ".join(sorted(cls._parsers.keys()))
            raise ValueError(
                f"Source '{source}' inconnue. Sources disponibles : {available}"
            )
        return parser_cls()

    @classmethod
    def list_sources(cls) -> list[dict]:
        """Return metadata for all registered parsers.

        Includes the "extra location field" metadata (see BaseParser) so
        the UI can generically render a per-source input for any source
        that can't work from city+postalCode alone — no template change
        needed when a future source needs one too.
        """
        return [
            {
                "id": pid,
                "name": pcls.SOURCE_NAME,
                "description": pcls.SOURCE_DESCRIPTION,
                "requires_extra_location": pcls.REQUIRES_EXTRA_LOCATION,
                "extra_location_label": pcls.EXTRA_LOCATION_LABEL,
                "extra_location_help": pcls.EXTRA_LOCATION_HELP,
                "url_note": pcls.URL_NOTE,
            }
            for pid, pcls in sorted(cls._parsers.items())
        ]


class BaseParser(ABC):
    """
    Interface de base pour tous les parsers de sources immobilières.

    Pour créer un nouveau parser :
        1. Hériter de BaseParser
        2. Définir SOURCE_ID (slug unique, ex: 'seloger')
        3. Définir SOURCE_NAME (nom affiché, ex: 'SeLoger')
        4. Implémenter scrape(criteria, use_bff) -> list[Listing] — c'est la
           seule méthode appelée par le pipeline (ScrapeService).
        5. Décorer la classe avec @ParserRegistry.register
    """

    SOURCE_ID: str = ""
    SOURCE_NAME: str = ""
    SOURCE_DESCRIPTION: str = ""

    # City + postal code is the universal location contract every source is
    # expected to work from (see has_valid_criteria below). A source that
    # cannot derive its own search identifier from city+postalCode alone
    # (e.g. SeLoger needs an opaque placeId with no public geocoding API)
    # sets REQUIRES_EXTRA_LOCATION = True and describes the extra field it
    # needs — the UI renders it generically from this metadata, so a future
    # source in the same situation needs no template changes.
    REQUIRES_EXTRA_LOCATION: bool = False
    EXTRA_LOCATION_LABEL: str = ""
    EXTRA_LOCATION_HELP: str = ""

    # Set when build_search_url() deliberately omits some criteria (e.g. a
    # source whose own filter query params break its location matching, so
    # they're enforced by the scraper instead of being reflected in the
    # reconstructed URL) — shown next to that URL so it doesn't look like a
    # bug when opened manually and some filters seem missing.
    URL_NOTE: str = ""

    @abstractmethod
    def scrape(self, criteria: dict, use_bff: bool = True) -> list[Listing]:
        """
        Scrape listings directly from the source using API/HTTP calls.

        Args:
            criteria: Search criteria dict (placeIds, priceMin, etc.)
            use_bff: Whether to use the BFF API for full pagination.

        Returns:
            List of Listing objects.
        """
        ...

    def parse(self, html: str) -> list[Listing]:
        """
        Parse raw HTML content and extract listings.

        Optional: only override for sources that parse a fetched HTML page
        directly instead of driving their own scrape() pipeline.

        Args:
            html: Raw HTML string from the source website.

        Returns:
            List of Listing objects extracted from the HTML.
        """
        raise NotImplementedError("parse() not implemented for this source")

    def build_search_url(self, criteria: dict) -> str | None:
        """
        Reconstruct a search URL from criteria.

        Override in subclasses for each source.

        Args:
            criteria: Search criteria dict (placeIds, priceMin, etc.)

        Returns:
            Search URL string or None if not implemented for this source.
        """
        return None

    def build_search_urls(self, criteria: dict) -> list[str]:
        """
        All search URLs for this source.

        Most sources only ever produce one URL (default: wraps
        build_search_url() as a single-item list). A source whose search
        can span several locations in one go (see get_locations()) should
        override this to return one URL per location.
        """
        url = self.build_search_url(criteria)
        return [url] if url else []

    def has_valid_criteria(self, criteria: dict) -> bool:
        """
        Whether `criteria` contains what this source needs to run a search.

        Default: at least one city + postalCode pair (see get_locations())
        is the universal location contract — every source (present or
        future) is expected to work from this unless it truly can't (see
        REQUIRES_EXTRA_LOCATION), in which case it overrides this to also
        check its own extra field.
        """
        return bool(get_locations(criteria))

    def __init_subclass__(cls, **kwargs):
        """Auto-register subclasses that have a SOURCE_ID."""
        super().__init_subclass__(**kwargs)
        if cls.SOURCE_ID:
            ParserRegistry.register(cls)
