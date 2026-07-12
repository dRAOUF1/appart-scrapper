"""Base parser interface and registry for listing sources."""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.listing import Listing


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
        """Return metadata for all registered parsers."""
        return [
            {
                "id": pid,
                "name": pcls.SOURCE_NAME,
                "description": pcls.SOURCE_DESCRIPTION,
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
        4. Implémenter parse(html) -> list[Listing]
        5. Décorer la classe avec @ParserRegistry.register
    """

    SOURCE_ID: str = ""
    SOURCE_NAME: str = ""
    SOURCE_DESCRIPTION: str = ""

    @abstractmethod
    def parse(self, html: str) -> list[Listing]:
        """
        Parse raw HTML content and extract listings.

        Args:
            html: Raw HTML string from the source website.

        Returns:
            List of Listing objects extracted from the HTML.
        """
        ...

    def scrape(self, criteria: dict, use_bff: bool = True) -> list[Listing]:
        """
        Scrape listings directly from the source using API/HTTP calls.

        Override this method for scrapers that don't need HTML input.

        Args:
            criteria: Search criteria dict (placeIds, priceMin, etc.)
            use_bff: Whether to use the BFF API for full pagination.

        Returns:
            List of Listing objects.
        """
        raise NotImplementedError("scrape() not implemented for this source")

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

    def __init_subclass__(cls, **kwargs):
        """Auto-register subclasses that have a SOURCE_ID."""
        super().__init_subclass__(**kwargs)
        if cls.SOURCE_ID:
            ParserRegistry.register(cls)
