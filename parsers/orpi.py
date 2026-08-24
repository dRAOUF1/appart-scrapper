"""Orpi.com listing scraper.

Contrairement à Laforêt/Century 21 (HTML server-rendered), Orpi expose une
API AJAX publique qui renvoie directement du JSON, trouvée par inspection du
bundle JS de la page de résultats et vérifiée en direct le 22/08/2026 sans
session ni authentification :

    GET https://www.orpi.com/recherche/ajax?transactions[]=buy&...&locations[0][value]=<slug>
    -> {"items": [...], "count": 16885, ...}

Chaque item porte déjà tout ce qu'il faut (pas de parsing HTML) :
id/référence UUID, type, transaction, price, surface, nbRooms, city{name},
department{name}, longAd, onMarketSince, estatePhotos, agency.name,
isExclusive. Le slug de l'annonce sert d'URL de détail après préfixe
(`/annonce-vente-{slug}/` ou `/annonce-location-{slug}/`, formats vérifiés)
et embarque le code postal (`...-rosny-sous-bois-93110-<uuid>`), seul moyen
de le connaître : l'API ne renvoie PAS de champ CP, or matches_locations en
a besoin pour cadrer les résultats.

Deux limites structurelles vérifiées en direct :

- CAP DE 500 ANNONCES PAR REQUÊTE : la pagination du site est purement
  client-side sur ces 500 items, aucun paramètre serveur (page/limit testés,
  ignorés) n'en renvoie davantage. Une recherche dont count dépasse 500 est
  donc tronquée — signalée en log, jamais masquée ;
- pas de tri ni de fenêtre : la requête rend « les » annonces du périmètre,
  dans un ordre propre au site.

Les localisations se COMBINENT EN UNION dans une seule requête
(locations[N][value], niveaux ville/département/région mélangés vérifiés :
Rosny + Paris = 443 annonces, Rosny + Gironde = 768) — jamais d'expansion
d'un périmètre en liste de communes. Les filtres prix/surface/pièces sont
natifs côté site (minPrice/maxPrice, minSurface/maxSurface,
numbersOfRooms[] en ÉGALITÉ multiple — vérifié : rooms=3,4 ne rend que des
3 et 4 pièces) ; le canonique « 5 pièces » signifiant « 5 et plus », toute
valeur >= 5 est émise comme {5..8} et le filtrage exact reste appliqué côté
scraper par _passes_filters, filet au cas où le site élargirait.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode

import requests
from loguru import logger

from core.criteria import (
    APARTMENT,
    BUY,
    HOUSE,
    LAND,
    PARKING,
    PROPERTY_TYPE_LABELS,
    RENT,
    matches_locations,
    source_overrides,
)
from core.geocode import CITY, REGION, region_departments
from models.listing import Listing
from parsers._coords import PRECISION_APPROXIMATIVE, PRECISION_EXACTE, extraire_coordonnees
from parsers._dates import normaliser_creation_date
from parsers.base import BaseParser, ParserRegistry, get_locations

BASE_URL = "https://www.orpi.com"
SEARCH_AJAX_URL = f"{BASE_URL}/recherche/ajax"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Vocabulaire natif du site (clés = vocabulaire canonique core.criteria).
# Valeurs vérifiées en direct : une valeur inconnue fait répondre une page
# d'erreur HTML à la place du JSON (« neuf », « parking »).
TRANSACTION_VALUES = {RENT: "rent", BUY: "buy"}
TYPE_VALUES = {
    APARTMENT: "appartement",
    HOUSE: "maison",
    PARKING: "stationnement",
    LAND: "terrain",
}

# Cap dur serveur (pagination client-side uniquement, voir docstring module).
MAX_RESULTS = 500

# Le canonique « 5 » signifie « 5 et plus » (voir search_edit.html) alors que
# numbersOfRooms[] est une égalité : une demande de 5+ est émise {5..8} —
# au-delà de 8 pièces, l'inventaire ORPI est négligeable — puis recadré par
# _passes_filters.
MAX_ROOM_VALUE = 8

_DETAIL_PREFIX = {BUY: "annonce-vente", RENT: "annonce-location"}

# Le code postal est incrusté dans le slug entre la ville et la référence
# (« appartement-t2-rosny-sous-bois-93110-bace612c-... », présent sur 50/50
# items d'une capture réelle). La référence n'est pas toujours un UUID :
# certaines agences en émettent une numérique (« ...-prugna-20166-492-
# 018097-660 »), l'ancre se limite donc au groupe de 5 chiffres lui-même.
# Une commune ne contient jamais cinq chiffres consécutifs dans son slug,
# et un faux positif échouerait fermé (CP hors périmètre) — jamais faux
# positif de cadrage.
_ZIP_FROM_SLUG_RE = re.compile(r"-(\d{5})-")


def _transaction(criteria: dict) -> str:
    """La transaction canonique demandée. La location par défaut : c'est ce
    que le formulaire propose en premier, et une recherche sans transaction
    explicite n'a jamais voulu dire « achat »."""
    transaction = criteria.get("transaction")
    return transaction if transaction in TRANSACTION_VALUES else RENT


def _property_types(criteria: dict) -> list[str]:
    """Les types de bien canoniques qu'Orpi sait traiter parmi ceux demandés.

    - Aucun type demandé -> appartement, le défaut du formulaire.
    - Types demandés hors capacités -> liste vide, et surtout PAS le défaut
      appartement : renvoyer des appartements à qui demande autre chose
      serait un faux résultat. (Cas théorique ici : SUPPORTED_PROPERTY_TYPES
      couvre tout le canonique, mais la règle reste symétrique des autres
      sources.)
    """
    requested = criteria.get("propertyTypes") or []
    if not requested:
        return [APARTMENT]
    return [t for t in requested if t in TYPE_VALUES]


def _rooms_values(criteria: dict) -> list[int]:
    """Les nombres de pièces à envoyer au site, depuis le canonique.

    Le canonique est une liste d'égalités où « 5 » vaut « 5 et plus » ; le
    site attend des égalités. Chaque valeur demandée est envoyée telle quelle
    si < 5 ; dès qu'une valeur >= 5 apparaît, elle devient {5..8}. Le
    filtrage exact reste assuré par _passes_filters."""
    rooms = criteria.get("rooms") or []
    values: set[int] = set()
    expanded_plus = False
    for room in rooms:
        try:
            value = int(room)
        except (TypeError, ValueError):
            continue
        if value >= 5:
            expanded_plus = True
        elif value > 0:
            values.add(value)
    if expanded_plus:
        values.update(range(5, MAX_ROOM_VALUE + 1))
    return sorted(values)


def _search_params(criteria: dict, slugs: list[str]) -> list[tuple[str, str]]:
    """Les paramètres EXACTS envoyés à /recherche/ajax pour cette recherche.

    Miroir fidèle de scrape() : build_search_urls() reconstruit l'URL humaine
    /recherche?<ces mêmes paramètres>, server-rendered avec le même résultat
    (vérifié en direct), donc « Voir l'URL » montre la vérité.

    `slugs` est ordonné : locations[0][value]... locations[n][value].
    """
    params: list[tuple[str, str]] = [
        ("transactions[]", TRANSACTION_VALUES[_transaction(criteria)])
    ]
    for property_type in _property_types(criteria):
        params.append(("realEstateTypes[]", TYPE_VALUES[property_type]))
    for slug in slugs:
        params.append(("locations[][value]", slug))

    if criteria.get("priceMin"):
        params.append(("minPrice", str(criteria["priceMin"])))
    if criteria.get("priceMax"):
        params.append(("maxPrice", str(criteria["priceMax"])))
    if criteria.get("surfaceMin"):
        params.append(("minSurface", str(criteria["surfaceMin"])))
    if criteria.get("surfaceMax"):
        params.append(("maxSurface", str(criteria["surfaceMax"])))
    for value in _rooms_values(criteria):
        params.append(("numbersOfRooms[]", str(value)))
    return params


def _zip_from_slug(slug: str) -> str:
    """Le code postal incrusté dans le slug d'annonce, ou '' — matches_locations
    échoue fermé sur un code vide : une annonce illisible n'est jamais
    supposée être dans le périmètre (même politique que Laforêt)."""
    m = _ZIP_FROM_SLUG_RE.search(slug or "")
    return m.group(1) if m else ""


def _coords_orpi(item: dict) -> tuple[tuple[float, float] | None, str]:
    """Les coordonnées natives d'un item /recherche/ajax, et leur précision.

    L'API porte latitude/longitude directs, plus un champ `blurredness`
    (valeurs entières observées en direct : 1 = position floutée au niveau de
    la rue, 2 = encore plus grossière) — toute valeur non nulle signifie que
    le point ne désigne pas le bien lui-même : 'approximative'. Une position
    illisible ou sentinelle (0.0) est rejetée par extraire_coordonnees.
    """
    coords = extraire_coordonnees(item.get("latitude"), item.get("longitude"))
    if not coords:
        return None, ""
    precision = PRECISION_APPROXIMATIVE if item.get("blurredness") else PRECISION_EXACTE
    return coords, precision


def _detail_url(item: dict) -> str:
    """L'URL de détail d'après la transaction : `/annonce-vente-{slug}/` ou
    `/annonce-location-{slug}/` (formats vérifiés en direct, HTTP 200)."""
    prefix = _DETAIL_PREFIX.get(item.get("transaction"), "annonce-vente")
    return f"{BASE_URL}/{prefix}-{item.get('slug', '')}/"


def _passes_filters(listing: Listing, criteria: dict, locations: list[dict]) -> bool:
    """Recadre localement ce que le site a renvoyé : localisation + bornes
    prix/surface/pièces.

    Le site filtre déjà nativement (paramètres de _search_params), mais ce
    double contrôle garde le scraper honnête si son API élargit un jour —
    même logique que Laforêt/Century21, sources chez qui ce filet est déjà
    intervenu. `locations` sont les périmètres canoniques couverts par LA
    requête (une région y figure avec ses départements résolus)."""
    if not matches_locations(listing.zip_code, locations):
        return False

    price_min = criteria.get("priceMin")
    price_max = criteria.get("priceMax")
    if listing.price_value is not None:
        if price_min and listing.price_value < price_min:
            return False
        if price_max and listing.price_value > price_max:
            return False

    surface_min = criteria.get("surfaceMin")
    surface_max = criteria.get("surfaceMax")
    try:
        surface = float(listing.surface.replace(",", ".")) if listing.surface else None
    except ValueError:
        surface = None
    if surface is not None:
        if surface_min and surface < surface_min:
            return False
        if surface_max and surface > surface_max:
            return False

    allowed = _rooms_values(criteria)
    # Sans expansion « 5+ », allowed est l'égalité stricte demandée.
    if allowed and listing.rooms:
        try:
            room_count = int(float(listing.rooms))
        except (TypeError, ValueError):
            return True
        max_requested = max(int(r) for r in criteria.get("rooms") or [0])
        if max_requested >= 5:
            return room_count >= min(allowed)
        return room_count in allowed

    return True


def _dict_to_listing(item: dict) -> Listing:
    """Convertit un item de /recherche/ajax en Listing.

    Pas de champ CP dans l'API : extrait du slug (voir _zip_from_slug).
    Le titre est reconstitué — l'API n'en fournit pas — à partir du type,
    du nombre de pièces et de la localité, comme fait la carte du site."""
    reference = str(item.get("reference") or item.get("id") or "")
    slug = item.get("slug") or ""
    city = (item.get("city") or {}).get("name") or ""
    district = (item.get("district") or {}).get("name") or ""
    location_name = item.get("locationDescription") or city
    price = item.get("price")
    surface = item.get("surface")
    rooms = item.get("nbRooms")

    property_type = {
        "appartement": PROPERTY_TYPE_LABELS[APARTMENT],
        "maison": PROPERTY_TYPE_LABELS[HOUSE],
        "stationnement": PROPERTY_TYPE_LABELS[PARKING],
        "terrain": PROPERTY_TYPE_LABELS[LAND],
    }.get(item.get("type"), "")

    photos = [
        p.get("fullUrl") or p.get("url") or ""
        for p in (item.get("estatePhotos") or [])
        if isinstance(p, dict)
    ]
    title_parts = [property_type]
    try:
        if rooms is not None and int(rooms) > 0:
            title_parts.append(f"{int(rooms)} pièce{'s' if int(rooms) > 1 else ''}")
    except (TypeError, ValueError):
        pass
    title_parts.append(location_name)

    coords, precision = _coords_orpi(item)

    return Listing(
        listing_id=f"orpi_{reference}",
        url=_detail_url(item),
        title=" · ".join(part for part in title_parts if part),
        price=f"{int(price)} €" if price is not None else "",
        surface=str(surface) if surface is not None else "",
        rooms=str(rooms) if rooms is not None else "",
        location=location_name,
        image_url=photos[0] if photos else "",
        description=(item.get("longAd") or "")[:300],
        agency=(item.get("agency") or {}).get("name") or "",
        source="orpi",
        legacy_id=reference,
        price_value=float(price) if price is not None else None,
        city=city,
        district=district,
        zip_code=_zip_from_slug(slug),
        property_type=property_type,
        is_exclusive=bool(item.get("isExclusive")),
        creation_date=normaliser_creation_date(item.get("onMarketSince")),
        photos=json.dumps(
            [{"url": url, "alt": "", "key": ""} for url in photos if url]
        ),
        latitude=coords[0] if coords else None,
        longitude=coords[1] if coords else None,
        location_precision=precision,
    )


@ParserRegistry.register
class OrpiParser(BaseParser):
    """Scrape Orpi via son API AJAX publique /recherche/ajax (pas d'anti-bot
    sur ce site, vérifié en direct)."""

    SOURCE_ID = "orpi"
    SOURCE_NAME = "Orpi"
    SOURCE_DESCRIPTION = "Orpi.com — API AJAX JSON (villes, départements, régions)"

    # Orpi couvre tout le canonique : appartement, maison, parking
    # (stationnement), terrain — et les deux transactions. SUPPORTED_*
    # gardent donc leur défaut : rien à restreindre.

    MANUAL_OVERRIDE_LABEL = "slug(s) Orpi (optionnel)"
    MANUAL_OVERRIDE_HELP = (
        "Repli si la résolution automatique échoue : le ou les slugs de lieu "
        "d'Orpi, séparés par des virgules (ex. « rosny-sous-bois, gironde »). "
        "Visibles dans l'URL d'une recherche faite sur orpi.com."
    )

    URL_NOTE = (
        "Ce lien est la page de recherche d'Orpi rendue côté serveur ; elle "
        "porte exactement les paramètres interrogés par le scraper. Au-delà "
        "de 500 annonces sur un même périmètre, Orpi tronque son résultat "
        "(limite propre au site) : affiner les critères pour tout voir."
    )

    def _geo_repo(self):
        """Le cache persistant slugs <- périmètre, ou None s'il n'y a pas de
        storage — jamais lu depuis `flask.current_app` : le scraping s'exécute
        sur un thread de fond, hors contexte d'application."""
        return getattr(self.storage, "orpi_geo", None) if self.storage else None

    def _slugs(self, criteria: dict, locations: list[dict]) -> list[tuple[dict, str]]:
        """Les couples (localisation, slug) à interroger : les slugs collés à
        la main s'il y en a, sinon ceux résolus depuis les localisations.

        Une région se résout d'abord par son nom (« Île-de-France » ->
        `ile-de-france`, identifiant natif du site) ; sans nom, elle est
        élargie à ses départements dont chaque slug est résolu individuellement
        — tous partent quand même dans UNE requête (union native du site),
        jamais en liste de communes. Un périmètre non résolu est écarté avec un
        avertissement clair."""
        manual = source_overrides(criteria, self.SOURCE_ID).get("slugs")
        if manual:
            # L'utilisateur peut coller plus ou moins de slugs que de villes :
            # zip strict=False épouse ce qu'il y a, dans l'ordre.
            return list(zip(locations, manual, strict=False))

        repo = self._geo_repo()
        if repo is None:
            logger.warning(
                "[Orpi] Aucun storage fourni au parser, résolution du slug impossible "
                "(voir get_parser(source, storage=...))"
            )
            return []

        from services import orpi_geocode

        def resolve(target: dict) -> str | None:
            slug = orpi_geocode.resolve_slug_id(target, repo=repo)
            if not slug:
                logger.warning(
                    f"[Orpi] Aucun slug résolu pour {orpi_geocode._describe(target)}"
                )
            return slug

        resolved: list[tuple[dict, str]] = []
        for location in locations:
            if location.get("kind") != REGION:
                slug = resolve(location)
                if slug:
                    resolved.append((location, slug))
                continue

            from core.geocode import DEPARTMENT

            # La copie porte TOUJOURS les codes de départements de la région :
            # le filtrage par préfixes postaux en aval (matches_locations sur
            # une région) lit `departments`, qui manque quand la région a été
            # résolue par nom ou dont la liste a dû être demandée à l'API geo.
            # Jamais mutés : les critères sont partagés entre sources pendant
            # un scrape.
            codes = list(location.get("departments") or []) or region_departments(
                location.get("code") or ""
            )
            if not codes:
                logger.warning(
                    f"[Orpi] {orpi_geocode._describe(location)} sans départements "
                    "identifiables, ignorée"
                )
                continue
            scoped = {**location, "departments": codes}

            if location.get("name"):
                # La région a un identifiant natif (« Corse » -> `corse`,
                # « Île-de-France » -> `ile-de-france`, vérifiés en direct) :
                # UNE valeur locations[][value], pas une par département.
                slug = resolve(scoped)
                if slug:
                    resolved.append((scoped, slug))
                continue

            targets = [{"kind": DEPARTMENT, "code": code} for code in codes]
            slugs = [s for target in targets if (s := resolve(target))]
            for slug in slugs:
                resolved.append((scoped, slug))
            if not slugs:
                logger.warning(
                    f"[Orpi] {orpi_geocode._describe(location)} sans départements "
                    "résolus, ignorée"
                )
        return resolved

    def parse_manual_override(self, value: str) -> dict:
        value = (value or "").strip()
        if not value:
            return {}
        slugs = [s.strip() for s in value.split(",") if s.strip()]
        return {"slugs": slugs} if slugs else {}

    def remember_manual_override(self, criteria: dict) -> None:
        """Banque le(s) slug(s) saisi(s) à la main contre le périmètre de la
        recherche, pour que la résolution automatique en profite ensuite."""
        repo = self._geo_repo()
        if repo is None:
            return

        from services import orpi_geocode

        try:
            orpi_geocode.remember_manual_slugs(criteria, repo=repo)
        except Exception as e:
            logger.debug(f"[Orpi] Slug manuel non mémorisé : {e}")

    def to_native(self, criteria: dict) -> dict:
        """Rien à traduire : les filtres prix/surface/pièces d'Orpi parlent
        déjà canonique (bornes min/max natives), et les localisations passent
        par la résolution slug de services.orpi_geocode."""
        return criteria

    def has_valid_criteria(self, criteria: dict) -> bool:
        """Utilisable dès qu'il y a un slug manuel, ou au moins un périmètre
        identifiable (ville, ville entière, département, région avec nom).

        Une région SANS nom n'est pas résoluble directement mais l'est via ses
        départements : elle reste valide si elle porte sa liste (ou son code,
        pour la demander à l'API geo)."""
        if source_overrides(criteria, self.SOURCE_ID).get("slugs"):
            return True

        locations = get_locations(criteria)
        if any(loc.get("kind", CITY) != REGION for loc in locations):
            return True
        return any(
            loc.get("kind") == REGION
            and (
                loc.get("name")
                or loc.get("departments")
                or loc.get("code")
            )
            for loc in locations
        )

    def build_search_url(self, criteria: dict) -> str | None:
        urls = self.build_search_urls(criteria)
        return urls[0] if urls else None

    def build_search_urls(self, criteria: dict) -> list[str]:
        """L'URL humaine réellement équivalente au scrape : la page
        /recherche?… d'Orpi est server-rendered avec EXACTEMENT les mêmes
        paramètres et le même résultat que l'endpoint AJAX (vérifié en direct
        — count identique), et accepte plusieurs localisations dans une seule
        URL. Une seule URL donc, miroir fidèle de scrape(), pas une par
        ville."""
        locations = get_locations(criteria)
        if not locations:
            return []
        if not _property_types(criteria):
            return []

        resolved = self._slugs(criteria, locations)
        if not resolved:
            return []
        query = urlencode(_search_params(criteria, [slug for _, slug in resolved]))
        return [f"{BASE_URL}/recherche?{query}"]

    def scrape(self, criteria: dict) -> list[Listing]:
        locations = get_locations(criteria)
        if not locations:
            raise ValueError(
                "Orpi nécessite au moins une localisation (ville + code postal) dans les critères"
            )
        if not _property_types(criteria):
            raise ValueError(
                "Orpi ne référence aucun des types de bien demandés "
                f"(uniquement {sorted(TYPE_VALUES)})"
            )

        resolved = self._scraped_locations(criteria, locations)
        slugs = [slug for _, slug in resolved]
        params = _search_params(criteria, slugs)

        session = requests.Session()
        session.headers.update({
            "User-Agent": DESKTOP_UA,
            "Accept": "application/json",
        })
        try:
            resp = session.get(SEARCH_AJAX_URL, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            raise ValueError(f"Requête Orpi échouée ({', '.join(slugs)}): {e}") from e
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError(
                f"Réponse inattendue d'Orpi ({', '.join(slugs)}) : JSON sans items"
            )

        items = data["items"]
        total = data.get("count")
        if isinstance(total, int) and total > len(items):
            logger.warning(
                f"[Orpi] Résultat tronqué par le site : {total} annonces sur ce "
                f"périmètre, {len(items)} renvoyées (cap {MAX_RESULTS}) — "
                "affiner les critères pour tout voir"
            )

        seen: set[str] = set()
        listings: list[Listing] = []
        skipped_type = 0
        for item in items:
            reference = str(item.get("reference") or item.get("id") or "")
            if not reference or reference in seen:
                continue
            seen.add(reference)
            # Biens vendus/désactivés ou hors types demandés (le site peut
            # glisser des programmes neufs dans les réponses larges).
            if item.get("sold") or item.get("enabled") is False:
                continue
            if item.get("type") not in TYPE_VALUES.values():
                skipped_type += 1
                continue
            listing = _dict_to_listing(item)
            if _passes_filters(listing, criteria, [loc for loc, _ in resolved]):
                listings.append(listing)

        if skipped_type:
            logger.debug(f"[Orpi] {skipped_type} item(s) hors types demandés écartés")
        logger.info(f"[Orpi] Scraping terminé : {len(listings)} annonces uniques")
        return listings

    def _scraped_locations(
        self, criteria: dict, locations: list[dict]
    ) -> list[tuple[dict, str]]:
        """La résolution des slugs pour scrape() : comme _slugs, mais échoue
        bruyamment (ValueError) quand AUCUN périmètre n'est exploitable —
        une recherche sans identifiant de lieu serait silencieusement une
        recherche nationale, jamais une recherche vide."""
        resolved = self._slugs(criteria, locations)
        if not resolved:
            raise ValueError(
                "Aucune localisation Orpi exploitable : aucun périmètre n'a pu "
                "être résolu en slug (voir les logs, ou utiliser la surcharge "
                "manuelle de slugs)"
            )
        return resolved
