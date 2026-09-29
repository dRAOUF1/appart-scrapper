"""esset-pm.com (Esset Property Management) — API JSON publique.

Esset PM gère des lots résidentiels en location (ex-Foncia/Humakey, groupe
Emeria) : le portail www.locations.esset-pm.com est une SPA React servie par
S3+CloudFront qui charge sa configuration au runtime depuis /conf.json — c'est
là que vit l'API, publique et sans authentification :

    https://bo-back.esset-pm.com/v1/public/api

Vérifié en direct le 23/08/2026 :

    POST /offre            corps JSON, renvoie TOUTES les offres du périmètre
                           en une réponse (pas de pagination serveur, ~90 lots)
    GET  /offre/{code}     la fiche complète d'une offre
    IMG  https://img.prod.fonciatech.net/esset{chemin_photo}

Le corps de recherche porte un SEUL périmètre, identifié par des codes
OFFICIELS tous dérivables directement du vocabulaire canonique — aucun cache
ni résolveur propre nécessaire (contrairement aux placeIds SeLoger ou aux
geoIds PAP) :

    typeLieu "v" + codeLieu <code postal>          (city, et whole_city par CP)
    typeLieu "d" + codeLieu <code département>     (department — INSEE direct)
    typeLieu "r" + codeLieu <code région>          (region — INSEE direct)

Trois pièges vérifiés en direct, qui dictent la conduite du scrape :

- le champ `budget` du corps est IGNORÉ par l'API (budget [100000, 200000]
  renvoie les mêmes loyers de 754 € que sans filtre) : comme les filtres de
  surface/pièces/chambres, il est donc appliqué ici même, sur chaque offre ;
- les drapeaux appartement/maison/parking/studio..f5 ne sont pas vérifiables
  sur un parc 100 % appartements (les maisons/parkings existent dans l'UI,
  aucun lot actif) : plutôt que risquer une EXCLUSION silencieuse côté serveur
  (irrécupérable localement), le POST part avec tous les drapeaux à false —
  comportement « pas de filtre » vérifié (renvoie tout) — et TOUT est rejoué
  localement par _passes_filters ;
- un codeLieu inconnu renvoie HTTP 200 [] (résultat vide légitime, pas une
  erreur) ; en revanche `codeDepartement` arrive bourré d'espaces (« 75 » »)
  dans les réponses — à stripper avant toute comparaison.

Anti-bot : CloudFront filtre les requêtes trop nues (403 sur conf.json avec un
User-Agent minimal) mais rien contre un client honnête : en-têtes navigateur
complets + retries suffisent, pas besoin d'empreinte TLS (curl_cffi).

Le portail ne référence QUE de la location (aucune vente) ; l'UI propose
appartement/maison/parking. La page /notre-offre ne encode AUCUN critère dans
son URL (l'état passe par l'history state de React Router) : « Voir l'URL »
pointe donc la page de recherche réelle, et la note d'URL l'explique.

La fiche publique d'une offre est /location/{codeAnnonce}
(codeAnnonce « 42107-54 », préfixe de déduplication `essetpm_`).
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.parse

import requests
from loguru import logger

from core.criteria import (
    APARTMENT,
    HOUSE,
    PARKING,
    RENT,
    matches_locations,
)
from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY
from models.listing import Listing
from parsers._coords import PRECISION_EXACTE, extraire_coordonnees
from parsers._dates import DATE_INCONNUE
from parsers.base import BaseParser, ParserRegistry, get_locations

BASE_URL = "https://www.locations.esset-pm.com"
API_URL = "https://bo-back.esset-pm.com/v1/public/api"
IMG_URL = "https://img.prod.fonciatech.net/esset"

# En-têtes navigateur complets : CloudFront répond 403 aux requêtes trop nues
# (vérifié en direct sur conf.json avec un User-Agent minimal).
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/notre-offre",
}

_HTTP_RETRIES = 3
_TIMEOUT_SECONDS = 15
# Petit délai entre deux appels API (POST par périmètre puis GET par fiche) :
# assurance anti-burst, aucune limite de débit observée à ce jour.
_REQUEST_DELAY_SECONDS = 0.3

# Le corps « pas de filtre » : tous les drapeaux à false = toutes les offres
# (vérifié en direct : 92 offres contre 90 avec drapeaux actifs). Seuls lieu/
# typeLieu/codeLieu sont remplis, un POST par périmètre.
_BASE_BODY = {
    "appartement": False,
    "maison": False,
    "parking": False,
    "studio": False,
    "f2": False,
    "f3": False,
    "f4": False,
    "f5": False,
    "lieu": "",
    "typeLieu": "",
    "codeLieu": "",
    "budget": [0, 15000],
}

# Premier mot de typeBien -> type canonique (« Appartement duplex F4 » tombe
# sur Appartement ; « Parking extérieur » sur Parking).
_TYPE_BY_LABEL = {
    "appartement": APARTMENT,
    "maison": HOUSE,
    "parking": PARKING,
}


def _strip_text(value: str | None) -> str:
    """Un texte HTML (descriptifBien) en texte brut : balises hors jeu,
    entités décodées, espaces aplatis."""
    if not value:
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _format_eur(value) -> str:
    """Un montant en euros formaté à la française (« 5 277 € »)."""
    if value is None:
        return ""
    return f"{int(value):,}".replace(",", " ") + " €"


def _to_float(value) -> float | None:
    """None si la valeur n'est pas convertible — une donnée illisible ne fait
    jamais échouer l'annonce entière."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _canonical_type(type_bien: str | None) -> str | None:
    """Le type canonique d'un libellé typeBien, None si non reconnu."""
    words = (type_bien or "").strip().split()
    return _TYPE_BY_LABEL.get(words[0].casefold()) if words else None


def _count_matches(count: int | None, allowed: list) -> bool:
    """Un compte (pièces/chambres) satisfait-il la liste canonique ? « 5 » du
    vocabulaire partagé signifie « 5 et plus » (voir search_edit.html).
    Un compte absent laisse passer (même contrat que PAP : on ne rejette pas
    ce qu'on ne sait pas lire)."""
    if not allowed or count is None:
        return True
    return any(count == r or (r >= 5 and count >= 5) for r in allowed)


def _photo_url(path: str | None) -> str:
    """L'URL absolue d'une photo : chemin relatif encodé (espaces et
    apostrophes sont fréquents dans les chemins côté Esset)."""
    path = (path or "").strip()
    if not path:
        return ""
    quoted = urllib.parse.quote(path, safe="/")
    separator = "" if path.startswith("/") else "/"
    return f"{IMG_URL}{separator}{quoted}"


class _ApiError(RuntimeError):
    """Une erreur d'appel à l'API Esset après épuisement des tentatives."""


def _request(session: requests.Session, method: str, url: str,
             **kwargs) -> requests.Response:
    """GET/POST avec backoff sur erreurs réseau et blocages CloudFront
    (403 « Request blocked » vu sur conf.json)."""
    last_error: Exception | None = None
    for attempt in range(_HTTP_RETRIES):
        if attempt > 0:
            time.sleep(2 ** attempt)
        try:
            resp = session.request(method, url, timeout=_TIMEOUT_SECONDS, **kwargs)
        except Exception as e:
            last_error = e
            logger.warning(f"[EssetPM] Erreur réseau ({url}) : {e}")
            continue
        if resp.status_code in (403, 429) or resp.status_code >= 500:
            last_error = RuntimeError(f"HTTP {resp.status_code}")
            logger.warning(f"[EssetPM] HTTP {resp.status_code}, retentative ({url})")
            continue
        resp.raise_for_status()
        return resp
    raise _ApiError(
        f"inaccessible après {_HTTP_RETRIES} tentatives ({url}) : {last_error}"
    )


def _search_body(location: dict) -> dict:
    """Le corps POST pour UN périmètre : localisation seule, tous les autres
    filtres à false (voir la docstring du module pour le pourquoi)."""
    kind = location.get("kind", CITY)
    # Jamais location["code"] direct : un département/région sans code doit
    # tomber sur le garde « sans code exploitable » ci-dessous, pas lever
    # KeyError avant lui.
    if kind == DEPARTMENT:
        scope = {"typeLieu": "d", "codeLieu": str(location.get("code") or "")}
    elif kind == REGION:
        scope = {"typeLieu": "r", "codeLieu": str(location.get("code") or "")}
    elif kind in (CITY, WHOLE_CITY):
        # Un code postal par requête : le site modélise lui-même ses lieux
        # comme des couples (ville, code postal) — jamais de liste.
        scope = {"typeLieu": "v",
                 "codeLieu": location.get("postalCode") or ""}
    else:
        raise ValueError(f"niveau de périmètre inconnu : {kind}")
    if not scope.get("codeLieu"):
        raise ValueError(f"périmètre sans code exploitable : {scope}")
    return {**_BASE_BODY, **scope}


def _perimeter_requests(locations: list[dict]) -> list[tuple[dict, dict]]:
    """Les couples (localisation, corps POST) couvrant les critères : un POST
    par code postal pour une ville entière (ses codes postaux sont portés par
    la localisation elle-même), un seul pour département/région."""
    pairs: list[tuple[dict, dict]] = []
    for location in locations:
        kind = location.get("kind", CITY)
        if kind == WHOLE_CITY:
            for postal_code in location.get("postalCodes") or []:
                single = {**location, "postalCode": postal_code}
                pairs.append((single, _search_body(single)))
            continue
        pairs.append((location, _search_body(location)))
    return pairs


def _describe(location: dict) -> str:
    """Un périmètre en clair, pour les logs et les messages d'erreur."""
    kind = location.get("kind", CITY)
    if kind == REGION:
        return f"région {location.get('name') or location.get('code')}"
    if kind == DEPARTMENT:
        return f"département {location.get('name') or location.get('code')}"
    if kind == WHOLE_CITY:
        return f"{location.get('city')} (toute la ville)"
    return f"{location.get('city')} ({location.get('postalCode')})"


def _passes_filters(row: dict, criteria: dict, locations: list[dict]) -> bool:
    """Rejoue TOUS les critères sur une offre brute de POST /offre — filet
    exact qui garantit qu'aucune annonce ne dépasse les critères, puisque
    l'API n'applique elle-même ni le budget ni la surface ni les pièces
    (budget ignoré serveur, vérifié en direct)."""
    zip_code = (row.get("codePostal") or "").strip()
    if not zip_code or not matches_locations(zip_code, locations):
        return False

    requested_types = list(criteria.get("propertyTypes") or [])
    if requested_types:
        property_type = _canonical_type(row.get("typeBien"))
        if property_type is None or property_type not in requested_types:
            return False

    rent = _to_float(row.get("loyerCc"))
    price_min = criteria.get("priceMin")
    price_max = criteria.get("priceMax")
    if rent is not None:
        if price_min and rent < price_min:
            return False
        if price_max and rent > price_max:
            return False

    surface = _to_float(row.get("surface"))
    surface_min = criteria.get("surfaceMin")
    surface_max = criteria.get("surfaceMax")
    if surface is not None:
        if surface_min and surface < surface_min:
            return False
        if surface_max and surface > surface_max:
            return False

    return _count_matches(_to_int(row.get("nbPieces")), criteria.get("rooms") or [])


def _detail_price_details(detail: dict) -> str:
    """Les précisions financières de la fiche, en clair et compact."""
    parts = []
    loyer_hc = _to_float(detail.get("loyerHc"))
    if loyer_hc is not None:
        parts.append(f"loyer HC {_format_eur(loyer_hc)}")
    for label, key in (
        ("provisions de charges", "charges"),
        ("honoraires", "honoraires"),
        ("dépôt de garantie", "depotGarantie"),
    ):
        value = _to_float(detail.get(key))
        if value:
            parts.append(f"{label} {_format_eur(value)}")
    return " · ".join(parts)


def _listing_from_detail(listing: Listing, detail: dict) -> Listing:
    """Enrichit un Listing avec la fiche complète (descriptif, chambres,
    DPE/GES, photos, finances). Les champs absents restent tels quels."""
    accroche = (detail.get("accroche") or "").strip()
    title = _strip_text(accroche) or listing.title

    photos = [_photo_url(p) for p in (detail.get("photos") or []) if p]
    photos_payload = json.dumps([{"url": p} for p in photos]) if photos else "[]"
    image_url = photos[0] if photos else listing.image_url

    listing.title = title
    listing.description = _strip_text(detail.get("descriptifBien")) or listing.description
    listing.epc = str(_to_int(detail.get("consoKwhep")) or "")
    listing.ges = str(_to_int(detail.get("emissionGes")) or "")
    listing.photos = photos_payload
    listing.image_url = image_url
    listing.price_details = _detail_price_details(detail)
    listing.headline = _strip_text(detail.get("nomProgramme") or "")

    # Issue #26 : quasi-natif — la fiche porte .lat/.lon directs, mais leur
    # absence prend DEUX formes vérifiées en direct : null ET 0.0/0.0.
    # extraire_coordonnees rejette les deux (plus l'illisible), sinon un lot
    # sans position atterrirait au golfe de Guinée sur la carte.
    coords = extraire_coordonnees(detail.get("lat"), detail.get("lon"))
    if coords:
        listing.latitude, listing.longitude = coords
        listing.location_precision = PRECISION_EXACTE
    return listing


@ParserRegistry.register
class EssetPmParser(BaseParser):
    """Scrape les locations gérées d'Esset Property Management via son API
    JSON publique (bo-back.esset-pm.com)."""

    SOURCE_ID = "essetpm"
    SUPPORTS_BEDROOMS = True
    SOURCE_NAME = "Esset Property Management"
    SOURCE_DESCRIPTION = (
        "locations.esset-pm.com — lots gérés en location, API JSON publique "
        "(ville, département, région)"
    )

    # Le portail ne référence que de la location (aucune vente, vérifié en
    # direct) ; l'UI propose appartement/maison/parking, pas de terrain.
    SUPPORTED_TRANSACTIONS = (RENT,)
    SUPPORTED_PROPERTY_TYPES = (APARTMENT, HOUSE, PARKING)

    URL_NOTE = (
        "La recherche Esset PM ne transmet aucun critère dans son URL "
        "(tout passe par l'écran de recherche du site) : le lien montre "
        "seulement le portail ; les filtres (localisation, loyer, surface, "
        "pièces) sont appliqués par le scraper lui-même sur chaque annonce."
    )

    def to_native(self, criteria: dict) -> dict:
        """Rien à traduire au niveau global : les identifiants de lieu
        attendus par l'API (code postal, codes INSEE département/région) sont
        portés tels quels par les localisations canoniques. Les corps POST
        sont construits par périmètre dans scrape()/_perimeter_requests().
        `criteria` n'est jamais modifié sur place."""
        return criteria

    def build_search_url(self, criteria: dict) -> str | None:
        urls = self.build_search_urls(criteria)
        return urls[0] if urls else None

    def build_search_urls(self, criteria: dict) -> list[str]:
        """L'URL publique unique du portail : l'application Esset ne encode
        AUCUN critère dans ses URL (état React, voir la docstring du module),
        il n'existe donc pas de lien scopé à refléter. Une seule URL dès
        qu'une localisation est exploitable — le miroir honnête de ce que
        scrape() interroge."""
        if not get_locations(criteria):
            return []
        return [f"{BASE_URL}/notre-offre"]

    def scrape(self, criteria: dict) -> list[Listing]:
        transaction = criteria.get("transaction") or RENT
        if transaction != RENT:
            raise ValueError(
                f"Esset Property Management ne référence que de la location, "
                f"pas « {transaction} »"
            )

        locations = get_locations(criteria)
        if not locations:
            raise ValueError(
                "Esset PM nécessite au moins une localisation (ville, "
                "département ou région) dans les critères"
            )

        self._guard_property_types(criteria)

        pairs = _perimeter_requests(locations)
        if not pairs:
            raise ValueError(
                "Aucun périmètre Esset PM exploitable : aucun code postal ou "
                "code INSEE dans les localisations"
            )

        session = requests.Session()
        session.headers.update(_HEADERS)

        seen: set[str] = set()
        listings: list[Listing] = []
        errors: list[str] = []

        for index, (location, body) in enumerate(pairs):
            try:
                if index > 0:
                    time.sleep(_REQUEST_DELAY_SECONDS)
                offers = self._fetch_offers(session, body)
                count = self._collect_offers(
                    session, criteria, location, offers, seen, listings
                )
                logger.debug(
                    f"[EssetPM] {_describe(location)} : {len(offers)} offre(s), "
                    f"{count} retenue(s)"
                )
            except Exception as e:
                errors.append(f"{_describe(location)} : {e}")
                logger.warning(f"[EssetPM] {_describe(location)} : {e}")

        if errors and len(errors) == len(pairs):
            raise ValueError("; ".join(errors))

        logger.info(f"[EssetPM] Scraping terminé : {len(listings)} annonces uniques")
        return listings

    def _guard_property_types(self, criteria: dict) -> None:
        """Échoue tôt quand AUCUN type demandé n'est exprimable (ex. terrain
        seul) : mieux qu'un résultat vide qui masquerait le problème. Les
        types partiellement couverts produisent un avertissement."""
        requested = list(criteria.get("propertyTypes") or [])
        if not requested:
            return
        expressible = [t for t in requested if t in _TYPE_BY_LABEL.values()]
        skipped = [t for t in requested if t not in expressible]
        if skipped:
            logger.warning(
                "[EssetPM] Type(s) non référencé(s) par le portail, ignorés : "
                f"{', '.join(sorted(skipped))}"
            )
        if not expressible:
            raise ValueError(
                f"Esset Property Management ne référence pas ce(s) type(s) "
                f"de bien : {', '.join(sorted(skipped))}"
            )

    def _fetch_offers(self, session: requests.Session, body: dict) -> list[dict]:
        """POST /offre pour un périmètre : la liste complète des offres (pas
        de pagination serveur)."""
        resp = _request(session, "POST", f"{API_URL}/offre", json=body)
        data = resp.json()
        if isinstance(data, dict):
            # L'app réagit à un objet porteur de `status` comme à une erreur.
            raise _ApiError(
                f"réponse inattendue de /offre : {str(data)[:200]}"
            )
        return data or []

    def _collect_offers(self, session: requests.Session, criteria: dict,
                        location: dict, offers: list[dict], seen: set,
                        listings: list[Listing]) -> int:
        """Filtre les offres d'un périmètre puis enrichit chacune avec sa
        fiche détaillée. Retourne le nombre retenu (pour le log)."""
        retained = 0
        for offset, row in enumerate(offers):
            code = (row.get("codeAnnonce") or "").strip()
            if not code or code in seen:
                continue
            if not _passes_filters(row, criteria, [location]):
                continue
            retained += 1
            seen.add(code)

            listing = self._to_listing(row)
            try:
                if offset > 0:
                    time.sleep(_REQUEST_DELAY_SECONDS)
                detail = self._fetch_detail(session, code)
            except Exception as e:
                # Dégradation gracieuse : la liste suffit à notifier, la fiche
                # complète reviendra au prochain passage.
                logger.warning(f"[EssetPM] Fiche {code} indisponible : {e}")
                detail = None
            if detail:
                listing = _listing_from_detail(listing, detail)
                bedrooms = _to_int(detail.get("nbChambres"))
                if not _count_matches(bedrooms, criteria.get("bedrooms") or []):
                    # La fiche révèle un nombre de chambres hors critères :
                    # l'offre est écartée avant d'être ajoutée.
                    seen.discard(code)
                    continue
            listings.append(listing)
        return retained

    def _fetch_detail(self, session: requests.Session, code: str) -> dict | None:
        """GET /offre/{codeAnnonce} : la fiche complète, None si absente."""
        resp = _request(session, "GET", f"{API_URL}/offre/{urllib.parse.quote(code)}")
        detail = resp.json()
        return detail if isinstance(detail, dict) and detail else None

    def _to_listing(self, row: dict) -> Listing:
        """Une offre brute de POST /offre en Listing (la fiche détaillée
        viendra compléter titre, description, chambres, DPE et photos)."""
        code = (row.get("codeAnnonce") or "").strip()
        city = (row.get("ville") or "").strip()
        property_type = _canonical_type(row.get("typeBien"))
        rent = _to_float(row.get("loyerCc"))
        surface = _to_float(row.get("surface"))
        pieces = _to_int(row.get("nbPieces"))

        return Listing(
            listing_id=f"essetpm_{code}",
            url=f"{BASE_URL}/location/{code}",
            title=f"{(row.get('typeBien') or 'Lot').strip()} à {city}".strip(),
            price=f"{_format_eur(rent)} CC" if rent is not None else "",
            surface=str(surface) if surface is not None else "",
            rooms=str(pieces) if pieces else "",
            location=city,
            image_url=_photo_url(row.get("photoCouverture")),
            agency=self.SOURCE_NAME,
            source=self.SOURCE_ID,
            legacy_id=code,
            price_value=rent,
            city=city,
            zip_code=(row.get("codePostal") or "").strip(),
            property_type=property_type or "",
            is_private=False,
            # Issue #12 : le payload essetpm ne contient qu'une
            # `dateCommandeDpe` (date de commande du diagnostic DPE) — PAS
            # une date de publication d'annonce, sémantique différente. Aucune
            # date exploitable → sentinelle explicite plutôt que chaîne vide.
            creation_date=DATE_INCONNUE,
        )
