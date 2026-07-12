"""Laforet.com listing scraper.

Server-rendered (Symfony/Turbo) site, no JSON API. Location is encoded as a
human-readable city slug + postal code directly in the URL path (as opposed
to SeLoger's opaque placeIds), e.g.:

    https://www.laforet.com/ville/location-appartement-paris-75018

Filters (price, surface, rooms) and pagination work as query params on that
same page (`filter[min]`, `filter[max]`, `?page=N`) — no need to touch the
`/louer/rechercher` or `/acheter/rechercher` endpoints, which robots.txt
disallows.
"""

from __future__ import annotations

import json
import re
import unicodedata

import requests
from bs4 import BeautifulSoup
from loguru import logger

from models.listing import Listing
from parsers.base import BaseParser, ParserRegistry

BASE_URL = "https://www.laforet.com"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

MAX_PAGES = 30

TRANSACTION_SLUGS = {"Rent": "location", "Sale": "achat"}
TYPE_SLUGS = {"Apartment": "appartement", "House": "maison"}
TYPE_LABELS = {v: k for k, v in TYPE_SLUGS.items()}

_DETAIL_LINK_RE = re.compile(r"/agence-immobiliere/[^\"'\s]+-(\d+)$")
_PRICE_RE = re.compile(r"([\d\s ]+)\s*€")
_CITY_ZIP_RE = re.compile(r"([A-ZÀ-Ü][A-Za-zÀ-ÿ' \-]*?)\s*\((\d{5})\)")
_SURFACE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m²")
_ROOMS_RE = re.compile(r"(\d+)\s*pi[eè]ce")


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


def _type_slug(criteria: dict) -> str:
    estate_type = _first(criteria.get("estateTypes"), "Apartment")
    if estate_type not in TYPE_SLUGS:
        raise ValueError(
            f"Laforet ne supporte pas le type de bien '{estate_type}' "
            f"(uniquement {sorted(TYPE_SLUGS)})"
        )
    return TYPE_SLUGS[estate_type]


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


def _passes_filters(listing: Listing, criteria: dict) -> bool:
    """Enforce location + price/surface/rooms filters ourselves.

    Laforet's /ville/{...}-{postalCode} page is NOT scoped to that exact
    postal code — verified live it backfills with listings from neighboring
    arrondissements/communes when there aren't enough in the exact one (for
    Paris 75014, only 1 of 41 returned listings was actually in 75014). So a
    search for one postal code must not silently include others — every
    listing's own postal code is checked against the requested one here.

    filter[...] query params are never sent (see build_search_url) since
    they additionally break this scoping outright, so price/surface/rooms
    are also re-checked here rather than trusted from the server.
    """
    postal_code = criteria.get("postalCode")
    if postal_code and listing.zip_code and listing.zip_code != postal_code:
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

    # has_valid_criteria: no override needed — city+postalCode is exactly
    # BaseParser's default contract, and Laforet needs nothing else.

    def build_search_url(self, criteria: dict) -> str | None:
        """Build the plain, unfiltered city URL.

        Verified live: adding `filter[min]`/`filter[max]`/`filter[surface]`
        query params doesn't just fail to filter reliably (already worked
        around by _passes_filters below) — it silently breaks the city
        scoping itself, returning listings from all over France instead of
        the requested city. So we never send those params; price/surface/
        rooms filtering is enforced entirely client-side in scrape().
        """
        city = criteria.get("city")
        postal_code = criteria.get("postalCode")
        if not city or not postal_code:
            return None

        transaction = _transaction_slug(criteria)
        type_slug = _type_slug(criteria)
        return f"{BASE_URL}/ville/{transaction}-{type_slug}-{_slugify(city)}-{postal_code}"

    def scrape(self, criteria: dict, use_bff: bool = True) -> list[Listing]:
        """`use_bff` is a SeLoger-specific concept and is ignored here."""
        if not self.has_valid_criteria(criteria):
            raise ValueError("Laforet nécessite 'city' et 'postalCode' dans les critères")

        base_search_url = self.build_search_url(criteria)
        type_slug = _type_slug(criteria)
        property_type = TYPE_LABELS[type_slug]

        session = requests.Session()
        session.headers.update({"User-Agent": DESKTOP_UA})

        seen: set[str] = set()
        listings: list[Listing] = []
        page = 1
        total_pages = 1

        while page <= total_pages and page <= MAX_PAGES:
            sep = "&" if "?" in base_search_url else "?"
            page_url = base_search_url if page == 1 else f"{base_search_url}{sep}page={page}"

            resp = session.get(page_url, timeout=15)
            if resp.status_code == 404:
                raise ValueError(
                    f"Laforet: ville/code postal invalide ({page_url})"
                )
            resp.raise_for_status()

            if page == 1:
                total_pages = _parse_total_pages(resp.text)

            for card in _parse_cards(resp.text):
                if card["reference"] in seen:
                    continue
                seen.add(card["reference"])
                listing = _dict_to_listing(card, property_type)
                if _passes_filters(listing, criteria):
                    listings.append(listing)

            page += 1

        if total_pages > MAX_PAGES:
            logger.warning(
                f"[Laforet] {total_pages} pages disponibles, limité à {MAX_PAGES} "
                f"({len(listings)} annonces récupérées, résultat partiel)"
            )

        logger.info(f"[Laforet] Scraping terminé : {len(listings)} annonces uniques")
        return listings
