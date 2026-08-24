"""BienIci.com listing scraper — API JSON publique.

Traduit les critères canoniques (core.criteria) vers le format attendu par
bienici — voir to_native(). Comme SeLoger, bienici ne cherche pas par ville
mais par un identifiant opaque (`zoneId`, ex. "-7444"), non dérivable d'un
code INSEE par formule : il est résolu depuis la localisation canonique via
l'endpoint public d'autocomplete du site, avec un cache persistant en base
(voir services.bienici_geocode).

Le(s) zoneId(s) collé(s) à la main restent acceptés comme repli, rangés dans
criteria["sourceOverrides"]["bienici"] : ils ont priorité sur la résolution
automatique et évitent un appel réseau inutile.
"""

from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import urlencode

from loguru import logger

from core.criteria import source_overrides
from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY
from models.listing import Listing
from parsers._coords import PRECISION_APPROXIMATIVE, PRECISION_EXACTE, extraire_coordonnees
from parsers._dates import normaliser_creation_date
from parsers.base import BaseParser, ParserRegistry, get_locations, has_transit

BASE_URL = "https://www.bienici.com"

# Vocabulaire canonique -> vocabulaire bienici. filterType est déjà identique
# au canonique (rent/buy) : gardé sous forme de table pour rester explicite
# et symétrique avec _PROPERTY_TYPES plutôt que de s'y fier implicitement.
_TRANSACTIONS = {"rent": "rent", "buy": "buy"}
_PROPERTY_TYPES = {
    "apartment": "flat",
    "house": "house",
    "parking": "parking",
    "land": "land",
}

# Chemin /recherche/{transaction}/{périmètre}/{type}[/{n}-pieces-et-plus].
# Vérifié en direct (URLs réelles indexées de bienici.com, ex.
# bienici.com/recherche/achat/gironde-33/maisonvilla,
# bienici.com/recherche/location/ile-de-france/appartement/2-pieces-et-plus).
_TRANSACTION_SLUGS = {"rent": "location", "buy": "achat"}
_TYPE_SLUGS = {"apartment": "appartement", "house": "maisonvilla", "parking": "parking", "land": "terrain"}


def _whole_city_postal_code(postal_codes: list[str]) -> str | None:
    """Le code postal générique d'une ville entière, pour l'ancre d'URL.

    bienici identifie « toute la ville » par le code du département suivi de
    zéros (`paris-75000`, confirmé en direct), jamais par le premier
    arrondissement trié (`75001`) : ce dernier désigne un arrondissement
    précis, pas la ville entière, et produisait un lien pour « Paris 1er »
    quand l'utilisateur avait choisi « Paris » (toute la ville). Les DOM ont
    un code département à 3 chiffres (`971`...`976`), le reste à 2."""
    if not postal_codes:
        return None
    postal_code = postal_codes[0]
    prefix_len = 3 if postal_code[:2] in ("97", "98") else 2
    return postal_code[:prefix_len].ljust(5, "0")


def _slugify(text: str) -> str:
    """Lowercase, strip accents, non-alnum -> '-' (même fonction que
    parsers.laforet._slugify, dupliquée ici plutôt que partagée : la seule
    autre source à en avoir besoin utilise déjà la sienne, une abstraction
    commune pour deux appelants n'apporterait rien)."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()

# accountType observés en direct sur de vraies annonces (agence, réseau de
# mandataires, mandataire indépendant) : tout le reste (aucune valeur
# "personal"/"individual" rencontrée dans nos échantillons, bienici agrège
# surtout des pros) est traité comme un particulier plutôt que de deviner une
# liste fermée qui pourrait exclure un cas réel non encore vu.
_PRO_ACCOUNT_TYPES = {"agency", "network", "mandatary"}


# Types de blurInfo observés en direct (realEstateAds.json, 23/08/2026) :
# « disk » = position floutée dans un disque de ~50 m autour du bien ;
# « cityOrArrondissement » = centre de la commune/arrondissement. Dans les DEUX
# cas le point ne désigne pas le bien lui-même : précision 'approximative'
# (rendu en cercle translucide côté carte). Tout autre type — une position
# non floutée n'ayant aucune raison de porter un blurInfo — resterait
# 'exacte' par défaut.
_FLOUS_BIENICI = {"disk", "cityOrArrondissement"}


def _coords_bienici(data: dict) -> tuple[tuple[float, float] | None, str]:
    """Les coordonnées natives d'une annonce bienici, et leur précision.

    realEstateAds.json porte blurInfo.position.lat/lon ; quand il est absent,
    l'annonce n'a pas de coordonnée exploitable (None, pas d'invention).
    """
    blur_info = data.get("blurInfo") or {}
    position = blur_info.get("position") or {}
    coords = extraire_coordonnees(position.get("lat"), position.get("lon"))
    if not coords:
        return None, ""
    precision = (
        PRECISION_APPROXIMATIVE if blur_info.get("type") in _FLOUS_BIENICI else PRECISION_EXACTE
    )
    return coords, precision


def _format_price(price, transaction_type: str) -> str:
    if price is None:
        return ""
    formatted = f"{int(price):,}".replace(",", " ")
    return f"{formatted} €/mois" if transaction_type == "rent" else f"{formatted} €"


def _dict_to_listing(data: dict) -> Listing:
    """Convertit une annonce brute de scraper.bienici en Listing."""
    photos = [
        {"url": p.get("url") or p.get("url_photo", "")}
        for p in (data.get("photos") or [])
        if isinstance(p, dict)
    ]
    image_url = photos[0]["url"] if photos else ""
    district = data.get("district") or {}
    price = data.get("price")
    ad_id = data.get("id", "")
    coords, precision = _coords_bienici(data)

    return Listing(
        listing_id=f"bi_{ad_id}",
        # L'API ne renvoie aucune URL de fiche annonce ; /annonce/{id} est le
        # format vérifié en direct (le shell SPA sert n'importe quel id à cet
        # endroit, le routing réel se fait côté client sur l'id seul, jamais
        # sur le slug de ville/type qui l'accompagne d'ordinaire).
        url=f"https://www.bienici.com/annonce/{ad_id}" if ad_id else "",
        title=data.get("title") or "",
        price=_format_price(price, data.get("transactionType")),
        surface=str(data["surfaceArea"]) if data.get("surfaceArea") is not None else "",
        rooms=str(data["roomsQuantity"]) if data.get("roomsQuantity") is not None else "",
        location=data.get("city") or district.get("name", "") or "",
        image_url=image_url,
        description=(data.get("description") or "")[:300],
        agency=data.get("accountDisplayName") or "",
        source="bienici",
        legacy_id=str(data.get("reference") or ""),
        price_value=float(price) if price is not None else None,
        city=data.get("city") or "",
        district=district.get("name", "") or "",
        zip_code=data.get("postalCode") or "",
        property_type=data.get("propertyType") or "",
        is_private=data.get("accountType") not in _PRO_ACCOUNT_TYPES,
        epc=data.get("energyClassification") or "",
        ges=data.get("greenhouseGazClassification") or "",
        is_new=bool(data.get("newProperty", False)),
        is_exclusive=bool(data.get("isBienIciExclusive", False)),
        has_3d_visit=bool(data.get("with3dModel", False)),
        creation_date=normaliser_creation_date(data.get("publicationDate")),
        update_date=data.get("modificationDate") or "",
        photos=json.dumps(photos),
        latitude=coords[0] if coords else None,
        longitude=coords[1] if coords else None,
        location_precision=precision,
    )


@ParserRegistry.register
class BienIciParser(BaseParser):
    """Scrape bienici.com via son API JSON publique (realEstateAds.json)."""

    SOURCE_ID = "bienici"
    SOURCE_NAME = "Bien'ici"
    SOURCE_DESCRIPTION = "Bienici.com — API JSON publique"

    # bienici couvre les quatre types de bien et les deux transactions :
    # SUPPORTED_TRANSACTIONS / SUPPORTED_PROPERTY_TYPES gardent donc leur
    # valeur par défaut (tout).

    MANUAL_OVERRIDE_LABEL = "zoneId(s) bienici (optionnel)"
    MANUAL_OVERRIDE_HELP = (
        "Inutile en principe : le lieu est résolu automatiquement depuis la "
        "ville. À ne renseigner que si une recherche bienici ne trouve rien — "
        "collez un ou plusieurs zoneId séparés par des virgules (ex. -7444, "
        "trouvable dans les outils de développement du navigateur sur une "
        "recherche bienici.com)."
    )

    def parse_manual_override(self, value: str) -> dict:
        value = (value or "").strip()
        if not value:
            return {}
        zone_ids = [z.strip() for z in value.split(",") if z.strip()]
        return {"zoneIds": zone_ids} if zone_ids else {}

    def remember_manual_override(self, criteria: dict) -> None:
        """Banque le(s) zoneId(s) saisi(s) à la main contre le code INSEE de
        sa ville, pour que la résolution automatique en profite ensuite."""
        repo = self._geo_repo()
        if repo is None:
            return

        from services import bienici_geocode

        try:
            bienici_geocode.remember_manual_zone_ids(criteria, repo=repo)
        except Exception as e:
            logger.debug(f"[BienIci] zoneId manuel non mémorisé : {e}")

    # ------------------------------------------------------------------
    # Traduction canonique -> bienici
    # ------------------------------------------------------------------

    def _geo_repo(self):
        """Le cache persistant zoneIds <- périmètre, ou None s'il n'y a pas
        de storage — jamais lu depuis `flask.current_app` : le scraping
        s'exécute sur un thread de fond, hors contexte d'application."""
        return getattr(self.storage, "bienici_geo", None) if self.storage else None

    def _zone_ids(self, criteria: dict) -> list[str]:
        """Les zoneIds à utiliser : ceux collés à la main s'il y en a, sinon
        ceux résolus depuis les localisations canoniques.

        Une localisation qui ne se résout pas est simplement absente du
        résultat, comme SeLogerParser._place_ids() : mieux vaut chercher sur
        les villes qui ont fonctionné que d'échouer en entier."""
        manual = source_overrides(criteria, self.SOURCE_ID).get("zoneIds")
        if manual:
            return list(manual)

        locations = get_locations(criteria)
        if not locations:
            return []

        repo = self._geo_repo()
        if repo is None:
            logger.warning(
                "[BienIci] Aucun storage fourni au parser, résolution du zoneId impossible "
                "(voir get_parser(source, storage=...))"
            )
            return []

        from services import bienici_geocode

        zone_ids: list[str] = []
        for location in locations:
            for zone_id in bienici_geocode.resolve_zone_ids(location, repo=repo) or []:
                if zone_id not in zone_ids:
                    zone_ids.append(zone_id)
        return zone_ids

    def to_native(self, criteria: dict) -> dict:
        """Critères canoniques -> critères bienici.

        Les clés bienici (`filterType`, `propertyType`, `zoneIdsByTypes`,
        `minArea`/`maxArea`, ...) sont construites ici et nulle part
        ailleurs — c'est ce qui permet au reste de l'application de ne
        connaître que le vocabulaire canonique."""
        native: dict = {"onTheMarket": [True]}

        zone_ids = self._zone_ids(criteria)
        if zone_ids:
            native["zoneIdsByTypes"] = {"zoneIds": zone_ids}

        transaction = _TRANSACTIONS.get(criteria.get("transaction"))
        if transaction:
            native["filterType"] = transaction

        property_types = [
            _PROPERTY_TYPES[t]
            for t in criteria.get("propertyTypes") or []
            if t in _PROPERTY_TYPES
        ]
        if property_types:
            native["propertyType"] = property_types

        for native_key, canonical_key in (
            ("minPrice", "priceMin"),
            ("maxPrice", "priceMax"),
            ("minArea", "surfaceMin"),
            ("maxArea", "surfaceMax"),
        ):
            if criteria.get(canonical_key) is not None:
                native[native_key] = criteria[canonical_key]

        # bienici ne connaît qu'un intervalle min/max, pas une liste de
        # valeurs exactes comme SeLoger (`rooms: ["2", "3"]`) : une sélection
        # à cases cochées non contiguës (ex. 2 et 5 pièces) se traduit donc
        # en un intervalle plus large (2 à 5) — traduction imparfaite mais
        # acceptée, aucune formule ne peut faire mieux avec ce vocabulaire.
        for canonical_key, min_key, max_key in (
            ("rooms", "minRooms", "maxRooms"),
            ("bedrooms", "minBedrooms", "maxBedrooms"),
        ):
            values = criteria.get(canonical_key)
            if values:
                native[min_key] = min(values)
                native[max_key] = max(values)

        return native

    def _url_anchor(self, location: dict) -> str | None:
        """Le segment de chemin identifiant le périmètre, ou None si ce
        périmètre ne peut pas être reconstruit.

        Vérifié en direct sur des URLs bienici.com réellement indexées :

            commune      {slug(ville)}-{code postal}       montrouge-92120
            ville entière {slug(ville)}-{code générique}    paris-75000
            département  {slug(nom)}-{code département}     gironde-33
            région       {slug(nom)}, SANS code             ile-de-france

        Le nom du département/région vient de l'autocomplete
        (core.geocode._department_suggestions/_region_suggestions) et
        survit à la normalisation des critères (core.criteria) : il est de
        toute façon nécessaire pour l'affichage (location_label), donc
        toujours présent en pratique pour une localisation choisie dans les
        suggestions."""
        kind = location.get("kind", CITY)
        if kind == CITY:
            city, postal_code = location.get("city"), location.get("postalCode")
            return f"{_slugify(city)}-{postal_code}" if city and postal_code else None
        if kind == WHOLE_CITY:
            city = location.get("city")
            postal_codes = location.get("postalCodes") or []
            code = _whole_city_postal_code(postal_codes)
            return f"{_slugify(city)}-{code}" if city and code else None
        if kind == DEPARTMENT:
            name, code = location.get("name"), location.get("code")
            return f"{_slugify(name)}-{code}" if name and code else None
        if kind == REGION:
            name = location.get("name")
            return _slugify(name) if name else None
        return None

    def _url_filters(self, criteria: dict) -> list[tuple[str, str]]:
        """Les filtres prix/surface/tri de l'URL, en query string.

        Ordre et présence de chaque clé vérifiés en direct sur un lien réel
        de recherche bienici (`?prix-min=650&prix-max=950&surface-min=18&
        surface-max=40&tri=publication-desc`) : `tri` est en dernier, pas en
        tête. `tri` trie par date de publication décroissante : cette
        source est un tracker de nouveautés, le lien doit montrer ce que le
        scraper lit. Pas de filtre chambres : aucune preuve qu'il existe
        côté URL, contrairement aux pièces (voir le segment
        `{n}-pieces-et-plus` de build_search_url)."""
        params: list[tuple[str, str]] = []

        for query_key, canonical_key in (
            ("prix-min", "priceMin"),
            ("prix-max", "priceMax"),
            ("surface-min", "surfaceMin"),
            ("surface-max", "surfaceMax"),
        ):
            value = criteria.get(canonical_key)
            if value is not None:
                params.append((query_key, str(value)))

        params.append(("tri", "publication-desc"))
        return params

    def build_search_url(self, criteria: dict) -> str | None:
        """L'URL de recherche bienici reconstruite à titre indicatif, ou None.

        Toutes les localisations canoniques reconstructibles (voir
        `_url_anchor`) sont jointes par des virgules dans le même segment de
        chemin — vérifié en direct sur un lien réel couvrant deux communes
        (`recherche/location/montrouge-92120,paris-75000/appartement`) :
        contrairement à ce qu'un chemin `/recherche/{transaction}/{périmètre}
        /{type}` unique pourrait laisser croire, bienici accepte plusieurs
        périmètres nommés dans une seule ancre, à la manière du `locations=`
        de SeLoger plutôt que du `filter[cities][]` répété de Laforet."""
        anchors = []
        for loc in get_locations(criteria):
            anchor = self._url_anchor(loc)
            if anchor is not None and anchor not in anchors:
                anchors.append(anchor)
        if not anchors:
            return None

        transaction = _TRANSACTION_SLUGS.get(criteria.get("transaction"), "location")
        property_types = criteria.get("propertyTypes") or []
        type_slug = _TYPE_SLUGS.get(property_types[0], "appartement") if property_types else "appartement"

        # `{n}-pieces-et-plus` : vérifié en direct sur une vraie URL bienici
        # (recherche/location/ile-de-france/appartement/2-pieces-et-plus).
        # N'ajoute rien en dessous de 2 : aucune preuve de la forme prise
        # pour un studio (un segment distinct existe mais sa sémantique
        # exacte n'a pas été vérifiée), mieux vaut l'omettre que deviner.
        rooms = criteria.get("rooms") or []
        rooms_segment = f"/{min(rooms)}-pieces-et-plus" if rooms and min(rooms) >= 2 else ""

        query = urlencode(self._url_filters(criteria))
        anchor = ",".join(anchors)

        return f"{BASE_URL}/recherche/{transaction}/{anchor}/{type_slug}{rooms_segment}?{query}"

    # ------------------------------------------------------------------
    # Scraping
    # ------------------------------------------------------------------

    def scrape(self, criteria: dict) -> list[Listing]:
        """Exécute le scraping pour les critères canoniques donnés.

        Les erreurs réelles (réseau, format inattendu) remontent à l'appelant
        au lieu d'être masquées en résultat vide, pour que ScrapeService
        puisse distinguer un échec d'une recherche légitimement sans
        résultat — même contrat que SeLogerParser.scrape()."""
        native = self.to_native(criteria)
        if not native.get("zoneIdsByTypes", {}).get("zoneIds"):
            raise ValueError(
                "Aucune zone bienici n'a pu être déterminée pour cette recherche "
                "(résolution automatique échouée et aucun zoneId fourni)"
            )

        from scraper.bienici import scrape as do_scrape

        raw = do_scrape(native)
        listings = [_dict_to_listing(d) for d in raw]

        seen: set[str] = set()
        unique: list[Listing] = []
        for li in listings:
            if li.listing_id not in seen:
                seen.add(li.listing_id)
                unique.append(li)

        logger.info(f"[BienIci] Scraping terminé : {len(unique)} annonces uniques")
        return unique

    def has_valid_criteria(self, criteria: dict) -> bool:
        """Utilisable dès qu'il y a un zoneId manuel, ou au moins un
        périmètre identifiable dont on saura résoudre le zoneId.

        La résolution elle-même est tentée au moment du scrape, pas ici : la
        création d'une recherche ne doit pas dépendre d'un appel réseau à
        bienici — même contrat que SeLogerParser.has_valid_criteria()."""
        if source_overrides(criteria, self.SOURCE_ID).get("zoneIds"):
            return True

        from services.bienici_geocode import area_cache_key

        return any(area_cache_key(loc) for loc in get_locations(criteria))

    def cannot_search_reason(self, criteria: dict) -> str | None:
        """Même contrat que BaseParser.

        Pas de message spécifique « pas de code INSEE » : `area_cache_key`
        résout désormais aussi bien par code postal/nom de ville que par
        code INSEE (voir services.bienici_geocode), donc toute localisation
        qui passe `get_locations` (ville + code postal, ou périmètre large)
        satisfait déjà `has_valid_criteria`.

        Fallback #28 : une recherche « transit-seule » reste cherchable —
        l'expansion produira ses localisations classiques avant `to_native`."""
        if not self.has_valid_criteria(criteria) and not has_transit(criteria):
            return "aucune localisation exploitable (ville + code postal requis)"

        unsupported = self.unsupported_criteria(criteria)
        if unsupported:
            return f"{self.SOURCE_NAME} ne référence pas {' ni '.join(unsupported)}"
        return None
