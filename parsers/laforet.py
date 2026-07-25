"""Laforet.com listing scraper.

Server-rendered (Symfony/Turbo + UX Live Component) site, no public JSON
listing API. Location is encoded in the URL path as a human-readable city
slug + postal code, e.g.:

    https://www.laforet.com/ville/location-appartement-paris-75018

That page always renders two sections: the real, correctly-scoped results,
followed unconditionally by a second "Appartements à proximité de {ville}"
section backfilled with listings from neighboring communes/arrondissements.
Both use the same card markup, so anything that parses the whole page
indiscriminately picks up that noise — see _extract_genuine_section().

Multiple locations in one search are merged server-side via
`filter[cities][]=<INSEE code>` query params (verified live against the
site's own "add a city" filter UI, network-captured with Playwright) — but
only within the *first*, genuine section; the noise section is unaffected
by the filter and must still be excluded the same way. `filter[cities][]`
takes INSEE-style commune codes, not postal codes, and Paris/Lyon/Marseille
need their arrondissement-specific code (not the whole-city INSEE code) —
c'est core.geocode qui s'en charge, partagé avec les autres sources.

Plusieurs types de bien tiennent aussi dans une seule requête :
filter[types][] est répétable et prime sur le type inscrit dans le slug du
chemin (vérifié en live — une page "achat-appartement-bordeaux-33000" avec
filter[types][]=apartment&filter[types][]=house rend bien 17 appartements
et 18 maisons). Le type d'une annonce est donc lu dans son URL, pas déduit
de ce qui a été demandé — voir _property_type_from_url().

filter[min]/filter[max]/filter[surface] (price/surface) were also verified
live: they have a real but imprecise effect on the genuine section (one
example let a listing above the requested max through), so price/surface/
rooms filtering is still fully enforced client-side in _passes_filters(),
same as before — only the *location* merging is trusted to the server now.
"""

from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from loguru import logger

from core.criteria import APARTMENT, BUY, HOUSE, RENT
from core.geocode import resolve_insee_code as _resolve_insee_code
from models.listing import Listing
from parsers.base import BaseParser, ParserRegistry, get_locations

BASE_URL = "https://www.laforet.com"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

MAX_PAGES = 30

# Path uses French slugs; the filter[...] query params use English values —
# verified live these are genuinely two different vocabularies on this site.
# Les clés sont le vocabulaire canonique (core.criteria).
TRANSACTION_SLUGS = {RENT: "location", BUY: "achat"}
TRANSACTION_FILTER_VALUES = {RENT: "rent", BUY: "buy"}
TYPE_SLUGS = {APARTMENT: "appartement", HOUSE: "maison"}
TYPE_FILTER_VALUES = {APARTMENT: "apartment", HOUSE: "house"}
# Le libellé affiché dans le titre des annonces trouvées.
TYPE_DISPLAY_NAMES = {APARTMENT: "Appartement", HOUSE: "Maison"}

# Marks where the real results end and the always-appended "nearby" backfill
# section begins — see module docstring. Verified this text is present even
# on pages with plenty of native inventory (Poitiers) where it has no effect
# (the "nearby" section is simply empty there), so truncating here is safe
# in every case, not just the sparse-inventory one.
_NEARBY_SECTION_MARKER = "proximité de"

# Must match the full listing-detail path shape, not just "ends in -<digits>".
# When a city has thin inventory Laforet backfills the results page with
# "nearby agency office" cards (e.g. an <a href="/agence-immobiliere/lyon-7">
# linking to the office itself, not a listing). "lyon-7" alone also ends in
# "-<digit>", so a looser pattern misidentifies these office cards as real
# listings (verified live: this returned a fake "listing" whose url was just
# the agency's own page, with no price/surface/rooms/location at all).
_DETAIL_LINK_RE = re.compile(
    r"/agence-immobiliere/[^/]+/(?:louer|acheter)/[^/]+/(?:appartement|maison)-[^/]+-(\d+)$"
)
_PRICE_RE = re.compile(r"([\d\s ]+)\s*€")
_CITY_ZIP_RE = re.compile(r"([A-ZÀ-Ü][A-Za-zÀ-ÿ' \-]*?)\s*\((\d{5})\)")
_SURFACE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m²")
_ROOMS_RE = re.compile(r"(\d+)\s*pi[eè]ce")

def _slugify(text: str) -> str:
    """Lowercase, strip accents, non-alnum -> '-' (e.g. 'Le Kremlin-Bicêtre' -> 'le-kremlin-bicetre')."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug


def _transaction(criteria: dict) -> str:
    """La transaction canonique demandée. La location par défaut : c'est ce
    que le formulaire propose en premier, et une recherche sans transaction
    explicite n'a jamais voulu dire "achat"."""
    transaction = criteria.get("transaction")
    return transaction if transaction in TRANSACTION_SLUGS else RENT


def _property_types(criteria: dict) -> list[str]:
    """Les types de bien canoniques que Laforet sait traiter parmi ceux
    demandés.

    - Aucun type demandé -> appartement, le défaut du formulaire.
    - Types demandés dont certains sont hors capacités (parking, terrain) ->
      on garde les autres sans lever d'exception : une recherche mixte
      « appartement + parking » doit tout de même ramener les appartements.
    - Uniquement des types hors capacités -> liste vide, et surtout PAS le
      défaut appartement : renvoyer des appartements à qui demande un parking
      serait un faux résultat. Les appelants traitent ce cas comme
      « rien à chercher » (voir build_search_urls et scrape).
    """
    requested = criteria.get("propertyTypes") or []
    if not requested:
        return [APARTMENT]
    return [t for t in requested if t in TYPE_SLUGS]


def _location_insee_code(location: dict) -> str | None:
    """Le code INSEE d'une localisation canonique.

    Celui fourni par l'autocomplete est utilisé tel quel (aucun appel
    réseau) ; sinon il est résolu depuis le code postal via core.geocode —
    le point de vérité partagé entre toutes les sources.
    """
    return location.get("inseeCode") or _resolve_insee_code(location["postalCode"])


def _extract_genuine_section(html: str) -> str:
    """Strip the always-appended "nearby" backfill section (see module
    docstring) before anything parses cards or pagination out of the page."""
    idx = html.find(_NEARBY_SECTION_MARKER)
    return html[:idx] if idx != -1 else html


def _parse_price(text: str) -> float | None:
    m = _PRICE_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\s", "", m.group(1))
    try:
        return float(digits)
    except ValueError:
        return None


def _parse_total_pages(html: str) -> int:
    """Read the total page count from the ItemList JSON-LD block."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except json.JSONDecodeError:
            continue
        blocks = data if isinstance(data, list) else [data]
        for block in blocks:
            if block.get("@type") == "ItemList":
                positions = [
                    item.get("position", 1)
                    for item in block.get("itemListElement", [])
                ]
                if positions:
                    return max(positions)
    return 1


def _parse_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []
    for article in soup.find_all("article"):
        link = None
        for a in article.find_all("a", href=True):
            if "/agence-immobiliere/" in a["href"]:
                link = a["href"]
                break
        if not link:
            continue
        m = _DETAIL_LINK_RE.search(link)
        if not m:
            continue
        reference = m.group(1)

        text = article.get_text(" ", strip=True)
        price_value = _parse_price(text)
        city_zip = _CITY_ZIP_RE.search(text)
        surface_m = _SURFACE_RE.search(text)
        rooms_m = _ROOMS_RE.search(text)

        results.append({
            "reference": reference,
            "url": link,
            "price_value": price_value,
            "city": city_zip.group(1).strip() if city_zip else "",
            "zip_code": city_zip.group(2) if city_zip else "",
            "surface": surface_m.group(1) if surface_m else "",
            "rooms": rooms_m.group(1) if rooms_m else "",
            "agency": link.split("/agence-immobiliere/", 1)[1].split("/", 1)[0],
        })
    return results


def _passes_filters(listing: Listing, criteria: dict, allowed_postal_codes: set) -> bool:
    """Enforce location + price/surface/rooms filters ourselves.

    `allowed_postal_codes` is the exact set of postal codes this search
    asked for — a listing whose own postal code we couldn't parse, or that
    isn't in that set, is never assumed to match (fail closed, not open:
    unlike price/surface/rooms below, location correctness can't be waived
    just because a card was hard to parse — this is exactly how a
    nearby-agency-office filler card, with no zip_code at all, previously
    slipped through as a fake listing).
    """
    if listing.zip_code not in allowed_postal_codes:
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
        # "5" in the shared rooms vocabulary means "5+" (see search_edit.html).
        if not any(room_count == r or (r >= 5 and room_count >= 5) for r in allowed):
            return False

    return True


def _property_type_from_url(url: str) -> str:
    """Le type de bien lu dans l'URL de l'annonce elle-même.

    Une même requête peut mélanger les types (filter[types][] est répétable
    et prime sur le slug du chemin — vérifié en live : une page
    "achat-appartement-..." avec filter[types][]=apartment&...=house rend
    bien 17 appartements et 18 maisons). Le type ne peut donc pas être
    déduit de ce qui a été demandé, il doit être lu par annonce.
    """
    for canonical, slug in TYPE_SLUGS.items():
        if f"/{slug}-" in url:
            return TYPE_DISPLAY_NAMES[canonical]
    return ""


def _dict_to_listing(data: dict) -> Listing:
    price_value = data["price_value"]
    property_type = _property_type_from_url(data["url"])
    return Listing(
        listing_id=f"lf_{data['reference']}",
        url=data["url"],
        title=f"{property_type} {data['city']}".strip(),
        price=f"{int(price_value)} €" if price_value is not None else "",
        surface=data["surface"],
        rooms=data["rooms"],
        location=data["city"],
        city=data["city"],
        zip_code=data["zip_code"],
        agency=data["agency"],
        source="laforet",
        legacy_id=data["reference"],
        price_value=price_value,
        property_type=property_type,
    )


@ParserRegistry.register
class LaforetParser(BaseParser):
    """Scrape Laforet.com via server-rendered HTML (no anti-bot on this site)."""

    SOURCE_ID = "laforet"
    SOURCE_NAME = "Laforêt"
    SOURCE_DESCRIPTION = "Laforet.com — scraping HTML serveur (ville + code postal)"

    # Laforet ne référence que de l'habitation : ni parking, ni terrain
    # (son URL n'a pas de slug pour eux et filter[types][] ne les connaît
    # pas). Déclaré ici pour que ce soit dit à l'utilisateur avant le
    # scrape, plutôt que levé en pleine exécution.
    SUPPORTED_PROPERTY_TYPES = (APARTMENT, HOUSE)

    URL_NOTE = (
        "Ce lien ne montre que la localisation : les filtres prix/surface/pièces "
        "sont appliqués par le scraper mais volontairement absents de l'URL, "
        "car leur effet côté site n'est pas assez fiable pour s'y fier seul."
    )

    # has_valid_criteria / cannot_search_reason : pas de surcharge nécessaire,
    # ville + code postal (le contrat par défaut de BaseParser) suffisent —
    # le code INSEE dont filter[cities][] a besoin est résolu à partir du
    # code postal quand l'autocomplete ne l'a pas déjà fourni.

    def to_native(self, criteria: dict) -> dict:
        """Rien à traduire : Laforet construit ses URLs directement depuis le
        canonique (voir _search_paths). Les bornes prix/surface/pièces sont
        appliquées côté scraper par _passes_filters, qui lit lui aussi le
        canonique."""
        return criteria

    def _base_path(self, criteria: dict, location: dict) -> str:
        """Le chemin de la page de résultats pour cette localisation.

        Le type dans le slug est celui du premier type demandé, mais il n'a
        pas d'effet réel : filter[types][] prime sur lui (vérifié en live).
        """
        transaction = TRANSACTION_SLUGS[_transaction(criteria)]
        type_slug = TYPE_SLUGS[_property_types(criteria)[0]]

        return (
            f"{BASE_URL}/ville/{transaction}-{type_slug}-"
            f"{_slugify(location['city'])}-{location['postalCode']}"
        )

    def _type_filters(self, criteria: dict) -> list[tuple[str, str]]:
        """Un filter[types][] par type de bien demandé — le paramètre est
        répétable et c'est lui qui gouverne réellement le résultat."""
        return [
            ("filter[types][]", TYPE_FILTER_VALUES[t])
            for t in _property_types(criteria)
        ]

    def _split_locations(self, criteria: dict) -> tuple[list[tuple[dict, str]], list[dict]]:
        """Sépare les localisations selon qu'on a pu ou non leur trouver un
        code INSEE : les résolues partent dans une requête fusionnée unique
        (filter[cities][]), les autres dans une requête chacune plutôt que
        d'être silencieusement abandonnées."""
        resolved: list[tuple[dict, str]] = []
        unresolved: list[dict] = []
        for location in get_locations(criteria):
            code = _location_insee_code(location)
            if code:
                resolved.append((location, code))
            else:
                logger.warning(
                    f"[Laforet] Code INSEE introuvable pour {location['city']} "
                    f"{location['postalCode']}, requête séparée pour cette localisation"
                )
                unresolved.append(location)
        return resolved, unresolved

    def build_search_url(self, criteria: dict) -> str | None:
        """First location's URL — see build_search_urls() for all of them."""
        urls = self.build_search_urls(criteria)
        return urls[0] if urls else None

    def build_search_urls(self, criteria: dict) -> list[str]:
        """The URL(s) actually used to scrape — mirrors scrape()'s own
        strategy exactly, so "Voir l'URL" shows the truth: every location
        that resolves to an INSEE code is combined into one merged URL
        (filter[cities][]), not shown as separate links per city/postal
        code. Only a location that can't be resolved gets its own plain
        URL, matching the real per-location fallback.
        """
        if not get_locations(criteria):
            return []
        if not _property_types(criteria):
            # Seuls des types que Laforet ne référence pas (parking, terrain) :
            # aucune URL plutôt qu'un lien qui montrerait autre chose que ce qui
            # a été demandé.
            return []

        resolved, unresolved = self._split_locations(criteria)

        urls = []
        if resolved:
            primary_loc, _ = resolved[0]
            query_pairs = self._type_filters(criteria)
            query_pairs += [("filter[cities][]", code) for _, code in resolved]
            urls.append(f"{self._base_path(criteria, primary_loc)}?{urlencode(query_pairs)}")
        for location in unresolved:
            urls.append(self._base_path(criteria, location))
        return urls

    def scrape(self, criteria: dict) -> list[Listing]:
        locations = get_locations(criteria)
        if not locations:
            raise ValueError("Laforet nécessite au moins une localisation (ville + code postal) dans les critères")
        if not _property_types(criteria):
            raise ValueError(
                "Laforet ne référence aucun des types de bien demandés "
                f"(uniquement {sorted(TYPE_SLUGS)})"
            )

        session = requests.Session()
        session.headers.update({
            "User-Agent": DESKTOP_UA,
            "Accept": "text/html, application/xhtml+xml",
        })

        # Toutes les localisations résolues en code INSEE partent dans une
        # seule requête + pagination via filter[cities][] (vérifié en live :
        # ça combine correctement les résultats de plusieurs villes dans une
        # page bien cadrée). Celle qui ne se résout pas (code postal inconnu,
        # API geo injoignable) prend sa propre requête au lieu d'être
        # silencieusement abandonnée.
        resolved, unresolved = self._split_locations(criteria)

        seen: set[str] = set()
        listings: list[Listing] = []
        errors: list[str] = []
        attempts = 0
        failures = 0

        if resolved:
            attempts += 1
            try:
                listings.extend(
                    self._scrape_merged(session, criteria, resolved, seen)
                )
            except Exception as e:
                failures += 1
                logger.warning(f"[Laforet] Requête fusionnée ({len(resolved)} localisations) échouée: {e}")
                errors.append(f"requête fusionnée: {e}")
                unresolved = unresolved + [loc for loc, _ in resolved]

        for location in unresolved:
            attempts += 1
            try:
                listings.extend(
                    self._scrape_location(session, criteria, location, seen)
                )
            except Exception as e:
                failures += 1
                logger.warning(f"[Laforet] {location['city']} {location['postalCode']}: {e}")
                errors.append(f"{location['city']} {location['postalCode']}: {e}")

        if attempts and failures == attempts:
            raise ValueError("; ".join(errors))

        logger.info(f"[Laforet] Scraping terminé : {len(listings)} annonces uniques")
        return listings

    def _scrape_merged(self, session, criteria: dict, resolved: list, seen: set) -> list[Listing]:
        """One request (+ pagination) covering every resolved location at
        once, via filter[cities][]=<INSEE code> repeated per location."""
        primary_loc, _ = resolved[0]
        base_path = self._base_path(criteria, primary_loc)
        allowed_postal_codes = {loc["postalCode"] for loc, _ in resolved}

        base_query = self._type_filters(criteria)
        base_query += [("filter[cities][]", code) for _, code in resolved]

        listings: list[Listing] = []
        page = 1
        total_pages = 1

        while page <= total_pages and page <= MAX_PAGES:
            query = list(base_query)
            if page > 1:
                query.append(("page", page))

            resp = session.get(base_path, params=query, timeout=15)
            if resp.status_code == 404:
                raise ValueError(f"URL de base invalide ({base_path})")
            resp.raise_for_status()

            genuine_html = _extract_genuine_section(resp.text)
            if page == 1:
                total_pages = _parse_total_pages(genuine_html)

            for card in _parse_cards(genuine_html):
                if card["reference"] in seen:
                    continue
                listing = _dict_to_listing(card)
                if not _passes_filters(listing, criteria, allowed_postal_codes):
                    continue
                seen.add(card["reference"])
                listings.append(listing)

            page += 1

        if total_pages > MAX_PAGES:
            logger.warning(
                f"[Laforet] requête fusionnée : {total_pages} pages disponibles, limité à {MAX_PAGES} "
                f"({len(listings)} annonces récupérées, résultat partiel)"
            )

        return listings

    def _scrape_location(self, session, criteria: dict, location: dict, seen: set) -> list[Listing]:
        """Fallback path for a single location whose postal code couldn't
        be resolved to an INSEE code (or when the merged request failed)."""
        base_search_url = self._base_path(criteria, location)
        allowed_postal_codes = {location["postalCode"]}

        listings: list[Listing] = []
        page = 1
        total_pages = 1

        while page <= total_pages and page <= MAX_PAGES:
            sep = "&" if "?" in base_search_url else "?"
            page_url = base_search_url if page == 1 else f"{base_search_url}{sep}page={page}"

            resp = session.get(page_url, timeout=15)
            if resp.status_code == 404:
                raise ValueError(f"ville/code postal invalide ({page_url})")
            resp.raise_for_status()

            genuine_html = _extract_genuine_section(resp.text)
            if page == 1:
                total_pages = _parse_total_pages(genuine_html)

            for card in _parse_cards(genuine_html):
                if card["reference"] in seen:
                    continue
                listing = _dict_to_listing(card)
                if not _passes_filters(listing, criteria, allowed_postal_codes):
                    continue
                seen.add(card["reference"])
                listings.append(listing)

            page += 1

        if total_pages > MAX_PAGES:
            logger.warning(
                f"[Laforet] {location['city']} {location['postalCode']}: {total_pages} pages disponibles, "
                f"limité à {MAX_PAGES} ({len(listings)} annonces récupérées, résultat partiel)"
            )

        return listings
