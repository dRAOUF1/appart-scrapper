"""SeLoger.com listing scraper.

Traduit les critères canoniques (core.criteria) vers le format attendu par
SeLoger — voir to_native(). Le seul point qui demande du travail est la
localisation : SeLoger ne cherche pas par ville mais par `placeId` opaque
(ex. AD08FR31096), qu'aucune formule ne dérive d'un code INSEE. Il est donc
résolu depuis la localisation canonique via l'endpoint d'autocomplete du
site, avec un cache persistant en base (voir services.seloger_geocode).

Le placeId collé à la main reste accepté comme repli, rangé dans
criteria["sourceOverrides"]["seloger"] : il a priorité sur la résolution
automatique et évite un appel réseau inutile.
"""

from __future__ import annotations

import json

from loguru import logger

from core.criteria import source_overrides
from models.listing import Listing
from parsers.base import BaseParser, ParserRegistry, get_locations


def _dict_to_listing(data: dict) -> Listing:
    """Convert a scraped dict into a Listing object."""
    photos = data.get("photos", [])
    phone = data.get("phone", [])

    image_url = ""
    if photos and isinstance(photos, list) and len(photos) > 0:
        first_photo = photos[0]
        if isinstance(first_photo, dict):
            image_url = first_photo.get("url", first_photo.get("source", ""))
        elif isinstance(first_photo, str):
            image_url = first_photo

    return Listing(
        listing_id=f"sl_{data.get('id', '')}",
        url=data.get("url", ""),
        title=data.get("title", ""),
        price=data.get("price", ""),
        surface=str(data["surface"]) if data.get("surface") is not None else "",
        rooms=str(data["rooms"]) if data.get("rooms") is not None else "",
        location=data.get("city", "") or data.get("district", ""),
        image_url=image_url,
        description=data.get("description", "")[:300] if data.get("description") else "",
        agency=data.get("agency", ""),
        source="seloger",
        legacy_id=str(data.get("legacyId", "")),
        price_value=data.get("priceValue"),
        price_details=data.get("priceDetails", ""),
        city=data.get("city", ""),
        district=data.get("district", ""),
        zip_code=data.get("zipCode", ""),
        property_type=data.get("propertyType", ""),
        is_private=data.get("isPrivate", False),
        phone=json.dumps(phone),
        epc=data.get("epc", ""),
        ges=data.get("ges", ""),
        is_new=data.get("isNew", False),
        is_exclusive=data.get("isExclusive", False),
        has_3d_visit=data.get("has3DVisit", False),
        creation_date=data.get("creationDate", ""),
        update_date=data.get("updateDate", ""),
        headline=data.get("headline", ""),
        photos=json.dumps(photos),
    )


# Vocabulaire canonique -> vocabulaire SeLoger.
_TRANSACTIONS = {"rent": "Rent", "buy": "Sale"}
_PROPERTY_TYPES = {
    "apartment": "Apartment",
    "house": "House",
    "parking": "Parking",
    "land": "Land",
}


@ParserRegistry.register
class SeLogerParser(BaseParser):
    """Scrape SeLoger via sa page de résultats (données JSON embarquées)."""

    SOURCE_ID = "seloger"
    SOURCE_NAME = "SeLoger"
    SOURCE_DESCRIPTION = "SeLoger.com — scraping de la page de résultats"

    # SeLoger couvre les quatre types de bien et les deux transactions :
    # SUPPORTED_TRANSACTIONS / SUPPORTED_PROPERTY_TYPES gardent donc leur
    # valeur par défaut (tout).

    MANUAL_OVERRIDE_LABEL = "URL de recherche ou Place ID SeLoger (optionnel)"
    MANUAL_OVERRIDE_HELP = (
        "Inutile en principe : le lieu est résolu automatiquement depuis la "
        "ville. À ne renseigner que si une recherche SeLoger ne trouve rien — "
        "collez une URL de recherche SeLoger ou un Place ID (ex. AD08FR31096)."
    )

    def parse_manual_override(self, value: str) -> dict:
        """Accepte soit une URL de recherche SeLoger complète (les critères y
        sont extraits, seuls les placeIds sont retenus — le reste vient du
        formulaire), soit une liste de Place IDs séparés par des virgules."""
        value = (value or "").strip()
        if not value:
            return {}

        if value.startswith("http"):
            from scraper.seloger import parse_search_url

            place_ids = parse_search_url(value).get("placeIds") or []
        else:
            place_ids = [p.strip() for p in value.split(",") if p.strip()]

        return {"placeIds": place_ids} if place_ids else {}

    def remember_manual_override(self, criteria: dict) -> None:
        """Banque le placeId saisi à la main contre le code INSEE de sa ville,
        pour que la résolution automatique en profite ensuite — une saisie
        manuelle n'a ainsi jamais à être refaite pour la même ville."""
        repo = self._geo_repo()
        if repo is None:
            return

        from services import seloger_geocode

        try:
            seloger_geocode.remember_manual_place_id(criteria, repo=repo)
        except Exception as e:
            logger.debug(f"[SeLoger] placeId manuel non mémorisé : {e}")

    # ------------------------------------------------------------------
    # Traduction canonique -> SeLoger
    # ------------------------------------------------------------------

    def _geo_repo(self):
        """Le cache persistant placeId <- code INSEE, ou None s'il n'y a pas de
        storage.

        Volontairement lu depuis `self.storage` (injecté, voir
        BaseParser.__init__) et jamais depuis `flask.current_app` : le scraping
        s'exécute sur un thread de fond, hors contexte d'application — s'y
        appuyer faisait échouer tous les scrapes automatiques de SeLoger.
        """
        return getattr(self.storage, "seloger_geo", None) if self.storage else None

    def _place_ids(self, criteria: dict) -> list[str]:
        """Les placeIds à utiliser : ceux collés à la main s'il y en a, sinon
        ceux résolus depuis les localisations canoniques.

        Une localisation qui ne se résout pas est simplement absente du
        résultat (elle est déjà loguée par seloger_geocode) : mieux vaut
        chercher sur les villes qui ont fonctionné que d'échouer en entier.
        Si plus rien ne reste, l'appelant traite ça comme une erreur — voir
        scrape() : une recherche SeLoger sans aucun placeId n'est pas une
        recherche vide, c'est une recherche sur la France entière.
        """
        manual = source_overrides(criteria, self.SOURCE_ID).get("placeIds")
        if manual:
            return list(manual)

        locations = get_locations(criteria)
        if not locations:
            return []

        repo = self._geo_repo()
        if repo is None:
            logger.warning(
                "[SeLoger] Aucun storage fourni au parser, résolution du placeId impossible "
                "(voir get_parser(source, storage=...))"
            )
            return []

        from services import seloger_geocode

        place_ids = []
        for location in locations:
            # Un identifiant par périmètre, quel que soit son niveau : SeLoger
            # en a un pour une région comme pour un code postal, et il couvre
            # tout le périmètre à lui seul.
            place_id = seloger_geocode.resolve_place_id(location, repo=repo)
            if place_id and place_id not in place_ids:
                place_ids.append(place_id)
        return place_ids

    def to_native(self, criteria: dict) -> dict:
        """Critères canoniques -> critères SeLoger.

        Les clés SeLoger (`placeIds`, `distributionTypes`, `estateTypes`,
        `spaceMin`/`spaceMax`) sont construites ici et nulle part ailleurs :
        c'est ce qui permet au reste de l'application de ne connaître que le
        vocabulaire canonique.
        """
        native: dict = {}

        place_ids = self._place_ids(criteria)
        if place_ids:
            native["placeIds"] = place_ids

        transaction = _TRANSACTIONS.get(criteria.get("transaction"))
        if transaction:
            native["distributionTypes"] = [transaction]

        estate_types = [
            _PROPERTY_TYPES[t]
            for t in criteria.get("propertyTypes") or []
            if t in _PROPERTY_TYPES
        ]
        if estate_types:
            native["estateTypes"] = estate_types

        for native_key, canonical_key in (
            ("priceMin", "priceMin"),
            ("priceMax", "priceMax"),
            ("spaceMin", "surfaceMin"),
            ("spaceMax", "surfaceMax"),
        ):
            if criteria.get(canonical_key) is not None:
                native[native_key] = criteria[canonical_key]

        for key in ("rooms", "bedrooms"):
            if criteria.get(key):
                # SeLoger attend des chaînes dans son query string.
                native[key] = [str(v) for v in criteria[key]]

        # Surcharges manuelles autres que placeIds (ex.
        # locationsInBuildingExcluded, récupéré d'une URL collée).
        for key, value in source_overrides(criteria, self.SOURCE_ID).items():
            if key != "placeIds" and value:
                native[key] = value

        return native

    # ------------------------------------------------------------------
    # Scraping
    # ------------------------------------------------------------------

    def scrape(self, criteria: dict) -> list[Listing]:
        """Exécute le scraping pour les critères canoniques donnés.

        Les erreurs réelles (réseau, anti-bot, format inattendu) remontent à
        l'appelant au lieu d'être masquées en résultat vide, pour que
        ScrapeService puisse distinguer un échec d'une recherche légitimement
        sans résultat.
        """
        native = self.to_native(criteria)
        if not native.get("placeIds"):
            raise ValueError(
                "Aucun lieu SeLoger n'a pu être déterminé pour cette recherche "
                "(résolution automatique échouée et aucun Place ID fourni)"
            )

        from scraper.seloger import scrape as do_scrape

        detailed = do_scrape(native)
        listings = [_dict_to_listing(d) for d in detailed]

        seen: set[str] = set()
        unique: list[Listing] = []
        for li in listings:
            if li.listing_id not in seen:
                seen.add(li.listing_id)
                unique.append(li)

        logger.info(f"[SeLoger] Scraping terminé : {len(unique)} annonces uniques")
        return unique

    def parse(self, html: str) -> list[Listing]:
        """Legacy: kept for interface compatibility but raises."""
        raise NotImplementedError(
            "SeLogerParser.parse() n'est plus supporté. Utilisez scrape(criteria) à la place."
        )

    def build_search_url(self, criteria: dict) -> str | None:
        """L'URL de recherche SeLoger reconstruite, ou None.

        Renvoie None plutôt qu'une URL sans `locations=` quand aucun placeId
        n'a pu être déterminé : une telle URL est une recherche sur la France
        entière, qui aurait l'air d'un lien légitime alors qu'elle ne
        correspond pas du tout à la recherche de l'utilisateur.
        """
        from scraper.seloger import build_search_url

        native = self.to_native(criteria)
        if not native.get("placeIds"):
            return None
        return build_search_url(native, order="DateDesc")

    def has_valid_criteria(self, criteria: dict) -> bool:
        """Utilisable dès qu'il y a un placeId manuel, ou au moins un périmètre
        identifiable dont on saura résoudre le placeId.

        La résolution elle-même est tentée au moment du scrape, pas ici : la
        création d'une recherche ne doit pas dépendre d'un appel réseau à
        SeLoger.
        """
        if source_overrides(criteria, self.SOURCE_ID).get("placeIds"):
            return True

        from services.seloger_geocode import area_cache_key

        return any(area_cache_key(loc) for loc in get_locations(criteria))

    def cannot_search_reason(self, criteria: dict) -> str | None:
        """Même contrat que BaseParser, avec un message qui explique le repli
        possible quand le périmètre n'est pas identifiable."""
        if not self.has_valid_criteria(criteria):
            if get_locations(criteria):
                return (
                    "la localisation n'a pas de code INSEE (choisissez-la dans "
                    "la liste de suggestions, ou renseignez un Place ID SeLoger)"
                )
            return "aucune localisation exploitable (ville + code postal requis)"

        unsupported = self.unsupported_criteria(criteria)
        if unsupported:
            return f"{self.SOURCE_NAME} ne référence pas {' ni '.join(unsupported)}"
        return None
