"""SeLoger.com listing parser."""

from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup
from loguru import logger

from parsers.base import BaseParser
from storage import Listing


def _generate_listing_id(url: str) -> str:
    """Generate a unique ID for a listing based on its URL."""
    match = re.search(r'/(\d{5,})\.htm', url)
    if match:
        return f"sl_{match.group(1)}"
    return f"sl_{hashlib.md5(url.encode()).hexdigest()[:12]}"


class SeLogerParser(BaseParser):
    """Parse SeLoger search-result HTML and extract listings."""

    SOURCE_ID = "seloger"
    SOURCE_NAME = "SeLoger"
    SOURCE_DESCRIPTION = "SeLoger.com — Annonces immobilières"

    SELECTORS = {
        "cards": [
            "[data-testid^='classified-card-mfe']",
        ],
        "price": [
            "[data-testid='cardmfe-price-testid']",
        ],
        "title": [
            "[data-testid='cardmfe-description-box-text-test-id']",
        ],
        "location": [
            "[data-testid='cardmfe-description-box-address']",
        ],
        "keyfacts": [
            "[data-testid='cardmfe-keyfacts-testid']",
        ],
        "link": [
            "a[data-testid='card-mfe-covering-link-testid']",
            "a[href*='/annonces/']",
        ],
        "image": [
            "img",
        ],
        "agency": [
            "[data-testid='cardmfe-card-bottom-strip-test-id']",
        ],
        "description": [
            "[data-testid='cardmfe-description-text-test-id']",
        ],
    }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self, html: str) -> list[Listing]:
        """Parse raw HTML from a SeLoger search-results page."""
        # Strategy 1: embedded JSON
        listings = self._extract_from_script_data(html)

        # Strategy 2: HTML cards
        if not listings:
            listings = self._extract_listings_from_html(html)

        # Deduplicate
        seen: set[str] = set()
        unique: list[Listing] = []
        for li in listings:
            if li.listing_id not in seen:
                seen.add(li.listing_id)
                unique.append(li)

        logger.info(f"[SeLoger] Parsing terminé : {len(unique)} annonces uniques")
        return unique

    # ------------------------------------------------------------------
    # Private — HTML extraction
    # ------------------------------------------------------------------

    def _find_element_multi(self, parent, selectors: list[str], attr: str = "text") -> str:
        for selector in selectors:
            try:
                el = parent.select_one(selector)
                if el:
                    if attr == "text":
                        return el.get_text(strip=True)
                    elif attr == "href":
                        return el.get("href", "")
                    elif attr == "src":
                        return el.get("src", "") or el.get("data-src", "")
                    else:
                        return el.get(attr, "")
            except Exception:
                continue
        return ""

    def _extract_listings_from_html(self, html: str) -> list[Listing]:
        soup = BeautifulSoup(html, "lxml")
        listings: list[Listing] = []

        cards = []
        for selector in self.SELECTORS["cards"]:
            cards = soup.select(selector)
            if cards:
                logger.debug(f"Trouvé {len(cards)} cartes avec : {selector}")
                break

        if not cards:
            logger.debug("Aucun sélecteur de carte, recherche par liens...")
            links = soup.find_all("a", href=re.compile(r"seloger\.com/annonces/"))
            for link in links:
                url = link.get("href", "")
                if url and not url.startswith("http"):
                    url = f"https://www.seloger.com{url}"
                listings.append(Listing(
                    listing_id=_generate_listing_id(url),
                    url=url,
                    title=link.get_text(strip=True)[:200],
                    source="seloger",
                ))
            return listings

        for card in cards:
            try:
                link_el = (
                    card.select_one("a[data-testid='card-mfe-covering-link-testid']")
                    or card.select_one("a[href*='/annonces/']")
                )
                if not link_el:
                    continue

                link_url = link_el.get("href", "")
                if link_url and not link_url.startswith("http"):
                    link_url = f"https://www.seloger.com{link_url}"
                if not link_url or "seloger.com" not in link_url:
                    continue

                listing_id = _generate_listing_id(link_url)

                price = self._find_element_multi(card, self.SELECTORS["price"])
                location = self._find_element_multi(card, self.SELECTORS["location"])
                keyfacts = self._find_element_multi(card, self.SELECTORS["keyfacts"])
                agency = self._find_element_multi(card, self.SELECTORS["agency"])
                description = self._find_element_multi(card, self.SELECTORS["description"])
                image_url = self._find_element_multi(card, self.SELECTORS["image"], attr="src")

                surface = ""
                rooms = ""
                if keyfacts:
                    parts = [p.strip() for p in keyfacts.replace("·", "|").split("|")]
                    for part in parts:
                        if "m²" in part or "m2" in part.lower():
                            surface = part
                        elif "pièce" in part.lower() or "piece" in part.lower():
                            rooms = part

                title = link_el.get("title", "")
                if not title:
                    title = self._find_element_multi(card, self.SELECTORS["title"])

                listings.append(Listing(
                    listing_id=listing_id,
                    url=link_url,
                    title=title[:200] if title else "",
                    price=price,
                    surface=surface,
                    rooms=rooms,
                    location=location,
                    image_url=image_url,
                    description=description[:300] if description else "",
                    agency=agency,
                    source="seloger",
                ))

            except Exception as e:
                logger.debug(f"Erreur extraction carte : {e}")
                continue

        return listings

    # ------------------------------------------------------------------
    # Private — Embedded JSON extraction
    # ------------------------------------------------------------------

    def _extract_from_script_data(self, html: str) -> list[Listing]:
        listings: list[Listing] = []
        soup = BeautifulSoup(html, "lxml")

        for script in soup.find_all("script"):
            script_text = script.string or ""
            for pattern in [
                r'window\["initialData"\]\s*=\s*(\{.*?\});',
                r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});',
                r'__NEXT_DATA__.*?(\{"props".*?\})',
            ]:
                match = re.search(pattern, script_text, re.DOTALL)
                if match:
                    try:
                        import json
                        data = json.loads(match.group(1))
                        listings.extend(self._parse_json_listings(data))
                        if listings:
                            logger.info(
                                f"[SeLoger] {len(listings)} annonces depuis JSON embarqué"
                            )
                            return listings
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.debug(f"Échec parsing JSON embarqué : {e}")

        return listings

    def _parse_json_listings(self, data: dict, depth: int = 0) -> list[Listing]:
        listings: list[Listing] = []
        if depth > 10:
            return listings

        if isinstance(data, dict):
            has_price = any(k in data for k in ["price", "prix", "pricing"])
            has_id = any(k in data for k in ["id", "listingId", "classifiedId"])

            if has_price and has_id:
                listing_id = str(
                    data.get("id") or data.get("listingId") or
                    data.get("classifiedId", "")
                )
                if listing_id:
                    price_data = data.get("price") or data.get("pricing", {})
                    if isinstance(price_data, dict):
                        price = price_data.get("price", price_data.get("value", ""))
                        price = f"{price} €" if price else ""
                    else:
                        price = str(price_data) if price_data else ""

                    url = (
                        data.get("url")
                        or data.get("permalink")
                        or data.get("classifiedURL", "")
                    )
                    if url and not url.startswith("http"):
                        url = f"https://www.seloger.com{url}"

                    agency_data = data.get("agency") or data.get("advertiser", {})
                    if isinstance(agency_data, dict):
                        agency = agency_data.get("name", agency_data.get("label", ""))
                    else:
                        agency = str(agency_data) if agency_data else ""

                    listings.append(Listing(
                        listing_id=f"sl_{listing_id}",
                        url=url,
                        title=str(data.get("title", data.get("description", ""))),
                        price=str(price),
                        surface=str(data.get("livingArea", data.get("surface", ""))),
                        rooms=str(data.get("rooms", data.get("nbRooms", ""))),
                        location=str(data.get("city", data.get("zipCode", ""))),
                        image_url=str(
                            data.get("photos", [""])[0] if data.get("photos") else ""
                        ),
                        agency=str(agency),
                        source="seloger",
                    ))

            for value in data.values():
                if isinstance(value, (dict, list)):
                    listings.extend(self._parse_json_listings(value, depth + 1))

        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    listings.extend(self._parse_json_listings(item, depth + 1))

        return listings
