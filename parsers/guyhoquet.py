"""Guy Hoquet (guy-hoquet.com) listing scraper — API JSON markers, repli HTML paginé.

Traduit les critères canoniques (core.criteria) vers le format `filters[fXX][]`
du site — voir to_native(). L'identifiant de lieu (`toulouse-31000_c3`,
`31_c2`, `76_c1`) est résolu depuis la localisation canonique, départements
et régions par pure dérivation, villes via l'autocomplete publique avec cache
persistant en base (voir services.guyhoquet_geocode).

La recherche se fait en UN SEUL appel quelle que soit la taille de la zone :
le site fusionne nativement toutes les localisations passées en
`filters[f20][]` (fusion OR vérifiée en direct le 23/08/2026 : Toulouse +
Ivry en un appel renvoie les résultats des deux villes, une région entière
compte ses ~1605 annonces en un seul appel).

Deux formats de réponse coexistent côté site (observés le même jour) :
markers JSON riche (`address{}`, `price{}` imbriqués, `type` en code
numérique) et markers plats (`city`, `zip`, `price` au premier niveau,
`type` en libellé). Le parsing lit les deux — voir _dict_to_listing.

Au-delà de 1000 annonces, l'API markers TRONQUE silencieusement sa réponse
(plafond vérifié en direct : total=2109 -> 1000 résultats). Le scrape passe
alors par la pagination HTML du site (18 cartes par page, plafond
MAX_PAGES généreux) pour ne rien perdre.
"""

from __future__ import annotations

import json
import math
import re

import requests
from bs4 import BeautifulSoup
from loguru import logger

from core.criteria import (
    APARTMENT,
    BUY,
    PROPERTY_TYPE_LABELS,
    RENT,
    matches_locations,
)
from core.geocode import CITY, REGION
from models.listing import Listing
from parsers._dates import normaliser_creation_date
from parsers.base import BaseParser, ParserRegistry, get_locations, has_transit

BASE_URL = "https://www.guy-hoquet.com"
RESULT_URL = f"{BASE_URL}/biens/result"

# Header OBLIGATOIRE : sans lui l'endpoint répond un shell HTML au lieu du
# JSON (vérifié en direct le 23/08/2026).
_AJAX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

# Plafond markers vérifié en direct (total > 1000 -> exactement 1000
# résultats renvoyés) : au-delà, bascule sur la pagination HTML.
_MARKERS_LIMIT = 1000
# Nombre de cartes par page HTML observé en direct.
_PAGE_SIZE = 18
# Plafond généreux du parcours paginé (120 pages x 18 cartes = 2160 biens,
# au-delà de quoi une recherche reste volontairement tronquée plutôt que de
# tourner indéfiniment — même filet que les autres sources paginées).
MAX_PAGES = 120

# Filet anti-mode-dégradé : le serveur a été observé (23/08/2026) répondant
# 200 OK en IGNORANT silencieusement les filtres de localisation — le total
# annoncé devient celui du site entier et chaque page paginée sort du
# périmètre. En marche normale, les premières pages contiennent forcément
# des biens du périmètre (tri par fraîcheur) : N pages consécutives sans un
# seul bien dedans signent ce mode dégradé, inutile de marteler les 120 pages.
_DEGRADED_PAGE_GRACE = 3

# Vocabulaire canonique -> vocabulaire guy-hoquet. Codes de transaction
# observés dans l'autocomplete du site (« Acheter » = 1, « Louer » = 2) ;
# saisonnier/meublé/viager (3/5/6) sont hors périmètre canonique.
_TRANSACTIONS = {BUY: "1", RENT: "2"}
_PROPERTY_TYPES = {
    "apartment": "appartement",
    "house": "maison",
    "parking": "parking-box",
    "land": "terrain",
}

# Préfixe du chemin d'URL de fiche selon la transaction, vérifié en direct :
# /location/x-{id} et /achat-vente/x-{id} répondent 301 vers la fiche
# canonique quel que soit ce qui précède l'id (le site re-slugifie tout
# seul) — d'où ces URLs synthétiques courtes, stables et jamais cassées.
_TRANSACTION_URL_PREFIX = {"1": "achat-vente", "2": "location"}

# Le champ `type` arrive tantôt en libellé (« Maison »), tantôt en code
# numérique. Seule correspondance numérique observée en direct à ce jour :
# 1 = « Appartement » (300 markers d'une même recherche location, tous de
# type 1 et tous des appartements) ; les codes non observés valent libellé
# vide plutôt qu'une supposition.
_MARKER_TYPE_CODES = {1: PROPERTY_TYPE_LABELS[APARTMENT]}

# Sémantique pièces : le canonique note « 5 » pour « 5+ », les cases GH vont
# de 1 à 10 (« 10+ » sur la dernière). Chambres : « 5+ » des deux côtés.
_ROOMS_PLUS_THRESHOLD = 5
_GH_ROOMS_MAX = 10
_GH_BEDROOMS_MAX = 5


def _clean_description(text: str) -> str:
    """Une description markers mise en texte : le site y laisse des <br />
    et entités HTML, tronquée comme chez toutes les sources."""
    if not text:
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", without_tags).strip()[:300]


def _as_float(value) -> float | None:
    """Un montant/surface numérique tolérant au format (« 1 650,5 » -> 1650.5),
    None sur ce qui n'est pas convertible — les markers mélangent int, float
    et chaînes selon la réponse."""
    if value is None:
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _gh_room_values(canonical_values) -> list[str]:
    """Les valeurs `number_room[]` Guy Hoquet couvrant la demande canonique.

    Le canonique note « 5 » pour « 5 et plus » (search_edit.html) alors que
    les cases à cocher GH sont EXACTES de 1 à 10, « 10+ » sur la dernière :
    une demande « 5+ » s'étend donc en [5..10] — l'OR multi-valeurs du filtre
    étant vérifié côté site (f70=[2,3] ne renvoie que des 2 et 3 pièces), la
    sélection étendue restitue exactement la sémantique canonique."""
    values: set[int] = set()
    for raw in canonical_values or []:
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        if n >= _ROOMS_PLUS_THRESHOLD:
            values.update(range(_ROOMS_PLUS_THRESHOLD, _GH_ROOMS_MAX + 1))
        else:
            values.add(n)
    return [str(v) for v in sorted(values)]


def _gh_bedroom_values(canonical_values) -> list[str]:
    """Les valeurs `number_bedroom[]` GH : même convention « 5 = 5 et plus »
    des deux côtés (la dernière case GH est libellée « 5+ », vérifié dans le
    formulaire du site) — mapping direct, sans extension."""
    values: set[int] = set()
    for raw in canonical_values or []:
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n > 0:
            values.add(min(n, _GH_BEDROOMS_MAX))
    return [str(v) for v in sorted(values)]


def _format_price(price, transaction_code: str) -> str:
    value = _as_float(price)
    if value is None:
        return ""
    formatted = f"{int(value):,}".replace(",", " ")
    return f"{formatted} €/mois" if transaction_code == _TRANSACTIONS[RENT] else f"{formatted} €"


def _nested_or_flat(data: dict, nested_key: str, nested_field: str, flat_key: str):
    """Un champ présent tantôt imbriqué (`price` -> `price`, `address` ->
    `zip`), tantôt à plat au premier niveau — les deux formats de markers
    coexistent côté site (voir docstring du module)."""
    container = data.get(nested_key)
    if isinstance(container, dict):
        value = container.get(nested_field)
        if value is not None:
            return value
    return data.get(flat_key)


def _property_type_label(raw) -> str:
    """Le libellé du type de bien, quel que soit le codage du champ `type`."""
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, int):
        return _MARKER_TYPE_CODES.get(raw, "")
    return ""


def _dict_to_listing(data: dict) -> Listing:
    """Convertit une annonce markers (format riche ou plat) en Listing."""
    ad_id = str(data.get("id", ""))
    transaction_code = str(data.get("type_transaction") or "")
    price = _nested_or_flat(data, "price", "price", "price")
    city = _nested_or_flat(data, "address", "city", "city") or ""
    zip_code = _nested_or_flat(data, "address", "zip", "zip") or ""

    photos = [
        {"url": p}
        for p in (data.get("pictures") or [])
        if isinstance(p, str) and p
    ]
    image_url = photos[0]["url"] if photos else ""

    return Listing(
        listing_id=f"gh_{ad_id}",
        # Aucune URL de fiche dans les markers : /{transaction}/x-{id} répond
        # 301 vers la fiche canonique (vérifié en direct), voir
        # _TRANSACTION_URL_PREFIX.
        url=f"{BASE_URL}/{_TRANSACTION_URL_PREFIX.get(transaction_code, 'location')}/x-{ad_id}"
        if ad_id
        else "",
        title=data.get("name") or "",
        price=_format_price(price, transaction_code),
        surface=str(data["surface"]) if data.get("surface") is not None else "",
        rooms=str(data["number_room"]) if data.get("number_room") is not None else "",
        location=f"{city} {zip_code}".strip(),
        image_url=image_url,
        description=_clean_description(data.get("description")),
        agency="",
        source="guyhoquet",
        legacy_id=str(data.get("reference") or ""),
        price_value=_as_float(price),
        city=city,
        district="",
        zip_code=zip_code,
        property_type=_property_type_label(data.get("type")),
        is_private=False,
        epc=data.get("energy_consumption") or "",
        ges=data.get("ges") or "",
        is_new=_property_type_label(data.get("type")) == "Programme neuf",
        is_exclusive=bool(data.get("exclusivity")),
        has_3d_visit=bool(data.get("virtual_visit")),
        # created_at est naïf (« YYYY-MM-DD HH:MM:SS ») : supposé UTC par le
        # normalisateur (issue #12).
        creation_date=normaliser_creation_date(data.get("created_at")),
        update_date=data.get("updated_at") or "",
        photos=json.dumps(photos),
    )


def _parse_card(card) -> Listing | None:
    """Une carte HTML (`div.resultat-item`) en Listing.

    Structure vérifiée en direct dans templates.properties :
        div.resultat-item[data-id="1894176"] > a.property_link_block[href]
        span.ttl (« Appartement 3 pièces 59.09 m² »), div.price (« 214 000 € »),
        ville+CP (« Villejuif 94800 ») dans un bloc texte dédié."""
    card_id = card.get("data-id")
    link = card.select_one("a.property_link_block[href]") or card.select_one("a[href]")
    if not card_id or link is None:
        return None

    # Ville + code postal : le site les publie dans un bloc texte dédié
    # (« Villejuif 94800 », seul contenu de son div). On cherche ce bloc
    # exact d'abord ; en repli, la DERNIÈRE occurrence du motif dans la carte
    # — un titre peut contenir un nombre à 5 chiffres (surface « 12345 m² »),
    # le bloc ville vient toujours après lui.
    place_re = re.compile(r"([A-Za-zÀ-ÿ'’\- ]+?)\s+(\d{5})\b")
    city, zip_code = "", ""
    for node in card.find_all("div"):
        m = place_re.fullmatch(node.get_text(" ", strip=True))
        if m:
            city, zip_code = m.group(1).strip(), m.group(2)
            break
    if not zip_code:
        matches = place_re.findall(card.get_text(" ", strip=True))
        if matches:
            city, zip_code = matches[-1][0].strip(), matches[-1][1]

    ttl = card.select_one(".ttl")
    title = ttl.get_text(" ", strip=True) if ttl else ""

    price_node = card.select_one(".price")
    price_text = price_node.get_text(" ", strip=True) if price_node else ""

    m2_match = re.search(r"([\d.,]+)\s*m²", title)
    rooms_match = re.search(r"(\d+)\s*pi[èe]ces?", title)

    return Listing(
        listing_id=f"gh_{card_id}",
        url=link["href"],
        title=title,
        price=price_text,
        surface=m2_match.group(1).replace(",", ".") if m2_match else "",
        rooms=rooms_match.group(1) if rooms_match else "",
        location=f"{city} {zip_code}".strip(),
        image_url="",
        description="",
        agency="",
        source="guyhoquet",
        city=city,
        zip_code=zip_code,
        property_type=title.split(" ")[0] if title else "",
        is_private=False,
    )


@ParserRegistry.register
class GuyHoquetParser(BaseParser):
    """Scrape guy-hoquet.com : markers JSON fusionnés, repli HTML paginé."""

    SOURCE_ID = "guyhoquet"
    SOURCE_NAME = "Guy Hoquet"
    SOURCE_DESCRIPTION = (
        "guy-hoquet.com — API JSON markers (fusion native des localisations), "
        "repli pagination HTML au-delà de 1000 biens"
    )

    # Guy Hoquet référence les quatre types canoniques (appartement, maison,
    # parking-box, terrain) et les deux transactions : SUPPORTED_* gardent
    # leur valeur par défaut (tout).
    SUPPORTED_TRANSACTIONS = (RENT, BUY)

    # Pas de repli manuel : décision utilisateur — la résolution hybride
    # (dérivation statique + autocomplete caché) suffit.
    MANUAL_OVERRIDE_LABEL = ""
    MANUAL_OVERRIDE_HELP = ""

    URL_NOTE = ""

    # ------------------------------------------------------------------
    # Traduction canonique -> guy-hoquet
    # ------------------------------------------------------------------

    def _scoped_location(self, location: dict) -> dict:
        """Une copie du périmètre prête pour le contrôle aval par préfixes
        postaux (matches_locations) : une RÉGION qui n'porterait pas sa liste
        de départements n'aurait AUCUN préfixe et écarterait toutes les
        annonces en silence — on la complète donc depuis l'API geo, sur une
        COPIE (les critères sont partagés entre sources pendant un scrape,
        jamais mutés). Même principe que Century21Parser._slugs."""
        if location.get("kind", CITY) == REGION and not location.get("departments"):
            code = location.get("code")
            if code:
                from core.geocode import region_departments

                return {**location, "departments": region_departments(code)}
        return location

    def _geo_repo(self):
        """Le cache persistant slug <- périmètre, ou None s'il n'y a pas de
        storage — jamais lu depuis `flask.current_app` : le scraping
        s'exécute sur un thread de fond, hors contexte d'application."""
        return getattr(self.storage, "guyhoquet_geo", None) if self.storage else None

    def _slugs(self, criteria: dict) -> list[str]:
        """Les slugs de localisation résolus, dédupliqués, dans l'ordre des
        périmètres des critères.

        Un périmètre qui ne se résout pas est signalé mais n'empêche pas la
        recherche sur les autres — c'est scrape() qui lèvera si AUCUN
        périmètre n'est résoluble."""
        from services.guyhoquet_geocode import resolve_slug

        repo = self._geo_repo()
        slugs: list[str] = []
        for location in get_locations(criteria):
            slug = resolve_slug(location, repo)
            if slug and slug not in slugs:
                slugs.append(slug)
        return slugs

    def to_native(self, criteria: dict) -> dict:
        """Critères canoniques -> dictionnaire de filtres guy-hoquet.

        Les clés sont les IDs numériques des filtres du site (10=transaction,
        20=localisations, 30=types, 40/45=prix, 50/60=surface, 70/80=pièces/
        chambres) — construites ici et nulle part ailleurs : _search_params
        les sérialise en `filters[XX][]`, build_search_url les affiche en
        `fXX=` dans le hash public. Ne modifie jamais `criteria` : les
        critères sont partagés entre toutes les sources d'une même recherche
        pendant un scrape."""
        native: dict = {}

        transaction = _TRANSACTIONS.get(criteria.get("transaction"))
        if transaction:
            native["10"] = [transaction]

        slugs = self._slugs(criteria)
        if slugs:
            native["20"] = slugs

        property_types = [
            _PROPERTY_TYPES[t]
            for t in criteria.get("propertyTypes") or []
            if t in _PROPERTY_TYPES
        ]
        if property_types:
            native["30"] = property_types

        for native_key, canonical_key in (
            ("40", "priceMax"),
            ("45", "priceMin"),
            ("50", "surfaceMin"),
            ("60", "surfaceMax"),
        ):
            if criteria.get(canonical_key) is not None:
                native[native_key] = [criteria[canonical_key]]

        room_values = _gh_room_values(criteria.get("rooms"))
        if room_values:
            native["70"] = room_values

        bedroom_values = _gh_bedroom_values(criteria.get("bedrooms"))
        if bedroom_values:
            native["80"] = bedroom_values

        return native

    def _search_params(self, native: dict, page: int, with_markers: bool) -> list[tuple[str, str]]:
        """Les query params d'un appel /biens/result.

        ⚠️ Le backend attend des IDs NUMÉRIQUES (`filters[20][]`, les
        `data-filter` de son formulaire) : la variante préfixée
        `filters[f20][]` est ignorée SILENCIEUSEMENT — la réponse repasse en
        France entière (total=16380 au lieu de 17, vérifié en direct dans les
        deux sens le 23/08/2026). Le préfixe « f » n'existe que dans le hash
        de l'URL publique (voir build_search_url).

        Un filtre multi-valué répète sa clé ; l'URL publique joint elle ses
        valeurs par virgules."""
        params: list[tuple[str, str]] = [("p", str(page))]
        for key, values in native.items():
            for value in values:
                params.append((f"filters[{key}][]", str(value)))
        params.append(("with_markers", "true" if with_markers else "false"))
        return params

    def _fetch_markers(self, native: dict) -> tuple[int | None, list[dict]]:
        """L'appel markers unique pour tous les périmètres fusionnés.

        Retourne (total, résultats) ; total None si la réponse n'a pas la
        forme attendue — les erreurs HTTP réelles (réseau, 500) remontent à
        l'appelant plutôt que d'être masquées en résultat vide."""
        resp = requests.get(
            RESULT_URL,
            params=self._search_params(native, page=1, with_markers=True),
            headers=_AJAX_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        markers = data.get("markers")
        results = markers.get("results") if isinstance(markers, dict) else None
        if not isinstance(results, list):
            # Une réponse malformée est un échec réel (le site a changé de
            # format), pas une recherche vide légitime : on la remonte pour
            # que ScrapeService la distingue d'un résultat vide. Le site a
            # par ailleurs observé un jour `markers` en LISTE (autre endpoint)
            # — AttributeError interdit, même contrat ValueError.
            raise ValueError("Réponse markers Guy Hoquet inattendue : champ results absent")
        return (markers.get("total"), results)

    def _fetch_page_html(self, native: dict, page: int) -> str:
        """Le fragment HTML des cartes d'une page (templates.properties)."""
        resp = requests.get(
            RESULT_URL,
            params=self._search_params(native, page=page, with_markers=False),
            headers=_AJAX_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        templates = data.get("templates") or {}
        return templates.get("properties") or ""

    def _scrape_paged(self, native: dict, total: int, locations: list[dict]) -> list[Listing]:
        """La pagination HTML : le seul chemin qui rend tout quand la source
        dépasse le plafond markers de 1000."""
        needed_pages = math.ceil(total / _PAGE_SIZE)
        pages = min(needed_pages, MAX_PAGES)
        if needed_pages > MAX_PAGES:
            logger.warning(
                f"[Guy Hoquet] Recherche tronquée aux {MAX_PAGES} premières pages "
                f"({total} biens annoncés)"
            )
        listings: list[Listing] = []
        seen: set[str] = set()
        pages_without_match = 0
        for page in range(1, pages + 1):
            html = self._fetch_page_html(native, page)
            cards = BeautifulSoup(html, "lxml").select("div.resultat-item[data-id]")
            if not cards:
                break
            matched_before = len(listings)
            for card in cards:
                listing = _parse_card(card)
                if listing is None or listing.listing_id in seen:
                    continue
                if not matches_locations(listing.zip_code, locations):
                    continue
                seen.add(listing.listing_id)
                listings.append(listing)
            pages_without_match = pages_without_match + 1 if len(listings) == matched_before else 0
            if pages_without_match >= _DEGRADED_PAGE_GRACE:
                logger.warning(
                    f"[Guy Hoquet] {_DEGRADED_PAGE_GRACE} pages consécutives sans bien du "
                    f"périmètre ({len(listings)} retenus) : les filtres semblent ignorés "
                    "par le site, arrêt anticipé"
                )
                break
            logger.debug(f"[Guy Hoquet] Page {page}/{pages} : {len(cards)} cartes lues")
        return listings

    def build_search_url(self, criteria: dict) -> str | None:
        """L'URL publique de recherche guy-hoquet.com, ou None.

        Format hash natif du site, octet pour octet celui que produit son
        propre frontend (filters.js : hashKey initialisé à la constante
        « 1 », puis la page, puis chaque filtre `&f<id>=valeurs` jointes par
        virgules — init-hash.js relit les filtres en cherchant littéralement
        `'&f10'`, AVEC le `&` : un hash qui commencerait par `#f10=` ne serait
        pas lu) :

            /biens/result#1&p=1&f10=2&f20=toulouse-31000_c3,ivry-sur-seine-94200_c3
                &f30=appartement&f40=1200&f45=600&f50=20&f60=40

        La recherche GH fusionnant nativement toutes les localisations,
        cette URL est unique — miroir exact du scrape, qui fait lui aussi
        un seul appel."""
        from urllib.parse import quote

        native = self.to_native(criteria)
        slugs = native.get("20")
        if not slugs:
            return None

        parts: list[str] = ["1", "p=1"]
        for key in ("10", "20", "30", "40", "45", "50", "60", "70", "80"):
            values = native.get(key)
            if values:
                joined = ",".join(str(v) for v in values)
                parts.append(f"f{key}={quote(joined, safe=',')}")
        return f"{BASE_URL}/biens/result#{'&'.join(parts)}"

    def scrape(self, criteria: dict) -> list[Listing]:
        """Exécute le scraping pour les critères canoniques donnés.

        UN SEUL appel markers pour tous les périmètres (fusion OR native du
        site) ; bascule sur la pagination HTML quand la source tronque
        (> 1000). Les erreurs réelles (réseau, format inattendu, aucun
        périmètre résolu) remontent en ValueError/exception plutôt qu'en
        liste vide, pour que ScrapeService distingue un échec d'une recherche
        légitimement sans résultat — même contrat que les autres sources."""
        native = self.to_native(criteria)
        slugs = native.get("20") or []
        if not slugs:
            raise ValueError(
                "Aucun périmètre Guy Hoquet n'a pu être résolu pour cette recherche "
                "(autocomplète échouée sur toutes les localisations)"
            )
        locations = [self._scoped_location(loc) for loc in get_locations(criteria)]

        total, results = self._fetch_markers(native)

        if total is not None and total > _MARKERS_LIMIT:
            logger.info(
                f"[Guy Hoquet] {total} biens annoncés : dépassement du plafond markers "
                f"({_MARKERS_LIMIT}), bascule sur la pagination HTML"
            )
            listings = self._scrape_paged(native, total, locations)
        else:
            listings = []
            seen: set[str] = set()
            for data in results:
                listing = _dict_to_listing(data)
                if listing.listing_id in seen:
                    continue
                if not matches_locations(listing.zip_code, locations):
                    continue
                seen.add(listing.listing_id)
                listings.append(listing)

        logger.info(f"[Guy Hoquet] Scraping terminé : {len(listings)} annonces retenues")
        return listings

    def has_valid_criteria(self, criteria: dict) -> bool:
        """Utilisable dès qu'au moins un périmètre est identifiable : soit il
        porte un area_cache_key (ville), soit il se dérive statiquement
        (département/région). La résolution elle-même est tentée au moment
        du scrape, pas ici : créer une recherche ne doit pas dépendre d'un
        appel réseau."""
        from services.guyhoquet_geocode import area_cache_key, is_statically_resolvable

        locations = get_locations(criteria)
        if any(area_cache_key(loc) for loc in locations):
            return True
        return any(is_statically_resolvable(loc) for loc in locations)

    def cannot_search_reason(self, criteria: dict) -> str | None:
        """Même contrat que BaseParser, fallback #28 compris : une recherche
        « transit-seule » reste cherchable — l'expansion produira ses
        localisations classiques avant `to_native`."""
        if not self.has_valid_criteria(criteria) and not has_transit(criteria):
            return "aucune localisation exploitable (ville, département ou région requis)"

        unsupported = self.unsupported_criteria(criteria)
        if unsupported:
            return f"{self.SOURCE_NAME} ne référence pas {' ni '.join(unsupported)}"
        return None
