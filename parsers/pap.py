"""pap.fr listing scraper — HTML server-rendered.

PAP rend ses pages de résultats côté serveur (comme Laforêt et Century 21) :
chaque page de recherche est du HTML qu'il faut parser avec BeautifulSoup.
Vérifié en direct le 22/08/2026.

Anti-bot : le site est derrière Cloudflare, mais le filtre porte sur
l'EMPREINTE TLS du client, pas sur son IP ni sur un vrai challenge JS — curl
et requests nus reçoivent un 403 « Just a moment », tandis qu'une requête à
empreinte navigateur (curl_cffi, impersonation Chrome) passe systématiquement
(~40 requêtes de sondage sans un seul blocage). Même classe de contournement
que DataDome chez SeLoger ; contrairement à lefigaro (vrai mur JS, écarté).

La localisation est encodée dans l'URL de recherche par un identifiant
numérique opaque propre au site (`g439` = Paris, `g37782` = Paris 15e,
`g397` = Gironde, `g471` = Île-de-France), résolu depuis la localisation
canonique via l'autocomplete public /json/ac-geo avec cache persistant en
base (voir services.pap_geocode). Contrairement aux autres sources, TOUS les
niveaux ont un identifiant natif — ville, ville entière, département ET
région : aucune élargissement région -> départements ici.

Grammaire des URLs de recherche (chaque forme vérifiée en direct par la
destination finale après redirection du site) :

    /annonce/vente-immobiliere-{lieu}-g{id}       vente, tous types
    /annonce/vente-appartements-...-g{id}         vente + 1 type (pluriel)
    /annonce/vente-parking-...-g{id}              (sauf « parking », singulier)
    /annonce/locations-{lieu}-g{id}               location, tous types
    /annonce/locations-appartement-...-g{id}      location + 1 type (singulier)
    ...-g{id}-2, -3, ...                          pagination (suffixe numérique)

Trois pièges vérifiés en direct :

- le slug de LIEU est IGNORÉ (seul `-g{id}` compte : un mauvais nom redirige
  proprement vers le bon libellé) ;
- un slug de TYPE non reconnu est lui aussi réécrit silencieusement — la
  recherche perd alors son filtre de type et devient TOUT TYPES (« vente-
  parkings » pluriel ou « locations-terrain » renvoient la page générique) :
  seuls les segments ci-dessus, vérifiés un à un, sont donc émis ;
- la FUSION des périmètres se fait en concaténant leurs identifiants dans UN
  SEUL bloc `g`, SANS tiret entre eux (`...-g439g43267`) — vérifié en direct
  le 23/08/2026 : les ids sont canonisés par le site en ordre croissant
  (g43267g439 -> 301 vers g439g43267), la page fusionnée répond 200 et sert
  les annonces des DEUX périmètres, y compris entre niveaux (deux
  départements, g442g456) ; la pagination -2/-3 s'y applique normalement.
  Deux blocs séparés par un tiret (-g439-g43267) NE marchent PAS :
  redirection vers le seul premier périmètre. D'où l'émission retenue : UNE
  série PAR TYPE demandé, TOUTES les localisations regroupées dans chaque
  série (ids triés en ordre NUMÉRIQUE croissant pour éviter la 301).

Les filtres prix et surface ONT un équivalent natif dans l'URL, vérifié en
direct sous toutes ses formes (seule, combinée, avec pagination) :

    -jusqu-a-{N}-euros / -a-partir-de-{N}-euros / -entre-{a}-et-{b}-euros
    -jusqu-a-{N}-m2     / -a-partir-de-{N}-m2     / -entre-{a}-et-{b}-m2

(le site renormalise lui-même l'ordre et les formes via redirection). Ils ne
sont pas optionnels : la pagination HTML plafonne à 25 PAGES côté serveur
(vérifié en direct : la page 26 recycle le début des résultats), soit ~330
annonces — sans filtres émis, une recherche large serait tronquée. La fusion
des périmètres a un compromis assumé : ce plafond vaut PAR SÉRIE, regrouper
plusieurs périmètres augmente donc le volume par série — et avec lui le
risque d'un résultat partiel, signalé par les warnings déjà prévus
(_DEPTH_CAP_SUSPECT / MAX_PAGES). Les listes
de pièces (« 3 et 4 », « 5 et plus »), sans équivalent fiable (la sémantique
du segment -N-pieces du site est ambiguë), restent appliquées côté scraper par
_passes_filters, qui rejoue TOUS les critères sur chaque annonce — filet
exact même quand l'URL a déjà filtré.

La carte de résultat (vérifiée en direct) :

    <div class="search-list-item-alt">
      <div class="item-body">
        <a class="item-title" href="/annonces/{slug}-r{id}" name="{id}">
          <span class="item-price">649.000 €</span>
          <span class="h1">Paris 15E (75015)</span>
          <ul class="item-tags"><li>3 pièces</li><li>2 chambres</li><li>61,70 m²</li></ul>
        </a>
        <p class="item-description">...</p>
      </div>
    </div>

Le titre de carte EST la ligne de localisation (« Paris 15E (75015) ») ; le
code postal y est complet, parfois précédé d'un préfixe descriptif (« Maison
138 m² à construire Cestas (33610) ») — le regex lit la parenthèse FINALE.
PAP étant une plateforme de particuliers, aucune agence n'apparaît : agency
reste vide (le blacklist par agence ne s'applique pas à cette source) et
is_private vaut True.
"""

from __future__ import annotations

import re
import time
import unicodedata

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
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
from parsers.base import BaseParser, ParserRegistry, get_locations

BASE_URL = "https://www.pap.fr"

# Empreinte TLS navigateur : sans elle, Cloudflare répond un challenge 403
# « Just a moment » aux clients curl/requests (vérifié en direct).
IMPERSONATE = "chrome124"

MAX_PAGES = 60
# PAP affiche ~13 cartes par page (contre ~20-30 chez Century 21) : Paris 15e
# en vente appartements totalise ~855 annonces (~66 pages). Au-delà de la
# limite, le résultat est partiel et signalé comme tel (warning).
_HTTP_RETRIES = 3
# Petit délai entre deux pages d'une même série : assurance anti-burst, les
# sondages n'ayant montré aucune limite de débit à ce jour.
_PAGE_DELAY_SECONDS = 0.3
# À partir de cette profondeur, un arrêt « plus rien d'inédit » est signalé
# comme un plafond de profondeur probable du site (25 pages, vérifié en
# direct) plutôt que comme une fin naturelle de résultats.
_DEPTH_CAP_SUSPECT = 20

# Segments d'URL vérifiés un à un en direct (un slug de type non répertorié
# serait réécrit silencieusement en page générique TOUT TYPES par le site —
# voir la docstring du module). Le terrain n'est référencé ni en « vente-
# parkings » ni en « locations-terrain » : ce dernier n'existe pas (PAP ne
# propose pas de terrains à louer).
TRANSACTION_PATHS = {BUY: "vente-immobiliere", RENT: "locations"}
TYPE_PATHS = {
    BUY: {
        APARTMENT: "vente-appartements",
        HOUSE: "vente-maisons",
        LAND: "vente-terrains",
        PARKING: "vente-parking",
    },
    RENT: {
        APARTMENT: "locations-appartement",
        HOUSE: "locations-maison",
        PARKING: "locations-parking",
    },
}

# Lien fiche annonce : le suffixe -r{id} du chemin (le name= de la balise
# porte la même valeur, mais seul le href est garanti).
_DETAIL_PATH_RE = re.compile(r"/annonces/[\w-]*-r(\d+)")

# Textes extraits de la carte. Les prix PAP utilisent le POINT comme
# séparateur de milliers (« 649.000 € ») et la virgule décimale en location
# (« 820,30 € ») — voir _parse_price. Le code postal est lu dans la
# parenthèse FINALE de la ligne de localisation (des titres portent un
# préfixe descriptif : « Maison 138 m² à construire Cestas (33610) »).
_PRICE_RE = re.compile(r"([\d\s.,]+?)\s*(?:€|\beuros?\b)", re.IGNORECASE)
_LOCATION_LINE_RE = re.compile(r"^(.*?)\s*\((\d{2,5})\)\s*$", re.DOTALL)
_SURFACE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m\s*[²2]")
_ROOMS_RE = re.compile(r"(\d+)\s*pi[èe]ce")
_BEDROOMS_RE = re.compile(r"(\d+)\s*chambre")

# Marqueur des annonces « proches » que PAP INTERCALE dans le flux paginé à
# partir des premières pages (aucune structure dédiée, vérifié en direct :
# mêmes classes et conteneur que les vraies cartes). Leur ligne de
# localisation porte une distance (« Vanves (92170) à 2km de Paris 15e »,
# « Paris 14E (75014) à 2km de Paris 15e ») ; jamais une vraie carte. Même
# piège que la section « Biens se rapprochant » de Century 21 et le « à
# proximité » de Laforêt — ici sans conteneur distinct, d'où un filtre
# textuel. Elles sont écartées au parsing : c'est aussi ce qui fait terminer
# le parcours dès la fin des vrais résultats.
_PROXIMITY_RE = re.compile(r"\bà\s+\d+(?:[.,]\d+)?\s*km\b", re.IGNORECASE)


def _url_filter_segments(criteria: dict) -> list[str]:
    """Les segments de filtres prix/surface NATIFS de PAP, dans l'ordre
    canonique du site (prix avant surface ; formes vérifiées en direct,
    seules puis combinées, avec et sans pagination).

    Ces segments réduisent le pool sous le plafond de profondeur de la
    pagination (25 pages, voir la docstring du module) : sans eux, une
    recherche large serait tronquée par le site lui-même."""
    segments: list[str] = []
    price_min = criteria.get("priceMin")
    price_max = criteria.get("priceMax")
    if price_min and price_max:
        segments.append(f"entre-{int(price_min)}-et-{int(price_max)}-euros")
    elif price_max:
        segments.append(f"jusqu-a-{int(price_max)}-euros")
    elif price_min:
        segments.append(f"a-partir-de-{int(price_min)}-euros")

    surface_min = criteria.get("surfaceMin")
    surface_max = criteria.get("surfaceMax")
    if surface_min and surface_max:
        segments.append(f"entre-{int(surface_min)}-et-{int(surface_max)}-m2")
    elif surface_max:
        segments.append(f"jusqu-a-{int(surface_max)}-m2")
    elif surface_min:
        segments.append(f"a-partir-de-{int(surface_min)}-m2")
    return segments


def _transaction(criteria: dict) -> str:
    """La transaction canonique demandée. La location par défaut : c'est ce
    que le formulaire propose en premier (même convention que Laforet et
    Century21)."""
    transaction = criteria.get("transaction")
    return transaction if transaction in TRANSACTION_PATHS else RENT


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


def _location_slug(location: dict) -> str:
    """Un slug de lieu lisible pour l'URL — PUREMENT alphabétique.

    Le site IGNORE ce segment (seul -g{id} compte, un mauvais nom redirige
    vers le bon libellé), MAIS la réécriture qu'il applique aux slugs qu'il
    ne reconnaît pas fait PERDRE le suffixe de pagination : vérifié en
    direct, /annonce/vente-appartements-paris-75015-g37782-2 est redirigé
    vers la page 1 (le -2 disparaît quand le slug doit être renormalisé,
    « 75015 » se terminant par des chiffres), tandis que les trois formes
    alphabétiques testées paginent correctement (paris, bordeaux ->
    gironde-33, courbevoie). D'où l'absence du code postal ici : jamais de
    chiffre en fin de slug."""
    kind = location.get("kind", CITY)
    if kind in (DEPARTMENT, REGION):
        raw = location.get("name") or str(location.get("code") or "france")
    else:
        raw = location.get("city") or "france"
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    segments = [s for s in re.split(r"[^A-Za-z]+", ascii_text.lower()) if s]
    return "-".join(segments) or "france"


def _geo_block(geo_ids: list[str]) -> str:
    """Les identifiants CONCATÉNÉS d'une série fusionnée, tels qu'ils suivent
    le `g` de l'URL (« 439g43267 ») — sans aucun tiret entre eux : la forme
    -g439-g43267 ne porte que le premier périmètre (redirection vérifiée en
    direct, voir la docstring du module)."""
    return "g".join(geo_ids)


def _sorted_geo_pairs(pairs: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    """Les paires (localisation, geo_id) dédoublonnées par identifiant puis
    triées en ordre NUMÉRIQUE croissant.

    Le site canonise lui-même le bloc g en ordre croissant (g43267g439 ->
    301 vers g439g43267, vérifié en direct) : trier à l'émission évite cette
    redirection ET rend l'ordre déterministe quel que soit l'ordre des ids
    saisis à la main ou résolus. Le tri est numérique (l'ordre lexical
    mettrait « 43267 » devant « 439 ») ; un id manuel non numérique —
    aberrant — passe après tous les autres sans faire lever le tri."""
    uniques: dict[str, dict] = {}
    for location, geo_id in pairs:
        uniques.setdefault(geo_id, location)
    return [
        (location, geo_id)
        for geo_id, location in sorted(
            uniques.items(),
            key=lambda item: (
                not item[0].isdigit(),
                int(item[0]) if item[0].isdigit() else -1,
            ),
        )
    ]


def _fetch_with_retries(session, url: str) -> str:
    """GET avec backoff sur les erreurs réseau et le challenge Cloudflare.

    Le 403 « Just a moment » (empreinte TLS non navigateur, quota, pic
    passager) est retenté avec un backoff exponentiel avant de conclure ;
    le 404 est définitif (localisation inexistante)."""
    last_error: Exception | None = None
    for attempt in range(_HTTP_RETRIES):
        if attempt > 0:
            time.sleep(2 ** attempt)
        try:
            resp = session.get(url, timeout=15)
        except Exception as e:
            last_error = e
            logger.warning(f"[PAP] Erreur réseau ({url}) : {e}")
            continue
        if resp.status_code == 404:
            raise ValueError(f"localisation invalide ({url})")
        if resp.status_code == 403 or "Just a moment" in resp.text[:3000]:
            last_error = RuntimeError(f"challenge Cloudflare ({url})")
            logger.warning(f"[PAP] HTTP 403/challenge, retentative ({url})")
            continue
        resp.raise_for_status()
        return resp.text
    raise ValueError(
        f"page inaccessible après {_HTTP_RETRIES} tentatives ({url}) : {last_error}"
    )


def _clean_text(text: str | None) -> str:
    """Le texte d'un élément de carte : espaces insécables aplatis."""
    return re.sub(r"[\s\xa0]+", " ", text or "").strip()


def _parse_price(text: str) -> float | None:
    """Le montant d'un texte de prix, en float.

    PAP écrit ses milliers au POINT (« 649.000 € » = 649000) et ses décimales
    à la virgule en location (« 820,30 € » = 820.3) : une virgule présentе
    fait d'elle le séparateur décimal (les points restant des milliers),
    sinon les points sont des milliers."""
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    raw = re.sub(r"[\s\xa0]", "", m.group(1)).strip(",")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _property_type_label(detail_href: str) -> str:
    """Le type de bien en clair, lu dans le slug de l'URL de la fiche (la
    carte n'a pas de champ structuré). « /annonces/appartement-paris-15e-...
    -r123 » -> « Appartement » ; les garages/boxes comptent comme Parking."""
    lowered = detail_href.casefold()
    for token, label in (
        ("appartement", "Appartement"),
        ("studio", "Studio"),
        ("maison", "Maison"),
        ("terrain", "Terrain"),
        ("parking", "Parking"),
        ("garage", "Parking"),
        ("-box-", "Parking"),
    ):
        if token in lowered:
            return label
    return ""


def _card_image(card) -> str:
    """L'URL absolue de la première photo de la carte (src relatif)."""
    img = card.find("img") if card else None
    src = (img.get("src") or img.get("data-src") or "").strip() if img else ""
    if not src or src.startswith("data:"):
        return ""
    return f"{BASE_URL}{src}" if src.startswith("/") else src


def _parse_cards(html: str) -> list[dict]:
    """Les cartes de résultats d'une page.

    Une carte est repérée par son lien `a.item-title` pointant une fiche
    /annonces/...-r{id} — les blocs publicitaires (immoneuf.com, annonces
    sponsorisées sans fiche -r{id}) n'ont pas un tel lien et sortent
    naturellement. Les cartes « proches » (distance dans la ligne de
    localisation, voir _PROXIMITY_RE) sont écartées ici. Chaque champ est lu
    dans son élément dédié, jamais dans le texte global de la carte."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    for link in soup.select("a.item-title[href]"):
        href = link.get("href") or ""
        m = _DETAIL_PATH_RE.search(href)
        if not m:
            continue
        uid = m.group(1)

        # Les cartes partenaires pointent une URL déjà ABSOLUE (r33480 ->
        # https://www.acceslogement.fr/..., capture réelle) : la garder telle
        # quelle, ne JAMAIS préfixer (et surtout pas jeter la carte — le
        # comptage des fiches doit rester identique).
        card_url = href if href.startswith("http") else f"{BASE_URL}{href}"

        line = _clean_text(link.select_one(".h1").get_text(" ", strip=True)) \
            if link.select_one(".h1") else ""
        if _PROXIMITY_RE.search(line):
            continue
        price_el = link.select_one(".item-price")
        price_text = _clean_text(price_el.get_text(" ", strip=True)) if price_el else ""

        city, zip_code = "", ""
        line_m = _LOCATION_LINE_RE.match(line)
        if line_m:
            city = _clean_text(line_m.group(1))
            zip_code = line_m.group(2)

        rooms = bedrooms = surface = ""
        tags = link.select(".item-tags li")
        for tag in tags:
            text = _clean_text(tag.get_text(" ", strip=True))
            rooms_m = _ROOMS_RE.search(text)
            bedrooms_m = _BEDROOMS_RE.search(text)
            surface_m = _SURFACE_RE.search(text)
            if rooms_m and not rooms:
                rooms = rooms_m.group(1)
            elif bedrooms_m and not bedrooms:
                bedrooms = bedrooms_m.group(1)
            elif surface_m and not surface:
                surface = surface_m.group(1).replace(" ", "")

        card = link.find_parent(class_=re.compile(r"^search-list-item"))
        description = ""
        desc_el = card.select_one(".item-description") if card else None
        if desc_el is None:
            desc_el = link.find_next("p", class_="item-description")
        if desc_el is not None:
            description = _clean_text(desc_el.get_text(" ", strip=True))[:300]

        results.append({
            "uid": uid,
            "url": card_url,
            "title": line,
            "price_text": price_text,
            "price_value": _parse_price(price_text),
            "city": city,
            "zip_code": zip_code,
            "surface": surface,
            "rooms": rooms,
            "bedrooms": bedrooms,
            "description": description,
            "image_url": _card_image(card),
            "property_type": _property_type_label(href),
        })
    return results


def _dict_to_listing(data: dict) -> Listing:
    """Une carte brute de _parse_cards en Listing."""
    return Listing(
        listing_id=f"pap_{data['uid']}",
        url=data["url"],
        title=data["title"],
        price=data["price_text"],
        surface=data["surface"],
        rooms=data["rooms"],
        location=data["city"],
        city=data["city"],
        zip_code=data["zip_code"],
        description=data["description"],
        image_url=data["image_url"],
        property_type=data["property_type"],
        source="pap",
        legacy_id=data["uid"],
        price_value=data["price_value"],
        # PAP est une plateforme de particulier à particulier : pas d'agence
        # sur les cartes (le blacklist par agence ne s'applique pas à cette
        # source) et toutes les annonces sont des vendeurs privés.
        agency="",
        is_private=True,
    )


def _location_ok(zip_code: str, locations: list[dict]) -> bool:
    """Le code postal lu sur la carte est-il couvert par l'un des périmètres ?

    PAP affiche le code postal COMPLET sur ses cartes (« Cestas (33610) »,
    « Paris 15E (75015) », vérifié en direct). Un code complet se compare
    strictement (matches_locations) ; un code incomplet serait couvert dès
    qu'il est compatible par préfixe avec l'un des préfixes attendus, DANS UN
    SENS OU DANS L'AUTRE — même contrat que Century 21.

    Même contrat que Laforet et Century21 sur le code absent : une annonce
    sans code postal lisible ne passe jamais (échec fermé)."""
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
    """Applique nous-mêmes TOUS les filtres (localisation + prix/surface/pièces).

    Le prix et la surface sont déjà portés par l'URL quand les critères les
    définissent (_url_filter_segments) : ce rejeu est le filet exact qui
    garantit qu'aucune annonce ne dépasse les critères, quelle que soit la
    conduite du site."""
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


@ParserRegistry.register
class PapParser(BaseParser):
    """Scrape pap.fr via server-rendered HTML."""

    SOURCE_ID = "pap"
    SOURCE_NAME = "PAP"
    SOURCE_DESCRIPTION = "pap.fr — scraping HTML serveur (ville, département, région)"

    # PAP couvre les quatre types canoniques et les deux transactions
    # (vérifié en direct sur les segments vente-* et locations-*) :
    # SUPPORTED_* gardent leur défaut. Nuance interne : le terrain n'existe
    # qu'en vente (pas de segment « locations-terrain », voir TYPE_PATHS).

    MANUAL_OVERRIDE_LABEL = "identifiant(s) de lieu PAP (optionnel)"
    MANUAL_OVERRIDE_HELP = (
        "Inutile en principe : le lieu est résolu automatiquement depuis la "
        "ville. À ne renseigner que si une recherche PAP ne trouve rien — "
        "collez le ou les identifiants numériques de fin d'URL d'une "
        "recherche pap.fr, séparés par des virgules (ex. 439, 37782 : c'est "
        "le nombre après g dans l'adresse)."
    )

    URL_NOTE = (
        "Le lien porte la transaction, les types, la localisation et les "
        "bornes de prix/surface ; les listes de pièces (« 3 et 4 », "
        "« 5 et plus ») sont appliquées par le scraper lui-même sur chaque "
        "annonce."
    )

    def _geo_repo(self):
        """Le cache persistant identifiants <- périmètre, ou None s'il n'y a
        pas de storage — jamais lu depuis `flask.current_app` : le scraping
        s'exécute sur un thread de fond, hors contexte d'application."""
        return getattr(self.storage, "pap_geo", None) if self.storage else None

    def _pairs(self, criteria: dict, locations: list[dict]) -> list[tuple[dict, str]]:
        """Les couples (localisation, identifiant géo) à scraper : les ids
        collés à la main s'il y en a, sinon ceux résolus depuis les
        localisations (cache puis autocomplete, voir services.pap_geocode).

        Un périmètre dont la résolution échoue est écarté avec un
        avertissement clair — jamais développé en liste de communes."""
        manual = source_overrides(criteria, self.SOURCE_ID).get("geoIds")
        if manual:
            # L'utilisateur peut coller plus ou moins d'ids que de villes :
            # zip strict=False épouse ce qu'il y a, dans l'ordre.
            return list(zip(locations, [str(g) for g in manual], strict=False))

        repo = self._geo_repo()
        if repo is None:
            logger.warning(
                "[PAP] Aucun storage fourni au parser, résolution du lieu impossible "
                "(voir get_parser(source, storage=...))"
            )
            return []

        from services import pap_geocode

        pairs: list[tuple[dict, str]] = []
        for location in locations:
            geo_id = pap_geocode.resolve_geo_id(location, repo=repo)
            if not geo_id:
                logger.warning(
                    f"[PAP] Aucun identifiant résolu pour {pap_geocode._describe(location)}"
                )
                continue
            pairs.append((location, str(geo_id)))
        return pairs

    def _series(self, criteria: dict,
                locations: list[dict]) -> list[tuple[list[dict], list[str], str]]:
        """L'expansion complète du scrape : (localisations couvertes, geo_ids
        triés, segment d'URL) pour CHAQUE type demandé, TOUTES les
        localisations fusibles REGROUPÉES dans chaque série.

        La fusion multi-périmètres est native chez PAP : les ids se
        concatènent dans un seul bloc g (voir la docstring du module), une
        recherche 2 villes × 1 type tient donc dans UNE série au lieu d'une
        par localisation — le slug de nom reste reconstruit depuis la
        première localisation du bloc (celle du premier id trié), le site
        l'ignorant. Le compromis troncature (le plafond de ~25 pages vaut
        par série) est documenté dans la docstring du module.

        Un type demandé sans segment vérifié pour cette transaction (le
        terrain en location, qui n'existe pas sur PAP) est écarté avec un
        avertissement quand d'autres types restent exprimables ; s'il ne
        reste RIEN d'exprimable, l'échec est levé plutôt que d'élargir
        silencieusement à tous les types."""
        pairs = self._pairs(criteria, locations)
        if not pairs:
            return []

        transaction = _transaction(criteria)
        requested = list(criteria.get("propertyTypes") or [])
        expressible = [t for t in requested if t in TYPE_PATHS[transaction]]
        skipped = [t for t in requested if t not in TYPE_PATHS[transaction]]
        if skipped:
            labels = ", ".join(sorted(skipped))
            logger.warning(
                f"[PAP] Type(s) sans recherche dédiée en "
                f"« {'vente' if transaction == BUY else 'location'} », ignorés : {labels}"
            )
            if not expressible:
                raise ValueError(
                    f"PAP ne référence pas ce(s) type(s) de bien en "
                    f"{'vente' if transaction == BUY else 'location'} : {labels}"
                )

        segments: list[str] = [
            TYPE_PATHS[transaction][t] for t in expressible
        ] or [TRANSACTION_PATHS[transaction]]

        fused = _sorted_geo_pairs(pairs)
        fused_locations = [location for location, _ in fused]
        fused_ids = [geo_id for _, geo_id in fused]
        return [(fused_locations, fused_ids, segment) for segment in segments]

    def _base_url(self, criteria: dict, locations: list[dict],
                  geo_ids: list[str], segment: str) -> str:
        """L'URL de la première page d'une série (miroir exact du scrape),
        avec les filtres natifs prix/surface quand les critères en portent.
        Le slug de lieu dérive de la première localisation du bloc (celle du
        premier id trié) : le site ignore ce slug et le reconstruit depuis le
        premier id — l'aligner évite sa renormalisation."""
        url = f"{BASE_URL}/annonce/{segment}-{_location_slug(locations[0])}-g{_geo_block(geo_ids)}"
        filters = _url_filter_segments(criteria)
        if filters:
            url += "-" + "-".join(filters)
        return url

    def parse_manual_override(self, value: str) -> dict:
        value = (value or "").strip()
        if not value:
            return {}
        geo_ids = [s.strip() for s in value.split(",") if s.strip()]
        return {"geoIds": geo_ids} if geo_ids else {}

    def remember_manual_override(self, criteria: dict) -> None:
        """Banque le(s) identifiant(s) saisi(s) à la main contre le périmètre
        de la recherche, pour que la résolution automatique en profite."""
        repo = self._geo_repo()
        if repo is None:
            return

        from services import pap_geocode

        try:
            pap_geocode.remember_manual_geo_ids(criteria, repo=repo)
        except Exception as e:
            logger.debug(f"[PAP] Identifiant manuel non mémorisé : {e}")

    def to_native(self, criteria: dict) -> dict:
        """Rien à traduire : PAP construit ses URLs directement depuis le
        canonique (voir build_search_urls), et les bornes prix/surface/
        pièces sont appliquées côté scraper par _passes_filters, qui lit lui
        aussi le canonique."""
        return criteria

    # ------------------------------------------------------------------
    # Reconstruction d'URL
    # ------------------------------------------------------------------

    def build_search_url(self, criteria: dict) -> str | None:
        """First series' URL — see build_search_urls() for all of them."""
        urls = self.build_search_urls(criteria)
        return urls[0] if urls else None

    def build_search_urls(self, criteria: dict) -> list[str]:
        """Une URL PAR TYPE demandé, toutes les localisations FUSIONNÉES dans
        un seul bloc g trié ascendant (une URL PAP ne porte qu'un SEUL bloc —
        voir la docstring du module) : la liste reflète EXACTEMENT ce que
        scrape() parcourt, page 1 de chaque série."""
        locations = get_locations(criteria)
        if not locations:
            return []

        return [
            self._base_url(criteria, fused_locations, geo_ids, segment)
            for fused_locations, geo_ids, segment in self._series(criteria, locations)
        ]

    # ------------------------------------------------------------------
    # Scraping
    # ------------------------------------------------------------------

    def scrape(self, criteria: dict) -> list[Listing]:
        locations = get_locations(criteria)
        if not locations:
            raise ValueError(
                "PAP nécessite au moins une localisation (ville + code postal) dans les critères"
            )

        series = self._series(criteria, locations)
        if not series:
            raise ValueError(
                "Aucune localisation PAP exploitable : impossible de résoudre "
                "les identifiants de lieu (voir services.pap_geocode)"
            )

        session = curl_requests.Session(impersonate=IMPERSONATE)
        session.headers.update({
            "Accept": "text/html, application/xhtml+xml",
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Referer": f"{BASE_URL}/",
        })

        seen: set[str] = set()
        listings: list[Listing] = []
        errors: list[str] = []

        for serie_locations, geo_ids, segment in series:
            block = f"g{_geo_block(geo_ids)}"
            described = ", ".join(_describe(location) for location in serie_locations)
            try:
                listings.extend(
                    self._scrape_series(session, criteria, serie_locations, geo_ids, segment, seen)
                )
            except Exception as e:
                errors.append(f"{described} ({block}): {e}")
                logger.warning(f"[PAP] {described} ({block}): {e}")

        if errors and len(errors) == len(series):
            raise ValueError("; ".join(errors))

        logger.info(f"[PAP] Scraping terminé : {len(listings)} annonces uniques")
        return listings

    def _scrape_series(self, session, criteria: dict, locations: list[dict],
                       geo_ids: list[str], segment: str, seen: set) -> list[Listing]:
        base_url = self._base_url(criteria, locations, geo_ids, segment)

        def fetch(page: int) -> str:
            if page > 1:
                time.sleep(_PAGE_DELAY_SECONDS)
            url = base_url if page == 1 else f"{base_url}-{page}"
            return _fetch_with_retries(session, url)

        # La page fusionnée sert TOUS les périmètres du bloc g : le filtrage
        # reçoit l'union complète — chaque annonce est rattachée au sien.
        return self._collect_pages(fetch, criteria, locations, seen,
                                   f"{segment}-g{_geo_block(geo_ids)}")

    def _collect_pages(self, fetch, criteria: dict, locations: list[dict],
                       seen: set, label: str) -> list[Listing]:
        """Parcourt les pages d'une série jusqu'à épuisement, en collectant
        les annonces qui passent les filtres.

        La condition d'arrêt est « cette page n'apporte plus aucune annonce
        inédite DANS LE PÉRIMÈTRE », et non le nombre total affiché par le
        site. Deux raisons :

        - elle absorbe le chevauchement entre pages ET le recyclage par le
          site de sa profondeur maximale (25 pages, voir la docstring du
          module : la page 26 redirige vers du déjà-vu) ;
        - à partir des premières pages, PAP intercale des annonces « proches »
          HORS périmètre (d'abord marquées « à Xkm », puis non marquées —
          vérifié en direct) et finit par n'alimenter plus qu'avec elles :
          ne compter que du inédit couvert par les localisations fait
          terminer le parcours dès la fin des vrais résultats au lieu
          d'errer jusqu'au plafond sur du bruit. Les cartes sans code postal
          lisible sont exclues du décompte (elles sont de toute façon
          écartées en aval, échec fermé).

        MAX_PAGES borne le parcours (même raison que Laforet et Century21 ;
        PAP pagine par ~15 cartes, la limite est donc relevée à 60)."""
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

                if not _location_ok(card["zip_code"], locations):
                    continue
                nouvelles += 1

                if uid in seen:
                    continue
                listing = _dict_to_listing(card)
                if _passes_filters(listing, criteria, locations):
                    seen.add(uid)
                    listings.append(listing)

            if not nouvelles:
                if pages_lues >= _DEPTH_CAP_SUSPECT:
                    logger.warning(
                        f"[PAP] {label} : pagination interrompue à la page "
                        f"{pages_lues} (plafond de profondeur du site probable) — "
                        f"{len(listings)} annonces retenues, résultat possiblement partiel"
                    )
                break
        else:
            logger.warning(
                f"[PAP] {label} : limite de {MAX_PAGES} pages atteinte "
                f"({len(listings)} annonces retenues, résultat possiblement partiel)"
            )

        logger.debug(f"[PAP] {label} : {pages_lues} page(s) lue(s), {len(listings)} annonces retenues")
        return listings

    def has_valid_criteria(self, criteria: dict) -> bool:
        """Utilisable dès qu'il y a un identifiant manuel, ou au moins un
        périmètre dont on saura tirer un identifiant (tous les niveaux
        canoniques ont un identifiant natif PAP — voir services.pap_geocode)."""
        if source_overrides(criteria, self.SOURCE_ID).get("geoIds"):
            return True

        from services.pap_geocode import area_cache_key

        return any(
            loc.get("kind") in (CITY, WHOLE_CITY, DEPARTMENT, REGION) and area_cache_key(loc)
            for loc in get_locations(criteria)
        )
