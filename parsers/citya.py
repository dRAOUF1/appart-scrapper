"""Citya Immobilier (citya.com) listing scraper — HTML server-rendered paginé.

citya.com est un Symfony/Twig server-rendered (cartes `div.property-card`,
vérifié en direct le 23/08/2026) sans API JSON publique exploitable : l'API
interne `/api/*` répond 401 hors provenance interne. Le scrape passe donc par
la pagination HTML du site :

    GET https://www.citya.com/annonces/location/{type}/{slugs}?page=N&filtres
    -> cartes `div.property-card[data-itemid][data-itemname][data-category]
       [data-price]`, ~24 par page, page au-delà de la fin répondant
       gracieusement 200 avec zéro carte.

Contrats vérifiés contre le vivant :

- UN SEUL type de bien par requête : il n'existe AUCUN moyen de combiner deux
  types (`appartement-maison` replie sur tous, `appartement/maison` répond
  404, aucun paramètre GET de type) -> le scrape boucle sur les types,
  dédupliqué par itemId ;
- PLUSIEURS localisations se composent par VIRGULES dans le même segment de
  chemin (`/appartement/toulouse-31555,bordeaux-33063`, titre « ... ou ... »)
  — c'est le mécanisme officiel du site : son frontend remplit
  `realLocalisation` avec les slugs sélectionnés joints par « , »
  (search_property.js : `e.map(e => e.slug).join(",")`). L'union est EXACTE
  pour les villes normales (13+33=46 vérifié), les départements et les
  régions, inclusions gérées (toulouse+haute-garonne = haute-garonne seul ;
  ville+région = région seule). EXCEPTION CRITIQUE : les entrées « toute la
  ville » des communes à arrondissements FAUSSENT la composition — Paris et
  Marseille y contribuent ZÉRO annonce, Lyon y contribue un ensemble sans
  AUCUN itemId commun avec sa recherche seule (19 vs 42, zéro chevauchement ;
  la seule présence de paris-75 dans la liste suffit à casser lyon-69000,
  alors que ile-de-france-11+lyon-69000 compose exactement). Ces agrégats
  partent donc TOUJOURS en requête séparée ; seuls les slugs issus de
  city/department/region sont composés entre eux. Et un solo dont le
  périmètre postal est couvert par les autres localisations est purement et
  simplement économisé (paris-75 ⊂ ile-de-france-11 : ses 13 biens, itemIds
  identiques, sont tous dans la recherche régionale) ;
- filtres natifs courts vérifiés en direct et combinables : `prixMax`,
  `surfaceMin`, `nbrePiecesMin` (+`page`). Les bornes inverses (prixMin,
  surfaceMax, pièces max) n'existent pas côté site -> recadrage local par
  _passes_filters ;
- le site ÉLARGIT au secteur quand l'inventaire est trop mince (parking à
  Quiberon -> titres scopés mais biens à Rennes/Brest/Saint-Brieuc, vérifié
  en direct) : matches_locations est OBLIGATOIRE en aval, sans quoi un
  périmètre étroit ramènerait silencieusement des annonces voisines ;
- un slug mal formé ne renvoie PAS d'erreur mais la FRANCE ENTIÈRE en 200
  (`toulouse-31000` au code postal, `foo-bar-31555` au nom inventé) : on ne
  construit jamais un slug soi-même, uniquement ceux résolus par
  services.citya_geocode (autocomplete du site + cache en base) ou saisis
  en surcharge manuelle.

Limites v1 : LOCATION SEULEMENT (la vente existe côté site,
/annonces/vente/..., même structure de cartes, mais n'est pas câblée —
SUPPORTED_TRANSACTIONS le déclare, le front prévient avant le scrape).
Pas de description ni de DPE sur les cartes : ces champs restent vides.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup
from loguru import logger

from core.criteria import (
    APARTMENT,
    HOUSE,
    LAND,
    PARKING,
    RENT,
    location_postal_prefixes,
    matches_locations,
    source_overrides,
)
from core.geocode import CITY, DEPARTMENT, REGION
from models.listing import Listing
from parsers._dates import DATE_INCONNUE
from parsers.base import BaseParser, ParserRegistry, get_locations

BASE_URL = "https://www.citya.com"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Vocabulaire natif du site (clés = vocabulaire canonique core.criteria).
# Types validés en direct par les chemins /annonces/location/{type}/... ;
# immeuble / immobilier-professionnel / studio existent côté site mais sont
# hors vocabulaire canonique.
TRANSACTION_VALUES = {RENT: "location"}
TYPE_VALUES = {
    APARTMENT: "appartement",
    HOUSE: "maison",
    PARKING: "parking",
    LAND: "terrain",
}

# Cartes par page observées en direct (24, stable sur plusieurs recherches).
PAGE_SIZE = 24
# Plafond de la boucle paginée (30 x 24 = 720 annonces par couple
# localisation x type — très au-dessus des inventaires observés, même sur
# une région entière : Occitanie location ~534 annonces = 23 pages). Au-delà,
# troncature loggée, jamais masquée.
MAX_PAGES = 30


def _as_float(value) -> float | None:
    """Un montant numérique tolérant au format (« 1 453 » -> 1453.5 gère la
    décimale), None sur ce qui n'est pas convertible."""
    if value is None:
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _format_price(value: float | None) -> str:
    """Le prix affiché à la française (« 2453 » -> « 2 453 € »)."""
    if value is None:
        return ""
    return f"{int(value):,}".replace(",", " ") + " €"


def _transaction(criteria: dict) -> str:
    """La transaction canonique demandée. La location par défaut : c'est ce
    que le formulaire propose en premier, et une recherche sans transaction
    explicite n'a jamais voulu dire « achat »."""
    transaction = criteria.get("transaction")
    return transaction if transaction in TRANSACTION_VALUES else RENT


def _property_types(criteria: dict) -> list[str]:
    """Les types de bien canoniques que Citya sait traiter parmi ceux
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


def _allowed_rooms(canonical_values) -> tuple[set[int], bool]:
    """La demande de pièces canonique -> (valeurs exactes, « et plus »).

    Le canonique est une liste d'égalités où toute valeur >= 5 vaut « et
    plus ». Le site n'a qu'un filtre natif nbrePiecesMin (pas de maximum) :
    ce décodage sert AUSSI bien au filtre natif qu'au recadrage local
    exact."""
    values: set[int] = set()
    plus_open = False
    for raw in canonical_values or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        if value >= 5:
            plus_open = True
        else:
            values.add(value)
    return values, plus_open


def _rooms_min(canonical_values) -> int | None:
    """La borne nbrePiecesMin native : la plus petite valeur demandée, 5 si
    seule la borne ouverte « et plus » est demandée, None si rien."""
    values, plus_open = _allowed_rooms(canonical_values)
    if values:
        return min(values)
    return 5 if plus_open else None


def _native_filters(criteria: dict) -> list[tuple[str, str]]:
    """Les filtres EXACTS envoyés à chaque requête, dans le vocabulaire court
    du site (vérifiés en direct, combinables) : prixMax, surfaceMin,
    nbrePiecesMin. Les bornes inverses n'existent pas côté site — c'est
    _passes_filters qui recadre localement."""
    params: list[tuple[str, str]] = []
    price_max = criteria.get("priceMax")
    if price_max:
        params.append(("prixMax", str(price_max)))
    surface_min = criteria.get("surfaceMin")
    if surface_min:
        params.append(("surfaceMin", str(surface_min)))
    rooms_min = _rooms_min(criteria.get("rooms"))
    if rooms_min is not None and rooms_min >= 2:
        # nbrePiecesMin=1 ne filtrerait rien : le site ne référence pas de 0 pièce.
        params.append(("nbrePiecesMin", str(rooms_min)))
    # Le formulaire public envoie exactement ces deux champs pour
    # « Date (le plus récent) » (vérifié en live le 2026-08-29). Le cap
    # de pagination conserve ainsi la tête la plus fraîche du stock.
    params.extend((("sort", "b.dateCreation"), ("direction", "desc")))
    return params


def _search_url(slugs_target: str, type_native: str, criteria: dict, page: int = 1) -> str:
    """L'URL de recherche réellement interrogée pour une cible de localisations
    (un slug seul ou plusieurs joints par virgules — le format natif du site)
    et un type : miroir exact de scrape(), réutilisée par build_search_urls."""
    params = list(_native_filters(criteria))
    if page > 1:
        params.insert(0, ("page", str(page)))
    query = f"?{urlencode(params)}" if params else ""
    return f"{BASE_URL}/annonces/location/{type_native}/{slugs_target}{query}"


_PLACE_RE = re.compile(r"([A-Za-z0-9À-ÿ'’\- ]+?)\s*\((\d{5})\)")
_ROOMS_RE = re.compile(r"(\d+)\s*pi[èe]ces?\b", re.IGNORECASE)
_SURFACE_RE = re.compile(r"([\d]+(?:[.,]\d+)?)\s*m²", re.IGNORECASE)


def _parse_place(card_text: str) -> tuple[str, str]:
    """« Ville (code postal) » depuis le texte d'une carte.

    Le motif parenthèsé est distinctif (les prix ne sont jamais entre
    parenthèses, les surfaces en m²) ; en cas de multiples occurrences, la
    DERNIÈRE gagne — un titre peut contenir un nombre à cinq chiffres, le
    bloc ville vient après lui."""
    matches = _PLACE_RE.findall(card_text)
    if not matches:
        return "", ""
    name, zip_code = matches[-1]
    return name.strip(), zip_code


def _parse_card(card) -> Listing | None:
    """Une carte Citya (`div.property-card`) en Listing.

    Structure vérifiée en direct :
        div.property-card[data-itemid="GES12040005-198"]
                        [data-itemname="Appartement 1 pièce 18m²"]
                        [data-category="Appartement"][data-price="486"]
        > a[href="https://www.citya.com/annonces/location/appartement/
              bordeaux-33063/GES12040005-198"]
        > img[src="/media/images/agences/biens/.../....webp"]
        texte : « 486 € », « Bordeaux (33000) », « Meublé »

    L'id d'annonce (attribut data-itemId, préfixe variable selon l'agence :
    GES/LAPP/TAPP...) devient legacy_id et préfixe le listing_id ; le prix
    machine vient de data-price, pas du libellé affiché. BeautifulSoup
    normalisant les noms d'attributs HTML en minuscules, les attributs
    camelCase du site se lisent en bas de casse (data-itemname). Pas de
    description ni de DPE sur les cartes : ces champs restent vides."""
    item_id = card.get("data-itemid")
    link = card.select_one("a[href]")
    if not item_id or link is None:
        return None

    href = link.get("href") or ""
    url = urljoin(BASE_URL, href)

    card_text = card.get_text(" ", strip=True)
    city, zip_code = _parse_place(card_text)

    # BeautifulSoup normalise les noms d'attributs HTML en minuscules :
    # les attributs camelCase du site se lisent ici en bas de casse.
    title = (card.get("data-itemname") or "").strip()
    price_value = _as_float(card.get("data-price"))

    rooms_match = _ROOMS_RE.search(title)
    surface_match = _SURFACE_RE.search(title)

    photos = [
        {"url": urljoin(BASE_URL, img.get("src")), "alt": "", "key": ""}
        for img in card.select("img[src]")
        if "/media/images/" in (img.get("src") or "")
    ]

    return Listing(
        listing_id=f"citya_{item_id}",
        url=url,
        title=title,
        price=_format_price(price_value),
        surface=surface_match.group(1).replace(",", ".") if surface_match else "",
        rooms=rooms_match.group(1) if rooms_match else "",
        location=f"{city} ({zip_code})" if zip_code else city,
        image_url=photos[0]["url"] if photos else "",
        description="",
        agency="",
        source="citya",
        legacy_id=str(item_id),
        price_value=price_value,
        city=city,
        zip_code=zip_code,
        property_type=(card.get("data-category") or "").strip(),
        is_private=False,
        headline="Meublé" if "meublé" in card_text.lower() else "",
        photos=json.dumps(photos),
        # Issue #12 : les property-card Citya (data-itemid, data-itemname,
        # data-category) ne portent aucune date de publication — les cartes
        # sont volontairement pauvres (pas de description ni de DPE non plus).
        # Sentinelle explicite plutôt que chaîne vide.
        creation_date=DATE_INCONNUE,
    )


def _passes_filters(listing: Listing, criteria: dict, locations: list[dict]) -> bool:
    """Recadre localement ce que le site a renvoyé : localisation d'abord —
    le site élargit au secteur quand l'inventaire est trop mince (vérifié en
    direct : parking Quiberon -> biens à Rennes/Brest/Saint-Brieuc) — puis
    bornes prix/surface/pièces, y compris celles sans équivalent natif
    (prixMin, surfaceMax, maximum de pièces)."""
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

    allowed_values, plus_open = _allowed_rooms(criteria.get("rooms"))
    if allowed_values or plus_open:
        try:
            room_count = int(float(listing.rooms))
        except (TypeError, ValueError):
            return True  # pièce illisible : on garde, jamais d'exclusion sournoise
        if room_count > 0 \
                and room_count not in allowed_values \
                and not (plus_open and room_count >= 5):
            return False
        # pièce absente (0) : seul le contrôle localisation/prix/surface s'applique

    return True


def _covered_by(prefixes: list[str], other_prefixes: list[list[str]]) -> bool:
    """Tous les préfixes postaux d'une localisation sont-ils absorbés par ceux
    des autres ? Un préfixe p est couvert dès qu'une autre localisation
    porte un préfixe q dont p dépend (p.startswith(q)) — les préfixes sont
    hiérarchiques : « 75 » (département) couvre « 75001 » (code postal)."""
    flat = [q for qs in other_prefixes for q in qs]
    return bool(prefixes) and all(any(p.startswith(q) for q in flat) for p in prefixes)


@ParserRegistry.register
class CityaParser(BaseParser):
    """Scrape citya.com : HTML paginé, slugs de recherche résolus par son
    autocomplete (cache persistant), un couple localisation x type par
    requête."""

    SOURCE_ID = "citya"
    SOURCE_NAME = "Citya"
    SOURCE_DESCRIPTION = (
        "citya.com — HTML paginé (location), slugs résolus par l'autocomplete "
        "du site, villes/départements/régions métropolitaines"
    )

    # Location seulement dans cette première version : la vente existe côté
    # site (/annonces/vente/..., mêmes cartes) mais n'est pas câblée.
    # SUPPORTED_TRANSACTIONS le déclare — le front prévient AVANT le scrape
    # (unsupported_criteria).
    SUPPORTED_TRANSACTIONS = (RENT,)

    MANUAL_OVERRIDE_LABEL = "slug(s) de recherche Citya (optionnel)"
    MANUAL_OVERRIDE_HELP = (
        "Repli si la résolution automatique échoue : le ou les slugs de "
        "recherche Citya, séparés par des virgules (ex. « toulouse-31555, "
        "haute-garonne-31 »). Visibles dans l'URL d'une recherche faite sur "
        "citya.com (/annonces/location/<type>/<slug>)."
    )

    URL_NOTE = (
        "Le site accepte plusieurs localisations dans une seule URL (slugs "
        "joints par des virgules, union exacte) mais un seul type de bien par "
        "requête : le scraper produit donc une URL composée par type. Les "
        "villes entières à arrondissements (Paris, Lyon, Marseille) faussant "
        "cette union côté site, elles partent chacune dans leur propre URL — "
        "sauf quand leur périmètre est déjà couvert par une autre "
        "localisation de la recherche (ex. Paris ⊂ Île-de-France), auquel cas "
        "le lien redondant est omis. Le site peut en outre élargir au secteur "
        "quand l'inventaire est mince : des annonces hors périmètre visibles "
        "en ouvrant ces liens sont écartées par le contrôle local du scraper."
    )

    def _geo_repo(self):
        """Le cache persistant slug <- périmètre, ou None s'il n'y a pas de
        storage — jamais lu depuis `flask.current_app` : le scraping
        s'exécute sur un thread de fond, hors contexte d'application."""
        return getattr(self.storage, "citya_geo", None) if self.storage else None

    def _resolved_locations(
        self, criteria: dict, locations: list[dict]
    ) -> list[tuple[dict, str]]:
        """Les couples (localisation, slug) à interroger : les slugs collés à
        la main s'il y en a, sinon ceux résolus par services.citya_geocode.

        Deux périmètres résolus vers le même slug (ville + ville entière de
        la même commune) ne sont interrogés qu'une fois — la première
        localisation fait foi pour le contrôle aval. Un périmètre non résolu
        est écarté avec un avertissement clair, jamais muté (les critères
        sont partagés entre sources pendant un scrape)."""
        manual = source_overrides(criteria, self.SOURCE_ID).get("slugs")
        if manual:
            # L'utilisateur peut coller plus ou moins de slugs que de villes :
            # zip strict=False épouse ce qu'il y a, dans l'ordre.
            return list(zip(locations, manual, strict=False))

        repo = self._geo_repo()
        if repo is None:
            logger.warning(
                "[Citya] Aucun storage fourni au parser, résolution du slug impossible "
                "(voir get_parser(source, storage=...))"
            )
            return []

        from services import citya_geocode

        resolved: list[tuple[dict, str]] = []
        seen_slugs: set[str] = set()
        for location in locations:
            slug = citya_geocode.resolve_slug_id(location, repo=repo)
            if not slug:
                logger.warning(
                    f"[Citya] Aucun slug résolu pour {citya_geocode._describe(location)}"
                )
                continue
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            resolved.append((location, slug))
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

        from services import citya_geocode

        try:
            citya_geocode.remember_manual_slugs(criteria, repo=repo)
        except Exception as e:
            logger.debug(f"[Citya] Slug manuel non mémorisé : {e}")

    # Les niveaux dont le slug se compose sans risque avec les autres :
    # l'union virgule du site est EXACTE pour les villes normales, les
    # départements et les régions, inclusions comprises (toulouse-31555 +
    # haute-garonne-31 = haute-garonne seul, ville + région = région seule,
    # vérifié en direct). Seuls les agrégats « toute la ville » des communes
    # à arrondissements la faussent — voir _query_targets.
    _COMPOSABLE_KINDS = frozenset({CITY, DEPARTMENT, REGION})
    def _query_targets(self, resolved: list[tuple[dict, str]]) -> list[str]:
        """Les cibles de chemins à interroger : les slugs composables joints
        par des virgules en UNE requête — le mécanisme officiel du site, dont
        le frontend remplit `realLocalisation` avec
        `slugs.join(",")` (search_property.js) — plus une requête séparée par
        agrégat « toute la ville » de commune à arrondissements.

        Deux économies, sans jamais perdre une annonce :

        - un solo dont le slug figurerait déjà dans le groupe est sauté (une
          même commune résolue deux fois ne doit pas être interrogée deux
          fois) ;
        - un solo dont le périmètre postal est ENTIÈREMENT couvert par les
          autres localisations est sauté : sa requête ne ramènerait que des
          annonces déjà obtenues (vérifié en direct : les 13 biens de
          paris-75 sont tous présents dans la recherche ile-de-france-11,
          itemIds identiques). Couverture gloutonne dans l'ordre des
          localisations, pour ne jamais supprimer deux solos qui ne
          se couvriraient que mutuellement.

        Pourquoi cette ségrégation composable/solo : en composition,
        `paris-75`/`paris-75000` et `marseille-13000` contribuent ZÉRO
        annonce et `lyon-69000` un ensemble sans AUCUN itemId commun avec sa
        recherche seule (19 biens sur 42, zéro chevauchement — vérifié en
        direct le 23/08/2026 ; la présence de paris-75 dans la liste suffit à
        casser lyon-69000). Ces agrégats ne voyagent donc jamais dans un
        groupe."""
        composable = [
            (loc, slug) for loc, slug in resolved
            if loc.get("kind", CITY) in self._COMPOSABLE_KINDS
        ]
        group = [slug for _, slug in composable]
        group_set = set(group)
        group_prefixes = [location_postal_prefixes(loc) for loc, _ in composable]

        targets = [",".join(group)] if group else []
        kept_prefixes = list(group_prefixes)
        for loc, slug in resolved:
            if loc.get("kind", CITY) in self._COMPOSABLE_KINDS or slug in group_set:
                continue
            prefixes = location_postal_prefixes(loc)
            if _covered_by(prefixes, kept_prefixes):
                logger.info(
                    f"[Citya] {slug} solo redondant (périmètre déjà couvert par "
                    "les autres localisations), requête économisée"
                )
                continue
            kept_prefixes.append(prefixes)
            targets.append(slug)
        return targets

    def to_native(self, criteria: dict) -> dict:
        """Rien à traduire : le vocabulaire natif de Citya se limite à des
        chemins d'URL (transaction/type/slugs) et à trois filtres GET courts,
        construits à la volée par _search_url/_native_filters depuis les
        critères tels quels. La résolution des slugs passe par
        services.citya_geocode."""
        return criteria

    def has_valid_criteria(self, criteria: dict) -> bool:
        """Utilisable dès qu'il y a un slug manuel, ou au moins un périmètre
        identifiable : tout niveau canonique porte ce qu'il faut à son
        résolveur (commune : INSEE/code postal garanti par
        normalize_locations, département/région : leur code)."""
        if source_overrides(criteria, self.SOURCE_ID).get("slugs"):
            return True
        return bool(get_locations(criteria))

    def build_search_url(self, criteria: dict) -> str | None:
        urls = self.build_search_urls(criteria)
        return urls[0] if urls else None

    def build_search_urls(self, criteria: dict) -> list[str]:
        """Les URLs humaines réellement équivalentes au scrape : UNE par cible
        de localisations (le groupe composé par virgules, plus une par
        agrégat « toute la ville », voir _query_targets) et par type demandé,
        mêmes filtres natifs, page 1 — le site ignorant toute combinaison de
        types, c'est le seul lien fidèle."""
        locations = get_locations(criteria)
        if not locations:
            return []
        types = _property_types(criteria)
        if not types:
            return []
        resolved = self._resolved_locations(criteria, locations)
        return [
            _search_url(target, TYPE_VALUES[t], criteria)
            for target in self._query_targets(resolved)
            for t in types
        ]

    def _fetch_page(self, session: requests.Session, url: str) -> BeautifulSoup:
        """Une page de résultats parsée ; toute erreur HTTP réelle est un
        échec levé (ValueError), jamais aplatie en résultat vide — le
        pipeline doit pouvoir distinguer un échec d'une recherche légitimement
        sans résultat."""
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise ValueError(f"Requête Citya échouée ({url}) : {e}") from e
        return BeautifulSoup(resp.text, "lxml")

    def scrape(self, criteria: dict) -> list[Listing]:
        # Une transaction explicite non supportée est refusée AVANT toute
        # autre chose : la convertir en location serait un faux résultat
        # (le défaut RENT ne s'applique qu'à une recherche sans préférence).
        requested_transaction = criteria.get("transaction")
        if requested_transaction and requested_transaction not in self.SUPPORTED_TRANSACTIONS:
            raise ValueError(
                f"Citya ne référence que la location, pas la transaction "
                f"« {requested_transaction} »"
            )
        locations = get_locations(criteria)
        if not locations:
            raise ValueError(
                "Citya nécessite au moins une localisation (ville + code postal) dans les critères"
            )
        types = _property_types(criteria)
        if not types:
            raise ValueError(
                "Citya ne référence aucun des types de bien demandés "
                f"(uniquement {sorted(TYPE_VALUES)})"
            )

        resolved = self._resolved_locations(criteria, locations)
        if not resolved:
            raise ValueError(
                "Aucune localisation Citya exploitable : aucun périmètre n'a pu "
                "être résolu en slug de recherche (voir les logs, ou utiliser la "
                "surcharge manuelle de slugs)"
            )
        scoped_locations = [loc for loc, _ in resolved]
        targets = self._query_targets(resolved)

        session = requests.Session()
        session.headers.update({
            "User-Agent": DESKTOP_UA,
            "Accept": "text/html,application/xhtml+xml",
        })

        seen: set[str] = set()
        listings: list[Listing] = []
        for target in targets:
            for type_canonical in types:
                type_native = TYPE_VALUES[type_canonical]
                fetched_for_query = 0
                for page in range(1, MAX_PAGES + 1):
                    soup = self._fetch_page(session, _search_url(target, type_native, criteria, page))
                    cards = soup.select("div.property-card[data-itemid]")
                    if not cards:
                        text = soup.get_text(" ", strip=True)
                        total_match = re.search(r"\b(\d+)\s+résultats?\b", text, re.IGNORECASE)
                        declared_total = int(total_match.group(1)) if total_match else None
                        if page == 1 and declared_total != 0:
                            raise ValueError(
                                f"Citya {target}/{type_native} : HTTP 200 sans carte ni zéro résultat explicite "
                                "(page inattendue ou protection anti-bot)"
                            )
                        break
                    fetched_for_query += len(cards)
                    for card in cards:
                        listing = _parse_card(card)
                        if listing is None or listing.listing_id in seen:
                            continue
                        seen.add(listing.listing_id)
                        if _passes_filters(listing, criteria, scoped_locations):
                            listings.append(listing)
                logger.debug(
                    f"[Citya] {target}/{type_native} : {fetched_for_query} cartes parcourues, "
                    f"{len(listings)} retenues au total"
                )
                if fetched_for_query and page == MAX_PAGES and cards:
                    logger.warning(
                        f"[Citya] {target}/{type_native} : limite de {MAX_PAGES} pages atteinte ; "
                        "les annonces collectées restent les plus récentes grâce au tri date décroissant"
                    )

        logger.info(f"[Citya] Scraping terminé : {len(listings)} annonces uniques")
        return listings
