"""Base parser interface and registry for listing sources.

Un parser reçoit toujours des critères au vocabulaire canonique (voir
core.criteria) et les traduit lui-même vers le format de sa source, dans
to_native(). C'est ce qui permet à l'utilisateur de définir ses critères une
seule fois pour toutes les sources : ajouter une source ne demande ni de
toucher au front, ni de changer ce qui est stocké en base.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.criteria import (
    PROPERTY_TYPE_LABELS,
    PROPERTY_TYPES,
    TRANSACTION_LABELS,
    TRANSACTIONS,
    has_transit,
    normalize_locations,
)
from models.listing import Listing


def get_locations(criteria: dict) -> list[dict]:
    """Les localisations des critères, sous forme de liste de dicts.

    Chaque entrée a au minimum `city` et `postalCode`, et garde `inseeCode`
    / `lat` / `lon` quand l'autocomplete les a fournis — c'est le code INSEE
    qui permet à chaque source de retrouver son propre identifiant de lieu
    (voir core.geocode et services.seloger_geocode), il ne doit donc surtout
    pas être perdu en route.

    Délègue la normalisation à core.criteria : les localisations incomplètes
    sont écartées et l'ancien couple à plat city/postalCode reste compris,
    même si l'appelant a oublié de normaliser ses critères en amont.
    """
    return normalize_locations(criteria)


class ParserRegistry:
    """Auto-registry of all parser subclasses."""

    _parsers: dict[str, type[BaseParser]] = {}

    @classmethod
    def register(cls, parser_cls: type[BaseParser]) -> type[BaseParser]:
        """Register a parser class by its SOURCE_ID."""
        source_id = parser_cls.SOURCE_ID
        if source_id:
            cls._parsers[source_id] = parser_cls
        return parser_cls

    @classmethod
    def get(cls, source: str, storage=None) -> BaseParser:
        """Instantiate and return a parser by source slug.

        `storage` est passé aux sources qui ont besoin de la base (ex. SeLoger
        et son cache d'identifiants de lieu). Il est fourni explicitement
        plutôt que lu depuis `flask.current_app` parce que le scraping tourne
        sur un thread de fond, hors de tout contexte d'application (voir
        core.scrape_control et ScrapeService).
        """
        parser_cls = cls._parsers.get(source)
        if not parser_cls:
            available = ", ".join(sorted(cls._parsers.keys()))
            raise ValueError(
                f"Source '{source}' inconnue. Sources disponibles : {available}"
            )
        return parser_cls(storage=storage)

    @classmethod
    def list_sources(cls) -> list[dict]:
        """Return metadata for all registered parsers.

        Inclut les capacités de chaque source (transactions et types de bien
        qu'elle sait traiter) pour que le front puisse prévenir l'utilisateur
        qu'une case qu'il vient de cocher ne sera pas honorée par une des
        sources sélectionnées — sans rien coder en dur par source.
        """
        return [
            {
                "id": pid,
                "name": pcls.SOURCE_NAME,
                "description": pcls.SOURCE_DESCRIPTION,
                "supported_transactions": list(pcls.SUPPORTED_TRANSACTIONS),
                "supported_property_types": list(pcls.SUPPORTED_PROPERTY_TYPES),
                "manual_override_label": pcls.MANUAL_OVERRIDE_LABEL,
                "manual_override_help": pcls.MANUAL_OVERRIDE_HELP,
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
        4. Déclarer ce que la source sait traiter (SUPPORTED_TRANSACTIONS,
           SUPPORTED_PROPERTY_TYPES) si elle ne couvre pas tout
        5. Implémenter to_native(criteria) — la traduction du vocabulaire
           canonique vers le format de la source
        6. Implémenter scrape(criteria) -> list[Listing] — la seule méthode
           appelée par le pipeline (ScrapeService)
        7. Décorer la classe avec @ParserRegistry.register
    """

    SOURCE_ID: str = ""
    SOURCE_NAME: str = ""
    SOURCE_DESCRIPTION: str = ""

    def __init__(self, storage=None):
        """`storage` n'est utile qu'aux sources qui ont besoin de la base (ex.
        SeLoger et son cache d'identifiants de lieu). Il est injecté ici plutôt
        que lu depuis `flask.current_app` : le scraping tourne sur un thread de
        fond, sans contexte d'application. Les sources qui n'en ont pas besoin
        l'ignorent simplement.
        """
        self.storage = storage

    # Ce que la source sait traiter, dans le vocabulaire canonique. Par
    # défaut tout, à restreindre pour une source qui ne couvre qu'une partie
    # (Laforet ne référence par exemple ni parking ni terrain). Déclarer ces
    # limites permet de le dire à l'utilisateur AVANT de lancer un scrape,
    # au lieu de lever une exception au milieu du pipeline.
    SUPPORTED_TRANSACTIONS: tuple[str, ...] = TRANSACTIONS
    SUPPORTED_PROPERTY_TYPES: tuple[str, ...] = PROPERTY_TYPES

    # Set when build_search_url() deliberately omits some criteria (e.g. a
    # source whose own filter query params break its location matching, so
    # they're enforced by the scraper instead of being reflected in the
    # reconstructed URL) — shown next to that URL so it doesn't look like a
    # bug when opened manually and some filters seem missing.
    URL_NOTE: str = ""

    # Champ de saisie libre, optionnel, propre à cette source : le repli
    # quand la traduction automatique du canonique ne suffit pas (ex. coller
    # une URL SeLoger si son endpoint de résolution de lieu tombe). Laissé
    # vide, la source n'en propose aucun — le front n'affiche le champ, dans
    # ses options avancées, que pour les sources qui en déclarent un. Ce qui
    # est saisi finit dans criteria["sourceOverrides"][SOURCE_ID] via
    # parse_manual_override(), jamais au premier niveau des critères.
    MANUAL_OVERRIDE_LABEL: str = ""
    MANUAL_OVERRIDE_HELP: str = ""

    def parse_manual_override(self, value: str) -> dict:
        """Transforme la saisie libre de l'utilisateur en surcharge pour
        cette source. {} si la saisie est vide ou inexploitable.

        À surcharger par toute source qui déclare MANUAL_OVERRIDE_LABEL.
        """
        return {}

    def remember_manual_override(self, criteria: dict) -> None:
        """Appelé une fois après l'enregistrement d'une recherche, pour que la
        source puisse capitaliser ce que l'utilisateur a saisi à la main
        (typiquement : mémoriser l'identifiant de lieu fourni pour cette
        ville, afin de ne plus jamais avoir à le redemander).

        Ne doit jamais lever : ce n'est qu'une optimisation, elle ne doit pas
        faire échouer la création de la recherche.
        """
        return

    def to_native(self, criteria: dict) -> dict:
        """Traduit des critères canoniques vers le format de cette source.

        Par défaut : rien à traduire (une source qui se contente de lire
        `locations` et les bornes prix/surface telles quelles). À surcharger
        dès que la source a son propre vocabulaire — c'est ici, et nulle
        part ailleurs, que vivent ses particularités.

        Ne doit jamais modifier `criteria` sur place : les critères sont
        partagés entre toutes les sources d'une même recherche pendant un
        scrape.
        """
        return criteria

    @abstractmethod
    def scrape(self, criteria: dict) -> list[Listing]:
        """
        Scrape listings directly from the source using API/HTTP calls.

        Args:
            criteria: Critères au vocabulaire canonique (core.criteria).

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
            criteria: Critères au vocabulaire canonique (core.criteria).

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
        is the universal location contract — every source is expected to
        work from this, en résolvant elle-même son propre identifiant de
        lieu depuis la localisation canonique (voir to_native).

        Issue #28 : une recherche « transit-seule » est aussi valide. Les
        parsers ne lisent jamais `transit` (l'expansion en fait des
        localisations classiques avant to_native, voir
        services/transit_expansion) : l'accepter ici ne leur fait rien voir,
        ça garantit juste qu'une telle recherche n'est pas rejetée à la
        création/planification alors que le pipeline saura la servir.
        """
        return bool(get_locations(criteria)) or has_transit(criteria)

    def unsupported_criteria(self, criteria: dict) -> list[str]:
        """Les critères demandés que cette source ne sait pas honorer, en
        clair et prêts à afficher (ex. ["les parkings", "les terrains"]).

        Vide quand la source peut tout honorer. Vérifié à partir des
        déclarations SUPPORTED_* plutôt qu'en laissant la traduction
        échouer : c'était le défaut d'avant, où un type de bien non géré
        passait la validation puis levait une ValueError au milieu du
        scrape — et jusque dans la reconstruction d'URL.
        """
        unsupported = []

        transaction = criteria.get("transaction")
        if transaction and transaction not in self.SUPPORTED_TRANSACTIONS:
            label = TRANSACTION_LABELS.get(transaction, transaction)
            unsupported.append(f"la transaction « {label} »")

        for property_type in criteria.get("propertyTypes") or []:
            if property_type not in self.SUPPORTED_PROPERTY_TYPES:
                label = PROPERTY_TYPE_LABELS.get(property_type, property_type)
                unsupported.append(f"les biens de type « {label} »")

        return unsupported

    def cannot_search_reason(self, criteria: dict) -> str | None:
        """None si cette source peut lancer la recherche, sinon pourquoi.

        Point d'entrée unique pour tout ce qui doit décider si une source
        est utilisable : validation à la création d'une recherche (front),
        pipeline de scrape, reconstruction d'URL. Une seule formulation, au
        même endroit.
        """
        if not self.has_valid_criteria(criteria):
            return "aucune localisation exploitable (ville + code postal requis)"

        unsupported = self.unsupported_criteria(criteria)
        if unsupported:
            return f"{self.SOURCE_NAME} ne référence pas {' ni '.join(unsupported)}"

        return None

    def __init_subclass__(cls, **kwargs):
        """Auto-register subclasses that have a SOURCE_ID."""
        super().__init_subclass__(**kwargs)
        if cls.SOURCE_ID:
            ParserRegistry.register(cls)
