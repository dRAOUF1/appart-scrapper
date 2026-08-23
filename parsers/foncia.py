"""Foncia.com listing scraper.

Le site fr.foncia.com est une SPA Angular server-rendered adossée à un hub
d'API JSON publique sans authentification ni anti-bot (POST direct vérifié en
live le 23/08/2026, sans cookies ni tokens), trouvé dans le TransferState des
pages SSR :

    POST https://fnc-api.prod.fonciatech.net/annonces/annonces/search
    {"type": "location",                       <- "location" | "transaction" (achat)
     "filters": {"localities": {"slugs": [...]},
                 "typesBien": ["appartement", ...],
                 "prix": {"min": x, "max": y},      <- optionnel, même en location
                 "surface": {...}, "nbPiece": {...}},
     "size": 250, "page": 2}
    -> {"annonces": [...], "total": N, "count": n}

Contrats vérifiés contre le vivant :

- l'API valide STRICTEMENT les clés de filtres (clé inconnue -> HTTP 400 avec
  message explicite) : aucun filtre fantôme ne peut passer inaperçu ;
- les slugs de localités se COMBINENT EN UNION dans une seule requête,
  tous niveaux mélangés (ville + ville, ville + département vérifiés) —
  jamais d'expansion d'un périmètre en liste de communes ; la résolution
  passe par services/foncia_geocode.py ;
- pagination SERVEUR par `page` (le paramètre au-delà de la fin répond
  gracieusement count=0) ; `size` accepte jusqu'à ~500, 1000 -> 400 ;
- les filtres prix/surface/pièces sont natifs (`prix`, `surface`, `nbPiece`
  en plage {min, max} — nbPiece{min:4} ouvert fonctionne pour le « et plus »)
  et recadrés localement par _passes_filters, filet au cas où le site
  élargirait ;
- `expandNearby` est omis : absent, la réponse n'embarque aucune annonce
  « à proximité » hors périmètre.

Deux limites structurelles :

- LOCATION SEULEMENT : la transaction achat (type:"transaction") existe mais
  n'est pas câblée dans cette première version — SUPPORTED_TRANSACTIONS le
  déclare, le front prévient avant le scrape ;
- nbChambre est sous-rempli côté source (0 sur tous les items capturés) :
  le critère canonique bedrooms n'est donc NI filtré nativement NI localement
  — filtrer éliminerait presque tout le monde sur une donnée absente.

L'URL humaine est un miroir exact du périmètre interrogé : slugs joints par
« -- » et types par « -- » dans le chemin (/location/a--b/appartement--maison),
?advanced= obligatoire (404 sans). Les filtres prix/surface — que le site sait
relire depuis l'URL (?prix=600--900, format généré par son UI) — sont
VOLONTAIREMENT exclus du lien : ouverte à froid (sans session de navigation),
une URL filtrée répond 403 et, répétée, bannit l'IP du visiteur (vérifié le
23/08/2026, y compris sur des liens générés par Foncia lui-même). Ils restent
appliqués au niveau API ; la plage de pièces n'a de toute façon pas de
paramètre d'URL confirmé. Dit dans URL_NOTE.
"""

from __future__ import annotations

import json
from urllib.parse import urlencode

import requests
from loguru import logger

from core.criteria import (
    APARTMENT,
    HOUSE,
    LAND,
    PARKING,
    PROPERTY_TYPE_LABELS,
    RENT,
    matches_locations,
    source_overrides,
)
from core.geocode import REGION, region_departments
from models.listing import Listing
from parsers._dates import normaliser_creation_date
from parsers.base import BaseParser, ParserRegistry, get_locations

BASE_URL = "https://fr.foncia.com"
SEARCH_API_URL = "https://fnc-api.prod.fonciatech.net/annonces/annonces/search"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Vocabulaire natif du site (clés = vocabulaire canonique core.criteria).
# Valeurs validées par l'énum stricte de l'API (terrain renvoie total=0 à
# Toulouse mais est une valeur acceptée, pas un 400).
TRANSACTION_VALUES = {RENT: "location"}
TYPE_VALUES = {
    APARTMENT: "appartement",
    HOUSE: "maison",
    PARKING: "parking",
    LAND: "terrain",
}

# Pagination serveur : size élevé accepté (500 OK, 1000 -> 400) mais on
# reste prudent ; MAX_PAGES borne la boucle (8 x 250 = 2000 annonces par
# requête — largement au-dessus de l'inventaire Foncia d'un département
# entier, observé ~300). Au-delà, troncature loggée, jamais masquée.
PAGE_SIZE = 250
MAX_PAGES = 8


def _transaction(criteria: dict) -> str:
    """La transaction canonique demandée. La location par défaut : c'est ce
    que le formulaire propose en premier, et une recherche sans transaction
    explicite n'a jamais voulu dire « achat »."""
    transaction = criteria.get("transaction")
    return transaction if transaction in TRANSACTION_VALUES else RENT


def _property_types(criteria: dict) -> list[str]:
    """Les types de bien canoniques que Foncia sait traiter parmi ceux
    demandés.

    - Aucun type demandé -> appartement, le défaut du formulaire.
    - Types demandés hors capacités -> liste vide, et surtout PAS le défaut
      appartement : renvoyer des appartements à qui demande autre chose
      serait un faux résultat.
    """
    requested = criteria.get("propertyTypes") or []
    if not requested:
        return [APARTMENT]
    return [t for t in requested if t in TYPE_VALUES]


def _rooms_range(criteria: dict) -> tuple[int, int | None] | None:
    """La plage de pièces à envoyer au site, depuis le canonique, ou None si
    elle n'est pas exprimable nativement.

    Le canonique est une liste d'égalités où toute valeur >= 5 vaut « et
    plus » ; le site attend une PLAGE {min, max}. Une demande contiguë se
    traduit fidèlement ([2,3] -> {2..3}, [4,5+] -> min 4 sans max) ; une
    demande non contiguë ([1,3]) n'est PAS élargie en plage qui ramènerait
    du 2 pièces — elle part sans filtre natif et le filtrage exact reste
    assuré par _passes_filters."""
    values: set[int] = set()
    expanded_plus = False
    for room in criteria.get("rooms") or []:
        try:
            value = int(room)
        except (TypeError, ValueError):
            continue
        if value >= 5:
            expanded_plus = True
            continue
        if value > 0:
            values.add(value)

    if not values and not expanded_plus:
        return None
    if not values:
        return (5, None)
    ordered = sorted(values)
    if any(b - a != 1 for a, b in zip(ordered, ordered[1:], strict=False)):
        return None
    return (ordered[0], None) if expanded_plus else (ordered[0], ordered[-1])


def _range_filter(bounds: tuple[int, int | None] | None) -> dict | None:
    """Une plage canonique -> objet {min, max} du site (max omis si ouvert)."""
    if bounds is None:
        return None
    minimum, maximum = bounds
    payload: dict = {"min": minimum}
    if maximum is not None:
        payload["max"] = maximum
    return payload


def _search_filters(types: list[str], criteria: dict, slugs: list[str]) -> dict:
    """Les filtres EXACTS envoyés à /annonces/search pour cette requête.

    Miroir de scrape() : les slugs portent le périmètre, typesBien les types
    demandés, prix/surface/nbPiece les bornes canoniques quand elles sont
    exprimables nativement."""
    filters: dict = {
        "localities": {"slugs": list(slugs)},
        "typesBien": [TYPE_VALUES[t] for t in types],
    }
    price_min = criteria.get("priceMin")
    price_max = criteria.get("priceMax")
    if price_min or price_max:
        price: dict = {}
        if price_min:
            price["min"] = price_min
        if price_max:
            price["max"] = price_max
        filters["prix"] = price
    surface_min = criteria.get("surfaceMin")
    surface_max = criteria.get("surfaceMax")
    if surface_min or surface_max:
        surface: dict = {}
        if surface_min:
            surface["min"] = surface_min
        if surface_max:
            surface["max"] = surface_max
        filters["surface"] = surface
    rooms = _range_filter(_rooms_range(criteria))
    if rooms:
        filters["nbPiece"] = rooms
    return filters


def _search_query_params() -> list[tuple[str, str]]:
    """Les paramètres de requête de l'URL humaine : ?advanced= seul.

    Le site SAIT relire ses filtres depuis l'URL (`prix=250--1300`,
    `surface=60--170` — son UI génère ces liens, et la traduction query ->
    POST est vérifiée côté SSR pour prix) MAIS on ne les met PAS dans le
    lien : ouverte à froid (sans la session de navigation du site), une URL
    filtrée répond 403 — y compris pour des liens générés par Foncia
    lui-même, vérifié le 23/08/2026 — et les répétitions escaladent vers un
    ban complet de l'IP côté visiteur. Un lien « Voir l'URL » qui bannit
    l'utilisateur serait pire qu'un lien sans filtres ; les bornes restent
    appliquées au niveau API, dit dans URL_NOTE."""
    return [("advanced", "")]


def _search_body(types: list[str], criteria: dict, slugs: list[str], page: int = 1) -> dict:
    """Le corps EXACT du POST envoyé à l'API (page incluse)."""
    body: dict = {
        "type": TRANSACTION_VALUES[_transaction(criteria)],
        "filters": _search_filters(types, criteria, slugs),
        "size": PAGE_SIZE,
    }
    if page > 1:
        body["page"] = page
    return body


def _passes_filters(listing: Listing, criteria: dict, locations: list[dict]) -> bool:
    """Recadre localement ce que le site a renvoyé : localisation + bornes
    prix/surface/pièces.

    Le site filtre déjà nativement (filtres de _search_filters), mais ce
    double contrôle garde le scraper honnête si son API élargit un jour —
    même logique qu'Orpi/Laforêt/Century21. Les chambres sont volontairement
    ignorées : nbChambre est sous-rempli côté source (voir docstring module),
    les écarter reviendrait à jeter presque tout sur une donnée absente."""
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

    allowed = _rooms_range(criteria)
    if allowed and listing.rooms:
        try:
            room_count = int(float(listing.rooms))
        except (TypeError, ValueError):
            return True  # pièce illisible : on garde, jamais d'exclusion sournoise
        minimum, maximum = allowed
        if room_count > 0:
            if room_count < minimum:
                return False
            if maximum is not None and room_count > maximum:
                return False
        # nbPiece absent ou 0 (sous-remplissage observé) : seul le contrôle
        # de localisation/prix/surface s'applique, la pièce n'est pas devinable.

    return True


def _canonical_type(type_bien: str | None) -> str | None:
    """Le type canonique depuis le vocabulaire natif (inverse de TYPE_VALUES)."""
    for canonical, native in TYPE_VALUES.items():
        if native == type_bien:
            return canonical
    return None


def _detail_url(item: dict) -> str:
    """L'URL de détail : canonicalUrl fourni tel quel par l'API
    (« /location/toulouse-31200/appartement/331698636.htm » — le slug porte
    la localité PRÉCISE de l'annonce, parfois différente de celui de la
    recherche), préfixé du domaine."""
    canonical = item.get("canonicalUrl") or ""
    if not canonical:
        return ""
    return f"{BASE_URL}{canonical}" if canonical.startswith("/") else canonical


def _dict_to_listing(item: dict) -> Listing | None:
    """Convertit un item de /annonces/search en Listing.

    En location le prix vit dans `loyer` (pas de champ prix). Pas de nom
    d'agence dans l'API (seulement numeroAgence) : agency reste vide.
    datePublication est ISO 8601 tz-aware, normalisée en ISO-8601 UTC
    canonique (issue #12) par normaliser_creation_date."""
    reference = str(item.get("reference") or "")
    url = _detail_url(item)
    if not reference or not url:
        return None

    localisation = item.get("localisation") or {}
    locality = localisation.get("locality") or {}
    city = localisation.get("ville") or ""
    arrondissement = str(locality.get("arrondissement") or "")

    canonical_type = _canonical_type(item.get("typeBien"))
    property_type = PROPERTY_TYPE_LABELS.get(canonical_type, "") if canonical_type else ""

    photos = [
        p for p in (item.get("mediasCDN") or item.get("medias") or [])
        if isinstance(p, str) and p
    ]
    loyer = item.get("loyer")
    surface_fields = item.get("surface") or {}
    surface = surface_fields.get("habitable")
    if surface is None:
        surface = surface_fields.get("totale")
    rooms = item.get("nbPiece")

    title_parts = [property_type]
    try:
        if rooms is not None and int(rooms) > 0:
            title_parts.append(f"{int(rooms)} pièce{'s' if int(rooms) > 1 else ''}")
    except (TypeError, ValueError):
        pass
    title_parts.append(city.title() if city else "")

    display_name = locality.get("libelleDisplay") or city

    return Listing(
        listing_id=f"foncia_{reference}",
        url=url,
        title=" · ".join(part for part in title_parts if part),
        price=f"{int(loyer)} €" if loyer is not None else "",
        surface=str(surface) if surface is not None else "",
        rooms=str(rooms) if rooms else "",
        location=display_name.title(),
        image_url=photos[0] if photos else "",
        description=(item.get("description") or "")[:300],
        agency="",
        source="foncia",
        legacy_id=reference,
        price_value=float(loyer) if loyer is not None else None,
        city=city.title(),
        district=arrondissement.title(),
        zip_code=str(localisation.get("codePostal") or ""),
        property_type=property_type,
        is_exclusive=bool(item.get("exclusivite")),
        has_3d_visit=bool(item.get("urlVisite360")),
        creation_date=normaliser_creation_date(item.get("datePublication")),
        epc=item.get("noteConsoEnergie") or "",
        ges=item.get("noteEmissionGES") or "",
        photos=json.dumps(
            [{"url": photo_url, "alt": "", "key": ""} for photo_url in photos]
        ),
    )


@ParserRegistry.register
class FonciaParser(BaseParser):
    """Scrape Foncia via son API JSON publique fnc-api.prod.fonciatech.net
    (pas d'anti-bot sur ce hub, vérifié en direct)."""

    SOURCE_ID = "foncia"
    SOURCE_NAME = "Foncia"
    SOURCE_DESCRIPTION = "Foncia.com — API JSON publique (villes, départements, régions)"

    # Location seulement dans cette première version : l'achat existe côté
    # site (type:"transaction") mais n'est pas câblé. SUPPORTED_TRANSACTIONS
    # le déclare — le front prévient AVANT le scrape (unsupported_criteria).
    SUPPORTED_TRANSACTIONS = (RENT,)

    MANUAL_OVERRIDE_LABEL = "slug(s) de localité Foncia (optionnel)"
    MANUAL_OVERRIDE_HELP = (
        "Repli si la résolution automatique échoue : le ou les slugs de "
        "localité Foncia, séparés par des virgules (ex. « toulouse-31, "
        "vannes-56000 »). Visibles dans l'URL d'une recherche faite sur "
        "fr.foncia.com."
    )

    URL_NOTE = (
        "Ce lien porte exactement le périmètre et les types de biens "
        "interrogés par le scraper (les slugs y sont joints par « -- », "
        "comme dans la requête réelle). Les bornes prix/surface/pièces sont "
        "appliquées au niveau de l'API Foncia : ne pas les recopier dans "
        "l'URL est voulu — ouverte hors session du site, une URL filtrée "
        "répond 403 (WAF) et répétée, bannit l'adresse du visiteur."
    )

    def _geo_repo(self):
        """Le cache persistant slugs <- périmètre, ou None s'il n'y a pas de
        storage — jamais lu depuis `flask.current_app` : le scraping s'exécute
        sur un thread de fond, hors contexte d'application."""
        return getattr(self.storage, "foncia_geo", None) if self.storage else None

    def _slugs(self, criteria: dict, locations: list[dict]) -> list[tuple[dict, str]]:
        """Les couples (localisation, slug) à interroger : les slugs collés à
        la main s'il y en a, sinon ceux résolus par services.foncia_geocode
        (tous les niveaux canoniques y sont dérivables et vérifiés).

        Une région part dans UNE valeur locations.slugs (« Occitanie » ->
        `occitanie`) — sa copie porte toujours ses codes de départements,
        dont le filtrage par préfixes postaux en aval (matches_locations sur
        une région) a besoin ; jamais mutés, les critères étant partagés
        entre sources pendant un scrape. Un périmètre non résolu est écarté
        avec un avertissement clair."""
        manual = source_overrides(criteria, self.SOURCE_ID).get("slugs")
        if manual:
            # L'utilisateur peut coller plus ou moins de slugs que de villes :
            # zip strict=False épouse ce qu'il y a, dans l'ordre.
            return list(zip(locations, manual, strict=False))

        repo = self._geo_repo()
        if repo is None:
            logger.warning(
                "[Foncia] Aucun storage fourni au parser, résolution du slug impossible "
                "(voir get_parser(source, storage=...))"
            )
            return []

        from services import foncia_geocode

        resolved: list[tuple[dict, str]] = []
        for location in locations:
            target = location
            if location.get("kind") == REGION:
                # La copie porte TOUJOURS les codes de départements de la
                # région : matches_locations sur une région lit
                # `departments`, qui manque quand la région a été construite
                # à la main sans sa liste.
                codes = list(location.get("departments") or []) or region_departments(
                    location.get("code") or ""
                )
                target = {**location, "departments": codes}

            slug = foncia_geocode.resolve_slug_id(target, repo=repo)
            if slug:
                resolved.append((target, slug))
            else:
                logger.warning(
                    f"[Foncia] Aucun slug résolu pour {foncia_geocode._describe(target)}"
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

        from services import foncia_geocode

        try:
            foncia_geocode.remember_manual_slugs(criteria, repo=repo)
        except Exception as e:
            logger.debug(f"[Foncia] Slug manuel non mémorisé : {e}")

    def to_native(self, criteria: dict) -> dict:
        """Rien à traduire : les bornes prix/surface/pièces de Foncia parlent
        déjà canonique (plages min/max natives), et les localisations passent
        par la résolution slug de services.foncia_geocode."""
        return criteria

    def has_valid_criteria(self, criteria: dict) -> bool:
        """Utilisable dès qu'il y a un slug manuel, ou au moins un périmètre
        identifiable : tout niveau canonique est dérivable — la commune a
        besoin de son code postal (garanti par normalize_locations), les
        autres niveaux de leur code, le nom officiel manquant étant demandé
        à geo.api.gouv.fr par services.foncia_geocode."""
        if source_overrides(criteria, self.SOURCE_ID).get("slugs"):
            return True
        return bool(get_locations(criteria))

    def build_search_url(self, criteria: dict) -> str | None:
        urls = self.build_search_urls(criteria)
        return urls[0] if urls else None

    def build_search_urls(self, criteria: dict) -> list[str]:
        """L'URL humaine réellement équivalente au scrape : UNE seule URL où
        les slugs ET les types sont joints par « -- » (format natif du site :
        le SSR de /location/a--b/appartement--maison?advanced= envoie
        exactement notre requête d'union, vérifié en direct).

        Les filtres prix/surface n'y figurent PAS volontairement : ouverte
        à froid, une URL filtrée répond 403 (WAF du site) et escalade vers
        un ban de l'IP du visiteur — voir _search_query_params et
        URL_NOTE."""
        locations = get_locations(criteria)
        if not locations:
            return []
        types = _property_types(criteria)
        if not types:
            return []

        resolved = self._slugs(criteria, locations)
        if not resolved:
            return []
        slugs_joined = "--".join(slug for _, slug in resolved)
        types_joined = "--".join(TYPE_VALUES[t] for t in types)
        query = urlencode(_search_query_params())
        return [f"{BASE_URL}/location/{slugs_joined}/{types_joined}?{query}"]

    def scrape(self, criteria: dict) -> list[Listing]:
        # Une transaction explicite non supportée est refusée AVANT toute
        # autre chose : la convertir en location serait un faux résultat
        # (le défaut RENT ne s'applique qu'à une recherche sans préférence).
        requested_transaction = criteria.get("transaction")
        if requested_transaction and requested_transaction not in self.SUPPORTED_TRANSACTIONS:
            raise ValueError(
                f"Foncia ne référence que la location, pas la transaction "
                f"« {requested_transaction} »"
            )
        locations = get_locations(criteria)
        if not locations:
            raise ValueError(
                "Foncia nécessite au moins une localisation (ville + code postal) dans les critères"
            )
        types = _property_types(criteria)
        if not types:
            raise ValueError(
                "Foncia ne référence aucun des types de bien demandés "
                f"(uniquement {sorted(TYPE_VALUES)})"
            )

        resolved = self._scraped_locations(criteria, locations)
        slugs = [slug for _, slug in resolved]
        scoped_locations = [loc for loc, _ in resolved]

        session = requests.Session()
        session.headers.update({
            "User-Agent": DESKTOP_UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        seen: set[str] = set()
        listings: list[Listing] = []
        total: int | None = None
        fetched = 0
        skipped_type = 0
        page = 1
        while page <= MAX_PAGES:
            body = _search_body(types, criteria, slugs, page=page)
            try:
                resp = session.post(SEARCH_API_URL, json=body, timeout=20)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                raise ValueError(
                    f"Requête Foncia échouée ({', '.join(slugs)}, page {page}): {e}"
                ) from e
            annonces = data.get("annonces")
            if not isinstance(annonces, list):
                raise ValueError(
                    f"Réponse inattendue de Foncia ({', '.join(slugs)}) : "
                    "JSON sans liste annonces"
                )
            if total is None and isinstance(data.get("total"), int):
                total = data["total"]

            fetched += len(annonces)
            for item in annonces:
                reference = str(item.get("reference") or "")
                if not reference or reference in seen:
                    continue
                seen.add(reference)
                if item.get("status") and item.get("status") != "active":
                    continue
                if item.get("typeBien") not in TYPE_VALUES.values():
                    skipped_type += 1
                    continue
                listing = _dict_to_listing(item)
                if listing and _passes_filters(listing, criteria, scoped_locations):
                    listings.append(listing)

            if len(annonces) < PAGE_SIZE:
                break
            page += 1

        if total is not None and total > fetched:
            logger.warning(
                f"[Foncia] Résultat tronqué par le cap client ({MAX_PAGES * PAGE_SIZE} "
                f"annonces max) : {total} annoncées, {fetched} récupérées — affiner "
                "les critères pour tout voir"
            )
        if skipped_type:
            logger.debug(f"[Foncia] {skipped_type} item(s) hors types demandés écartés")
        logger.info(f"[Foncia] Scraping terminé : {len(listings)} annonces uniques")
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
                "Aucune localisation Foncia exploitable : aucun périmètre n'a pu "
                "être résolu en slug (voir les logs, ou utiliser la surcharge "
                "manuelle de slugs)"
            )
        return resolved
