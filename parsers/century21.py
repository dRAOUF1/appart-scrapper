"""Century21.fr listing scraper — HTML server-rendered.

Contrairement à SeLoger (JSON embarqué) et bienici (API JSON), Century 21
rend ses pages de résultats côté serveur : chaque page de recherche est du
HTML qu'il faut parser avec BeautifulSoup. Vérifié en direct le 15/08/2026 —
aucun anti-bot Cloudflare/DataDome, mais les réponses exigent un Referer et
l'autocomplete un X-Requested-With (sans eux, réponses vides).

La localisation est encodée dans le chemin de l'URL par un slug opaque propre
au site (`v-paris`, `cp-75001`, `v-st+etienne`), non dérivable du code INSEE
par formule : il est résolu depuis la localisation canonique via l'endpoint
public d'autocomplete du site, avec un cache persistant en base (voir
services.century21_geocode).

Format de la page de recherche (vérifié en direct) :

    /annonces/f/{transaction}/{slug}/            aucun type demandé
    /annonces/{transaction}-{type}/{slug}/       un seul type (pas de « f »)
    /annonces/f/{transaction}-{types}/{slug}/    deux types ou plus
    + suffixe [/page-N/] pour la pagination

Le « f » marque une recherche multi-critères : le segment types n'y apparaît
qu'avec AU MOINS deux types (un seul type avec « f » renvoie 410, tout comme
plusieurs types sans « f ») — voir _base_path.

Les filtres prix/surface/pièces sont portés par des segments d'URL dédiés,
dont la grammaire est exigeante (tout est vérifié en direct par confrontation
200/410, voir _url_filters) :

    s-{min}-{max}/st-{min}-{max}/b-0-{max}   trio ATOMIQUE et ordonné — tout
                                             sous-ensemble renvoie 410 ; le
                                             minimum du budget doit rester 0
    p-{n}                                    EXACTEMENT n pièces, un seul
                                             segment, seul ou en fin de chaîne

Le minimum de prix canonique (sans max) et les listes de pièces multiples ou
« 5 et plus » n'ont pas d'équivalent URL : ils restent appliqués côté scraper
par _passes_filters, qui rejoue TOUS les critères sur chaque annonce.

La page de résultats porte toujours deux sections qui ressemblent à des
résultats mais ne le sont pas (même leçon que Laforêt et sa section
« à proximité ») :

    - une section « Biens se rapprochant de votre recherche » alimentée par
      des biens hors périmètre (ex. Paris 4e pour une recherche Paris 1er) —
      son conteneur est distinct (`.c-the-list-of-properties-related`) et le
      parsing ne cible que `.c-the-list-of-properties-list` ;
    - des blocs publicitaires `c-the-ad`, sans `data-uid`.

Le formulaire du site ne référence qu'un seul périmètre à la fois : le champ
localisation devient désactivé dès qu'une ville est choisie (vérifié en
direct). Mais le moteur accepte en réalité PLUSIEURS périmètres par URL
(vérifié en direct) : les slugs se fusionnent en un seul segment, le premier
gardant son préfixe de niveau et les suivants se collant nus par tirets —
`/annonces/f/location-appartement/v-paris/d-91_essonne-92_hauts_de_seine/`
renvoie 200 et couvre les trois périmètres. Toutes les localisations d'une
recherche sont donc FUSIONNÉES en UNE seule URL (voir build_search_urls) :
regroupées PAR NIVEAU (_level_segments), chaque niveau formant son segment
au sein de la même URL, le scrape passe par UNE seule série de pages et
_passes_filters rejoue le contrôle sur TOUTES les localisations couvertes.
Seuls les slugs collés à la main restent un couple slug/périmètre par URL :
c'est l'utilisateur qui a fixé ce découpage, chaque slug garde ainsi son
isolement en cas d'erreur. La fusion exige la forme « f », même avec un seul
type demandé.

Niveaux de périmètre supportés : city (commune ou arrondissement),
whole_city et department (page `d-{code}_{nom}`, vérifiée en direct : 200
pour /annonces/f/achat/d-33_gironde/, 410 pour le slug nu /d-33/) ; le slug
complet se dérive de l'autocomplete du site (voir services.century21_geocode).
La région n'a AUCUN identifiant propre (autocomplete muet, `/r-11/` renvoie
410, vérifié en direct) : elle est élargie côté parser à ses départements —
jamais en liste de communes.
"""

from __future__ import annotations

import re
import time

import requests
from bs4 import BeautifulSoup
from loguru import logger

from core.criteria import (
    APARTMENT,
    BUY,
    HOUSE,
    LAND,
    PARKING,
    RENT,
    matches_locations,
    source_overrides,
)
from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY
from models.listing import Listing
from parsers._dates import DATE_INCONNUE
from parsers.base import BaseParser, ParserRegistry, get_locations

BASE_URL = "https://www.century21.fr"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

MAX_PAGES = 30
# Nombre de tentatives par page : Century 21 renvoie un HTTP 410 temporaire
# (quota anti-bot) sur les requêtes répétées — un backoff les absorbe.
_HTTP_RETRIES = 3

# Vocabulaire canonique -> slugs du chemin Century 21 (vérifiés en direct :
# /annonces/f/location-maison-appartement/... et /annonces/f/achat-parking-
# terrain/... renvoient 200).
TRANSACTION_SLUGS = {BUY: "achat", RENT: "location"}
TYPE_SLUGS = {APARTMENT: "appartement", HOUSE: "maison", PARKING: "parking", LAND: "terrain"}

# Lien fiche annonce : le href du premier <a> de la carte.
_DETAIL_PATH_RE = re.compile(r"/trouver_logement/detail/(\d+)")

# Textes extraits de la carte. Le get_text(" ", strip=True) aplatit le <sup>
# en « m 2 » (un espace entre m et l'exposant) : le regex surface tolère donc
# l'espace. Le code postal est parfois TRONQUÉ AU DÉPARTEMENT par le site pour
# les villes simples (Montrouge -> « 92 », Nantes -> « 44 », vérifié en direct ;
# Paris/Lyon/Marseille gardent leur arrondissement complet) : d'où le {2,5}.
_PRICE_RE = re.compile(r"([\d\s.,]+?)\s*€")
_CITY_ZIP_RE = re.compile(r"([A-ZÀ-Ü][A-Za-zÀ-ÿ' \-]{0,40}?)\s+(\d{2,5})\b")
_SURFACE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m\s*[²2]")
_ROOMS_RE = re.compile(r"(\d+)\s*pi[èe]ce")


def _transaction(criteria: dict) -> str:
    """La transaction canonique demandée. La location par défaut : c'est ce
    que le formulaire propose en premier (même convention que Laforet)."""
    transaction = criteria.get("transaction")
    return transaction if transaction in TRANSACTION_SLUGS else RENT


def _type_slugs(criteria: dict) -> list[str]:
    """Les slugs de type Century 21 pour les types canoniques demandés.

    Sans type demandé, aucun segment de type : l'URL `/annonces/f/achat/...`
    (vérifiée en direct : 200) couvre alors tous les types, comme bienici
    sans propertyType."""
    return [TYPE_SLUGS[t] for t in criteria.get("propertyTypes") or [] if t in TYPE_SLUGS]


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


def _merged_slug(slugs: list[str]) -> str:
    """Les slugs d'un MÊME niveau fusionnés en un seul segment d'URL.

    Le moteur Century 21 accepte plusieurs périmètres par recherche : le
    premier slug garde sa forme complète (`d-77_seine_et_marne`), les suivants
    perdent leur préfixe de niveau et se collent par tirets — format vérifié
    en direct : `/annonces/f/location-appartement/v-paris/d-91_essonne-
    92_hauts_de_seine/` renvoie 200 et couvre TOUTES les localisations
    (préfixes 75/91/92 sur la page 1). Le préfixe répété
    (`d-91_essonne-d-92_hauts_de_seine`) renvoie 410."""
    if not slugs:
        return ""
    first = slugs[0]
    rest = [re.sub(r"^[a-z]+-", "", s, count=1) for s in slugs[1:]]
    return "-".join([first, *rest])


# Ordre canonique des niveaux de périmètre dans une URL fusionnée : les
# localités par nom d'abord (`v-` ville entière, `cpv-` commune désignée par
# son code postal), puis les localités par code postal (`cp-`, seul moyen de
# viser un arrondissement), puis les départements (`d-`). Chaque préfixe est
# SON niveau pour le moteur du site : un segment homogène par préfixe, jamais
# de mélange.
_LEVEL_ORDER = ("v", "cpv", "cp", "d")


def _level_segments(slugs: list[str]) -> str:
    """Les segments de localisation d'une URL multi-périmètres.

    Le site lit UN type de périmètre par segment, et un seul FORMAT au sein
    de ce type : les slugs sont donc regroupés PAR PRÉFIXE (`v-`/`cpv-` et
    `cp-` sont deux familles de localités — nom vs code postal — issues des
    mêmes entrées d'autocomplete ; `d-` est le niveau département), chaque
    groupe est fusionné entre eux (_merged_slug) et l'ordre des segments est
    fixe (_LEVEL_ORDER). Mélanger les niveaux dans un MÊME segment renvoie
    410 (vérifié en direct : /f/location-appartement/
    v-paris-77_seine_et_marne-.../ -> 410 contre 200 pour /v-paris/
    d-77_seine_et_marne-.../) — après le premier slug, le segment ne sait
    plus relire qu'un seul niveau."""
    groups: dict[str, list[str]] = {}
    for slug in slugs:
        level = slug.partition("-")[0]
        groups.setdefault(level, []).append(slug)

    levels = [level for level in _LEVEL_ORDER if level in groups]
    levels += [level for level in groups if level not in _LEVEL_ORDER]
    return "/".join(_merged_slug(groups[level]) for level in levels)


def _is_merged_slug(slug: str) -> bool:
    """Ce segment porte-t-il plusieurs périmètres ?

    Un slug naturel ne contient jamais de tiret interne : les noms y sont
    normalisés en underscores (`d-77_seine_et_marne`, `v-st+etienne`,
    `cp-75015`). Un tiret APRÈS le préfixe de niveau ne peut donc venir que
    d'une fusion (_merged_slug)."""
    body = re.sub(r"^[a-z]+-", "", slug, count=1)
    return "-" in body


def _url_filters(criteria: dict) -> list[str]:
    """Les segments de filtres prix/surface/pièces de l'URL de recherche.

    Grammaire vérifiée en direct (chaque règle par confrontation 200/410) :

    - le trio surface -> terrain -> budget est ATOMIQUE : tout sous-ensemble
      renvoie 410, dans l'ordre fixe s-, st-, b- ; on émet donc les trois dès
      qu'un filtre du trio est demandé, avec des neutres sinon (`st-0-` :
      la surface terrain n'a pas d'équivalent canonique) ;
    - le minimum du budget doit rester 0 (`b-500-2000` renvoie 410, en
      location comme en achat) : le site ne sait filtrer QUE le maximum — un
      priceMin seul reste appliqué côté scraper ;
    - p-N filtre EXACTEMENT n pièces (page p-2 : uniquement des 2 pièces,
      vérifié sur cartes réelles), un seul segment p autorisé ; le « 5 »
      canonique signifiant « 5 et plus », et plusieurs valeurs étant
      inexpressibles, p n'est émis que pour une valeur unique < 5 ;
    - ces segments forcent la forme « f » même avec un seul type demandé
      (`location-appartement/{slug}/p-2/` sans « f » renvoie 410)."""
    surface_min = criteria.get("surfaceMin")
    surface_max = criteria.get("surfaceMax")
    price_max = criteria.get("priceMax")
    filters: list[str] = []
    if surface_min or surface_max or price_max:
        filters.append(f"s-{surface_min or 0}-{surface_max or ''}")
        filters.append("st-0-")
        filters.append(f"b-0-{price_max or ''}")

    rooms = criteria.get("rooms") or []
    if len(rooms) == 1 and isinstance(rooms[0], int) and 0 < rooms[0] < 5:
        filters.append(f"p-{rooms[0]}")
    return filters


def _resolvable_targets(location: dict) -> list[dict]:
    """Les localisations concrètes à résoudre pour ce périmètre.

    Une ville, une ville entière ou un département se résout lui-même ; une
    région est élargie à ses départements — Century 21 n'a aucun identifiant
    de niveau région (autocomplete muet et /r-11/ renvoie 410, vérifié en
    direct), même principe que bienici. La liste des départements portée par
    la localisation canonique est utilisée d'abord ; l'API geo n'est interrogée
    qu'en repli si elle manque. JAMAIS de développement en liste de communes :
    chaque département garde sa page de résultats propre."""
    kind = location.get("kind", CITY)
    if kind != REGION:
        return [location]
    departments = [str(code) for code in (location.get("departments") or []) if code]
    if departments:
        return [{"kind": DEPARTMENT, "code": code} for code in departments]

    from core.geocode import region_departments

    return [
        {"kind": DEPARTMENT, "code": code}
        for code in region_departments(location.get("code") or "")
    ]


def _fetch_with_retries(session, url: str) -> str:
    """GET avec backoff sur les erreurs réseau et le 410 (quota anti-bot).

    Century 21 renvoie un HTTP 410 « Gone » à la fois pour une page retirée
    et pour un quota anti-bot temporaire (vérifié en direct : une URL valide
    renvoie 410 à une requête non-browser après trop d'appels, puis 200 au
    navigateur). Le 410 est donc retenté avec un backoff exponentiel avant de
    conclure, contrairement au 404 (localisation inexistante, définitive)."""
    last_error: Exception | None = None
    for attempt in range(_HTTP_RETRIES):
        if attempt > 0:
            time.sleep(2 ** attempt)
        try:
            resp = session.get(url, timeout=15)
        except requests.exceptions.RequestException as e:
            last_error = e
            logger.warning(f"[Century21] Erreur réseau ({url}) : {e}")
            continue
        if resp.status_code == 404:
            raise ValueError(f"localisation invalide ({url})")
        if resp.status_code == 410:
            last_error = RuntimeError(f"HTTP 410 ({url}) — quota anti-bot ou page retirée")
            logger.warning(f"[Century21] HTTP 410, retentative ({url})")
            continue
        resp.raise_for_status()
        return resp.text
    raise ValueError(
        f"page inaccessible après {_HTTP_RETRIES} tentatives ({url}) : {last_error}"
    )


def _parse_price(text: str) -> float | None:
    """Le montant d'un texte de prix, en float. « 340 000 € » -> 340000,
    « 820,30 € par mois charges comprises » -> 820.3."""
    m = _PRICE_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\s", "", m.group(1)).replace(",", ".")
    try:
        return float(digits)
    except ValueError:
        return None


def _property_type_label(title: str) -> str:
    """Le type de bien en clair, lu dans le titre de la carte (le site n'a
    pas de champ structuré). « Appartement F2 à vendre PARIS » ->
    « Appartement » ; « Studio à vendre » -> « Studio »."""
    lowered = title.casefold()
    for label, token in (
        ("Appartement", "appartement"),
        ("Maison", "maison"),
        ("Parking", "parking"),
        ("Terrain", "terrain"),
    ):
        if token in lowered:
            return label
    if lowered.startswith("studio"):
        return "Studio"
    return ""


def _card_image(card) -> str:
    """L'URL absolue de la première photo de la carte (src relatif)."""
    img = card.find("img")
    src = (img.get("src") or img.get("data-src") or "").strip() if img else ""
    if not src or src.startswith("data:"):
        return ""
    return f"{BASE_URL}{src}" if src.startswith("/") else src


def _heading_text(card, selector: str) -> str:
    """Le texte d'un élément précis de la carte (heading ville/prix/titre),
    espaces resserrés — jamais le texte de la carte entière, qui ferait
    remonter « Exclusivité », la description ou des surfaces parasites."""
    el = card.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def _parse_cards(html: str) -> list[dict]:
    """Les cartes de la section de vrais résultats, jamais celles de la
    section « Biens se rapprochant » (conteneur `.c-the-list-of-properties-
    related`, hors périmètre) ni les blocs publicitaires (pas de `data-uid`).

    Une carte est un `div.c-the-property-thumbnail-with-content[data-uid]`
    situé dans `.c-the-list-of-properties-list` : c'est ce chemin que le
    sélecteur rend explicite. Chaque champ est lu dans son élément dédié
    (heading-4 = ville + CP + surface + pièces, heading-3 = titre, heading-1
    = prix), pas dans le texte global de la carte."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    container = soup.select_one(".c-the-list-of-properties-list.js-the-list-of-properties-list")
    if container is None:
        return results

    for card in container.select(".c-the-property-thumbnail-with-content[data-uid]"):
        uid = card.get("data-uid")
        if not uid:
            continue

        detail_link = None
        for a in card.find_all("a", href=True):
            if _DETAIL_PATH_RE.search(a["href"]):
                detail_link = a
                break
        if detail_link is None:
            continue

        title = detail_link.get("aria-label") or (detail_link.get("title") or "")
        if not title:
            title = _heading_text(card, ".c-text-theme-heading-3")

        heading = _heading_text(card, ".c-text-theme-heading-4")
        price_text = _heading_text(card, ".c-text-theme-heading-1")

        city_zip = _CITY_ZIP_RE.search(heading) if heading else None
        surface_m = _SURFACE_RE.search(heading) if heading else None
        rooms_m = _ROOMS_RE.search(heading) if heading else None

        results.append({
            "uid": uid,
            "url": f"{BASE_URL}{detail_link['href']}",
            "title": title,
            "price": _PRICE_RE.search(price_text) if price_text else None,
            "price_value": _parse_price(price_text),
            "city": city_zip.group(1).strip() if city_zip else "",
            "zip_code": city_zip.group(2) if city_zip else "",
            "surface": surface_m.group(1) if surface_m else "",
            "rooms": rooms_m.group(1) if rooms_m else "",
            "description": _card_description(card),
            "image_url": _card_image(card),
            "property_type": _property_type_label(title),
        })
    return results


def _card_description(card) -> str:
    """La description de la carte, tronquée comme les autres sources."""
    desc = card.select_one(".c-text-theme-base")
    if desc is None:
        return ""
    return desc.get_text(" ", strip=True)[:300]


def _location_ok(zip_code: str, locations: list[dict]) -> bool:
    """Le code postal lu sur la carte est-il couvert par l'un des périmètres ?

    Century 21 affiche le code postal COMPLET pour les arrondissements de
    Paris/Lyon/Marseille (75019), mais le tronque pour les villes simples
    (Montrouge -> « 92 », Nantes -> « 44 », vérifié en direct) et parfois au-
    delà du département (Corse-du-Sud -> « 201 », vérifié en direct, alors
    que le préfixe du département 2A est « 20 »). Un code complet se compare
    strictement (matches_locations) ; un code incomplet est couvert dès qu'il
    est compatible par préfixe avec l'un des préfixes attendus, DANS UN SENS
    OU DANS L'AUTRE (« 92 » tombe sous « 92120 » ; « 201 » couvre « 20 »).

    Même contrat que Laforet sur le code absent : une annonce sans code postal
    lisible ne passe jamais (échec fermé — c'est aussi ce qui tient la section
    « Biens se rapprochant » hors périmètre)."""
    if not zip_code:
        return False
    if len(zip_code) >= 5:
        return matches_locations(zip_code, locations)

    from core.criteria import location_postal_prefixes

    return any(
        prefix.startswith(zip_code) or zip_code.startswith(prefix)
        for loc in locations
        for prefix in location_postal_prefixes(loc)
    )


def _passes_filters(listing: Listing, criteria: dict, locations: list[dict]) -> bool:
    """Applique nous-mêmes les filtres localisation + prix/surface/pièces."""
    if not _location_ok(listing.zip_code, locations):
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
    if listing.surface:
        try:
            surface = float(listing.surface.replace(",", "."))
        except ValueError:
            surface = None
        if surface is not None:
            if surface_min and surface < surface_min:
                return False
            if surface_max and surface > surface_max:
                return False

    rooms_filter = criteria.get("rooms")
    if rooms_filter and listing.rooms:
        try:
            room_count = int(listing.rooms)
            allowed = {int(r) for r in rooms_filter}
        except (ValueError, TypeError):
            return True
        # "5" dans le vocabulaire partagé signifie "5 et plus" (voir
        # search_edit.html).
        if not any(room_count == r or (r >= 5 and room_count >= 5) for r in allowed):
            return False

    return True


def _dict_to_listing(data: dict) -> Listing:
    """Une carte brute de _parse_cards en Listing."""
    price = data["price"]
    price_text = f"{price.group(1).strip()} €" if price else ""
    return Listing(
        listing_id=f"c21_{data['uid']}",
        url=data["url"],
        title=data["title"],
        price=price_text,
        surface=data["surface"],
        rooms=data["rooms"],
        location=data["city"],
        city=data["city"],
        zip_code=data["zip_code"],
        description=data["description"],
        image_url=data["image_url"],
        property_type=data["property_type"],
        source="century21",
        legacy_id=data["uid"],
        price_value=data["price_value"],
        # La carte ne porte aucune agence structurée (l'agence n'apparaît que
        # dans la description) : le champ reste vide, le blacklist par agence
        # ne s'applique pas à cette source.
        agency="",
        # Issue #12 : le balisage des cartes Century21 (data-uid, aria-label,
        # h3, description) ne contient aucune date de publication. Sentinelle
        # explicite plutôt que chaîne vide.
        creation_date=DATE_INCONNUE,
    )


@ParserRegistry.register
class Century21Parser(BaseParser):
    """Scrape Century21.fr via server-rendered HTML."""

    SOURCE_ID = "century21"
    SOURCE_NAME = "Century 21"
    SOURCE_DESCRIPTION = "Century21.fr — scraping HTML serveur (ville, arrondissement)"

    # Century 21 couvre les quatre types canoniques (appartement, maison,
    # parking, terrain — vérifié en direct sur /annonces/f/achat-parking-
    # terrain/) et les deux transactions : SUPPORTED_* gardent leur défaut.

    MANUAL_OVERRIDE_LABEL = "slug(s) Century 21 (optionnel)"
    MANUAL_OVERRIDE_HELP = (
        "Inutile en principe : le lieu est résolu automatiquement depuis la "
        "ville. À ne renseigner que si une recherche Century 21 ne trouve "
        "rien — collez un ou plusieurs slugs d'URL séparés par des virgules "
        "(ex. v-paris, cp-75001, trouvables dans l'adresse d'une recherche "
        "century21.fr)."
    )

    URL_NOTE = (
        "Le lien reprend les filtres exprimables par Century 21 (surface, "
        "prix maximum, pièces exactes) ; le prix minimum et les listes de "
        "pièces (« 3 et 4 », « 5 et plus ») sont appliqués par le scraper "
        "lui-même sur chaque annonce."
    )

    def _geo_repo(self):
        """Le cache persistant slugs <- périmètre, ou None s'il n'y a pas de
        storage — jamais lu depuis `flask.current_app` : le scraping s'exécute
        sur un thread de fond, hors contexte d'application."""
        return getattr(self.storage, "century21_geo", None) if self.storage else None

    def _slugs(self, criteria: dict, locations: list[dict]) -> list[tuple[list[dict], str]]:
        """Les couples (localisations couvertes, segment d'URL) à scraper.

        Les slugs collés à la main d'abord : UN couple par slug, sans fusion —
        l'utilisateur a fixé ce découpage lui-même et chaque slug garde son
        isolement en cas d'erreur (le périmètre couvert reste la localisation
        appariée par zip strict=False).

        Sinon, chaque localisation est résolue individuellement (une région
        est élargie à ses départements via _resolvable_targets, chaque slug
        étant caché sous sa clé `dept:{code}` ; la copie « scoped » porte les
        codes demandés pour que le filtrage par préfixes postaux en aval
        fonctionne même quand la liste a dû être demandée à l'API geo — les
        critères ne sont jamais mutés, ils sont partagés entre sources). Puis
        TOUS les slugs résolus sont FUSIONNÉS en UNE seule URL multi-segments
        (_level_segments) : le moteur du site accepte plusieurs périmètres par
        recherche à condition de respecter un niveau par segment, donc toute
        recherche fusible se scrape en UNE seule série de pages, et le couple
        porte TOUTES les localisations couvertes pour le filtrage aval. Un
        périmètre dont la résolution échoue est écarté avec un avertissement
        clair — jamais développé en liste de communes."""
        manual = source_overrides(criteria, self.SOURCE_ID).get("slugs")
        if manual:
            # L'utilisateur peut coller plus ou moins de slugs que de villes :
            # zip strict=False épouse ce qu'il y a, dans l'ordre.
            return [([location], slug) for location, slug in zip(locations, manual, strict=False)]

        repo = self._geo_repo()
        if repo is None:
            logger.warning(
                "[Century21] Aucun storage fourni au parser, résolution du slug impossible "
                "(voir get_parser(source, storage=...))"
            )
            return []

        from services import century21_geocode

        def resolve(target: dict) -> str | None:
            slug = century21_geocode.resolve_slug_id(target, repo=repo)
            if not slug:
                logger.warning(
                    f"[Century21] Aucun slug résolu pour {century21_geocode._describe(target)}"
                )
            return slug

        contributions: list[tuple[dict, list[str]]] = []
        for location in locations:
            if location.get("kind", CITY) != REGION:
                slug = resolve(location)
                if slug:
                    contributions.append((location, [slug]))
                continue

            targets = _resolvable_targets(location)
            if not targets:
                logger.warning(
                    f"[Century21] {_describe(location)} sans départements identifiables, ignorée"
                )
                continue
            slugs = [s for target in targets if (s := resolve(target))]
            if not slugs:
                logger.warning(
                    f"[Century21] {_describe(location)} sans départements identifiables, ignorée"
                )
                continue
            # La copie porte les codes demandés : le filtrage par préfixes
            # postaux en aval (location_postal_prefixes sur une région) lit
            # `departments`, qui manque quand la liste a dû être demandée à
            # l'API geo. Jamais mutés : les critères sont partagés entre
            # sources pendant un scrape.
            scoped = {**location, "departments": [t["code"] for t in targets]}
            contributions.append((scoped, slugs))

        if not contributions:
            return []

        # La concaténation de deux slugs de ville en retirant le second
        # préfixe (`v-montrouge-nantes`) répond 200 mais ne garantit pas une
        # union des deux villes. Deux segments (`v-montrouge/v-nantes`)
        # répondent 410 (vérifié le 2026-08-29). Chaque contribution v-/cpv-
        # garde donc sa propre série ; les niveaux dont l'union est vérifiée
        # (cp-/d-) restent fusionnés.
        isolated = []
        mergeable = []
        for location, slugs in contributions:
            city_slugs = [slug for slug in slugs if slug.startswith(("v-", "cpv-"))]
            other_slugs = [slug for slug in slugs if slug not in city_slugs]
            isolated.extend(([location], slug) for slug in city_slugs)
            if other_slugs:
                mergeable.append((location, other_slugs))

        if not mergeable:
            return isolated
        covered = [location for location, _ in mergeable]
        segment = _level_segments([slug for _, slugs in mergeable for slug in slugs])
        return [*isolated, (covered, segment)]

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

        from services import century21_geocode

        try:
            century21_geocode.remember_manual_slugs(criteria, repo=repo)
        except Exception as e:
            logger.debug(f"[Century21] Slug manuel non mémorisé : {e}")

    def to_native(self, criteria: dict) -> dict:
        """Rien à traduire : Century 21 construit ses URLs directement depuis
        le canonique (voir build_search_urls), et les bornes prix/surface/
        pièces sont appliquées côté scraper par _passes_filters, qui lit lui
        aussi le canonique."""
        return criteria

    # ------------------------------------------------------------------
    # Reconstruction d'URL
    # ------------------------------------------------------------------

    def _base_path(self, criteria: dict, slug: str) -> str:
        """Le chemin de la page de résultats pour ce slug de localisation.

        Le format du chemin dépend du NOMBRE de types demandés ET de la
        présence de filtres ou d'un slug multi-périmètres (vérifié en direct
        sur des URLs indexées et en live) :

            1 périmètre   aucun type  /annonces/f/achat/{slug}/
                          un type     /annonces/achat-appartement/{slug}/ (sans « f »)
                          deux +      /annonces/f/achat-maison-appartement/{slug}/
            plusieurs     quel que soit le nombre de types :
            périmètres    /annonces/f/{transaction}[-{types}]/{slug fusionné}/

        Les segments de filtres (_url_filters) s'ajoutent après la
        localisation et FORCENT la forme « f », même avec un seul type :
        /annonces/location-appartement/v-paris/p-2/ renvoie 410 contre 200
        pour sa variante « f ». Le même segment avec un seul type MAIS le
        « f » (ou plusieurs types sans lui) renvoie aussi un HTTP 410 : c'est
        le format exact du site qui décide, pas une tolérance de parsing."""
        transaction = TRANSACTION_SLUGS[_transaction(criteria)]
        type_slugs = _type_slugs(criteria)
        filter_segments = _url_filters(criteria)
        suffix = "".join(f"/{segment}" for segment in filter_segments)
        if not type_slugs:
            return f"{BASE_URL}/annonces/f/{transaction}/{slug}{suffix}/"
        types = "-".join(type_slugs)
        if len(type_slugs) == 1 and not filter_segments and not _is_merged_slug(slug):
            return f"{BASE_URL}/annonces/{transaction}-{types}/{slug}/"
        return f"{BASE_URL}/annonces/f/{transaction}-{types}/{slug}{suffix}/"

    def build_search_url(self, criteria: dict) -> str | None:
        """La première URL — depuis la fusion (#7), LA recherche complète :
        voir build_search_urls()."""
        urls = self.build_search_urls(criteria)
        return urls[0] if urls else None

    def build_search_urls(self, criteria: dict) -> list[str]:
        """UNE seule URL portant toutes les localisations fusibles — le
        formulaire du site ne référence qu'un périmètre à la fois (le champ se
        désactive après une sélection, vérifié en direct), mais son moteur
        accepte plusieurs périmètres par URL : les slugs résolus sont donc
        regroupés PAR NIVEAU et fusionnés dans une même recherche
        (_level_segments), qui se scrape en UNE seule série de pages. Seuls
        les slugs collés à la main gardent une URL propre chacun (_slugs)."""
        locations = get_locations(criteria)
        if not locations:
            return []

        urls = []
        for _covered, segment in self._slugs(criteria, locations):
            urls.append(self._base_path(criteria, segment))
        return urls

    # ------------------------------------------------------------------
    # Scraping
    # ------------------------------------------------------------------

    def scrape(self, criteria: dict) -> list[Listing]:
        locations = get_locations(criteria)
        if not locations:
            raise ValueError("Century 21 nécessite au moins une localisation (ville + code postal) dans les critères")

        resolved = self._slugs(criteria, locations)
        if not resolved:
            raise ValueError(
                "Aucune localisation Century 21 exploitable : Century 21 ne "
                "référence que les villes (arrondissement ou ville entière), "
                "pas les départements ni les régions"
            )

        session = requests.Session()
        session.headers.update({
            "User-Agent": DESKTOP_UA,
            "Accept": "text/html, application/xhtml+xml",
            "Referer": "https://www.century21.fr/",
        })

        seen: set[str] = set()
        listings: list[Listing] = []
        errors: list[str] = []

        for covered, segment in resolved:
            label = ", ".join(_describe(location) for location in covered)
            try:
                listings.extend(
                    self._scrape_slug(session, criteria, covered, segment, seen)
                )
            except Exception as e:
                errors.append(f"{label} ({segment}): {e}")
                logger.warning(f"[Century21] {label}: {e}")

        if errors and len(errors) == len(resolved):
            raise ValueError("; ".join(errors))

        logger.info(f"[Century21] Scraping terminé : {len(listings)} annonces uniques")
        return listings

    def _scrape_slug(self, session, criteria: dict, covered: list[dict], segment: str,
                     seen: set) -> list[Listing]:
        """Une série de pages pour UN segment d'URL fusionné : le contrôle
        aval (_passes_filters) reçoit TOUTES les localisations couvertes par
        ce segment — comme la copie « scoped » du chemin région."""
        base_path = self._base_path(criteria, segment)

        def fetch(page: int) -> str:
            url = base_path if page == 1 else f"{base_path}page-{page}/"
            return _fetch_with_retries(session, url)

        return self._collect_pages(fetch, criteria, covered, seen, segment)

    def _collect_pages(self, fetch, criteria: dict, locations: list[dict],
                       seen: set, label: str) -> list[Listing]:
        """Parcourt les pages de résultats jusqu'à épuisement, en collectant
        les annonces qui passent les filtres.

        La condition d'arrêt est « cette page n'apporte plus aucune annonce
        inédite », et non le nombre total affiché par le site : elle absorbe
        le chevauchement entre pages et n'a pas besoin de lire le compteur.
        MAX_PAGES borne le parcours (même raison que Laforet)."""
        listings: list[Listing] = []
        pages_lues = 0
        vues_ici: set[str] = set()

        for page in range(1, MAX_PAGES + 1):
            cards = _parse_cards(fetch(page))
            pages_lues = page

            nouvelles = 0
            for card in cards:
                uid = card["uid"]
                if uid in vues_ici:
                    continue
                vues_ici.add(uid)
                nouvelles += 1

                if uid in seen:
                    continue
                listing = _dict_to_listing(card)
                if _passes_filters(listing, criteria, locations):
                    seen.add(uid)
                    listings.append(listing)

            if not nouvelles:
                break
        else:
            logger.warning(
                f"[Century21] {label} : limite de {MAX_PAGES} pages atteinte "
                f"({len(listings)} annonces retenues, résultat possiblement partiel)"
            )

        logger.debug(f"[Century21] {label} : {pages_lues} page(s) lue(s), {len(listings)} annonces retenues")
        return listings

    def has_valid_criteria(self, criteria: dict) -> bool:
        """Utilisable dès qu'il y a un slug manuel, ou au moins un périmètre
        de niveau ville, département ou région dont on saura tirer des slugs.

        Une région est valide dès qu'elle porte ses départements (ou son code,
        pour les demander à l'API geo) — voir _resolvable_targets."""
        if source_overrides(criteria, self.SOURCE_ID).get("slugs"):
            return True

        from services.century21_geocode import area_cache_key

        return any(
            loc.get("kind") in (CITY, WHOLE_CITY, DEPARTMENT, REGION) and area_cache_key(loc)
            for loc in get_locations(criteria)
        )
