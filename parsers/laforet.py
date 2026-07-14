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
see _resolve_insee_code().

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
TRANSACTION_SLUGS = {"Rent": "location", "Sale": "achat"}
TRANSACTION_FILTER_VALUES = {"Rent": "rent", "Sale": "buy"}
TYPE_SLUGS = {"Apartment": "appartement", "House": "maison"}
TYPE_FILTER_VALUES = {"Apartment": "apartment", "House": "house"}
TYPE_LABELS = {v: k for k, v in TYPE_SLUGS.items()}

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

# INSEE code lookups never change during a process's life — cache them so a
# search scraped every few minutes forever doesn't hit the public geo API
# on every single run.
_INSEE_CACHE: dict[str, str | None] = {}


def _slugify(text: str) -> str:
    """Lowercase, strip accents, non-alnum -> '-' (e.g. 'Le Kremlin-Bicêtre' -> 'le-kremlin-bicetre')."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug


def _first(value, default=None):
    if isinstance(value, list):
        return value[0] if value else default
    return value or default


def _transaction_slug(criteria: dict) -> str:
    distribution = _first(criteria.get("distributionTypes"), "Rent")
    return TRANSACTION_SLUGS.get(distribution, "location")


def _transaction_filter_value(criteria: dict) -> str:
    distribution = _first(criteria.get("distributionTypes"), "Rent")
    return TRANSACTION_FILTER_VALUES.get(distribution, "rent")


def _type_slug(criteria: dict) -> str:
    estate_type = _first(criteria.get("estateTypes"), "Apartment")
    if estate_type not in TYPE_SLUGS:
        raise ValueError(
            f"Laforet ne supporte pas le type de bien '{estate_type}' "
            f"(uniquement {sorted(TYPE_SLUGS)})"
        )
    return TYPE_SLUGS[estate_type]


def _type_filter_value(criteria: dict) -> str:
    estate_type = _first(criteria.get("estateTypes"), "Apartment")
    return TYPE_FILTER_VALUES[estate_type]


def _arrondissement_insee_code(postal_code: str) -> str | None:
    """Paris/Lyon/Marseille arrondissements: INSEE's `/communes` API only
    tracks these at the whole-city level (75056/69123/13055), but Laforet's
    filter[cities][] needs the arrondissement-specific "commune associée"
    code. Formulas verified against Laforet's own embedded page state for
    several arrondissements of each city (75014->75114, 69007->69387,
    13001->13201, etc.) — not guessed, checked against real values Laforet
    itself computes for its own default single-arrondissement pages.
    """
    if len(postal_code) != 5 or not postal_code.isdigit():
        return None
    if postal_code.startswith("75"):
        arr = int(postal_code[-2:])
        if 1 <= arr <= 20:
            return f"751{arr:02d}"
    elif postal_code.startswith("690"):
        arr = int(postal_code[-1])
        if 1 <= arr <= 9:
            return f"693{80 + arr}"
    elif postal_code.startswith("130"):
        arr = int(postal_code[-2:])
        if 1 <= arr <= 16:
            return f"132{arr:02d}"
    return None


def _lookup_insee_code(postal_code: str) -> str | None:
    """Resolve any other French postal code via the official, free, public
    geo.api.gouv.fr API (no key, no auth) — the same API Laforet's own city
    autocomplete calls (verified live via network capture)."""
    try:
        resp = requests.get(
            "https://geo.api.gouv.fr/communes",
            params={"codePostal": postal_code, "fields": "code"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0]["code"] if data else None
    except Exception as e:
        logger.warning(f"[Laforet] Résolution INSEE échouée pour {postal_code}: {e}")
        return None


def _resolve_insee_code(postal_code: str) -> str | None:
    """Postal code -> the commune code Laforet's filter[cities][] expects.
    None if it can't be resolved (unknown/foreign postal code, or the geo
    API is unreachable) — callers fall back to a per-location request."""
    if postal_code not in _INSEE_CACHE:
        code = _arrondissement_insee_code(postal_code)
        if code is None:
            code = _lookup_insee_code(postal_code)
        _INSEE_CACHE[postal_code] = code
    return _INSEE_CACHE[postal_code]


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

    space_min = criteria.get("spaceMin")
    space_max = criteria.get("spaceMax")
    if listing.surface:
        try:
            surface = float(listing.surface.replace(",", "."))
        except ValueError:
            surface = None
        if surface is not None:
            if space_min and surface < space_min:
                return False
            if space_max and surface > space_max:
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


def _dict_to_listing(data: dict, property_type: str) -> Listing:
    price_value = data["price_value"]
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

    URL_NOTE = (
        "Ce lien ne montre que la localisation : les filtres prix/surface/pièces "
        "sont appliqués par le scraper mais volontairement absents de l'URL, "
        "car leur effet côté site n'est pas assez fiable pour s'y fier seul."
    )

    # has_valid_criteria: no override needed — get_locations() (at least one
    # city+postalCode pair) is exactly BaseParser's default contract.

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
        locations = get_locations(criteria)
        if not locations:
            return []

        transaction = _transaction_slug(criteria)
        type_slug = _type_slug(criteria)

        resolved = []
        unresolved = []
        for loc in locations:
            code = _resolve_insee_code(loc["postalCode"])
            (resolved if code else unresolved).append((loc, code) if code else loc)

        urls = []
        if resolved:
            primary_loc, _ = resolved[0]
            base_path = (
                f"{BASE_URL}/ville/{transaction}-{type_slug}-"
                f"{_slugify(primary_loc['city'])}-{primary_loc['postalCode']}"
            )
            query_pairs = [("filter[types][]", _type_filter_value(criteria))]
            query_pairs += [("filter[cities][]", code) for _, code in resolved]
            urls.append(f"{base_path}?{urlencode(query_pairs)}")
        for loc in unresolved:
            urls.append(f"{BASE_URL}/ville/{transaction}-{type_slug}-{_slugify(loc['city'])}-{loc['postalCode']}")
        return urls

    def scrape(self, criteria: dict, use_bff: bool = True) -> list[Listing]:
        """`use_bff` is a SeLoger-specific concept and is ignored here."""
        locations = get_locations(criteria)
        if not locations:
            raise ValueError("Laforet nécessite au moins une localisation (ville + code postal) dans les critères")

        type_slug = _type_slug(criteria)
        property_type = TYPE_LABELS[type_slug]

        session = requests.Session()
        session.headers.update({
            "User-Agent": DESKTOP_UA,
            "Accept": "text/html, application/xhtml+xml",
        })

        # Resolve every location to the INSEE-style code filter[cities][]
        # needs, so they can all be merged into one request+pagination
        # (verified live: this correctly combines multiple cities' results
        # in a single, properly-scoped page). A location that can't be
        # resolved (unknown postal code, geo API unreachable) falls back to
        # its own separate request instead of being silently dropped.
        resolved: list[tuple[dict, str]] = []
        unresolved: list[dict] = []
        for loc in locations:
            code = _resolve_insee_code(loc["postalCode"])
            if code:
                resolved.append((loc, code))
            else:
                logger.warning(
                    f"[Laforet] Code INSEE introuvable pour {loc['city']} {loc['postalCode']}, "
                    "requête séparée pour cette localisation"
                )
                unresolved.append(loc)

        seen: set[str] = set()
        listings: list[Listing] = []
        errors: list[str] = []
        attempts = 0
        failures = 0

        if resolved:
            attempts += 1
            try:
                listings.extend(
                    self._scrape_merged(session, criteria, resolved, property_type, seen)
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
                    self._scrape_location(session, criteria, location, property_type, seen)
                )
            except Exception as e:
                failures += 1
                logger.warning(f"[Laforet] {location['city']} {location['postalCode']}: {e}")
                errors.append(f"{location['city']} {location['postalCode']}: {e}")

        if attempts and failures == attempts:
            raise ValueError("; ".join(errors))

        logger.info(f"[Laforet] Scraping terminé : {len(listings)} annonces uniques")
        return listings

    def _scrape_merged(self, session, criteria: dict, resolved: list, property_type: str, seen: set) -> list[Listing]:
        """One request (+ pagination) covering every resolved location at
        once, via filter[cities][]=<INSEE code> repeated per location."""
        primary_loc, _ = resolved[0]
        transaction = _transaction_slug(criteria)
        type_slug = _type_slug(criteria)
        base_path = (
            f"{BASE_URL}/ville/{transaction}-{type_slug}-"
            f"{_slugify(primary_loc['city'])}-{primary_loc['postalCode']}"
        )
        city_codes = [code for _, code in resolved]
        allowed_postal_codes = {loc["postalCode"] for loc, _ in resolved}

        base_query = [("filter[types][]", _type_filter_value(criteria))]
        base_query += [("filter[cities][]", code) for code in city_codes]

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
                listing = _dict_to_listing(card, property_type)
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

    def _scrape_location(self, session, criteria: dict, location: dict, property_type: str, seen: set) -> list[Listing]:
        """Fallback path for a single location whose postal code couldn't
        be resolved to an INSEE code (or when the merged request failed)."""
        transaction = _transaction_slug(criteria)
        type_slug = _type_slug(criteria)
        base_search_url = (
            f"{BASE_URL}/ville/{transaction}-{type_slug}-"
            f"{_slugify(location['city'])}-{location['postalCode']}"
        )
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
                listing = _dict_to_listing(card, property_type)
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
