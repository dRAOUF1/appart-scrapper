"""SeLoger.com listing scraper wrapper.

Primary: Mobile iOS API (app-seloger.enigmatic-parrot-live.aws.aviv.eu)
Fallback: Legacy BFF + classified-search with LZ-string
"""

from __future__ import annotations

import json
from loguru import logger

from parsers.base import BaseParser, ParserRegistry
from storage import Listing


def _dict_to_listing(data: dict) -> Listing:
    """Convert a scraped dict into a Listing object."""
    photos = data.get("photos", [])
    phone = data.get("phone", [])

    image_url = ""
    if photos and isinstance(photos, list) and len(photos) > 0:
        first_photo = photos[0]
        if isinstance(first_photo, dict):
            image_url = first_photo.get("url", first_photo.get("source", ""))
        elif isinstance(first_photo, str):
            image_url = first_photo

    return Listing(
        listing_id=f"sl_{data.get('id', '')}",
        url=data.get("url", ""),
        title=data.get("title", ""),
        price=data.get("price", ""),
        surface=str(data["surface"]) if data.get("surface") is not None else "",
        rooms=str(data["rooms"]) if data.get("rooms") is not None else "",
        location=data.get("city", "") or data.get("district", ""),
        image_url=image_url,
        description=data.get("description", "")[:300] if data.get("description") else "",
        agency=data.get("agency", ""),
        source="seloger",
        legacy_id=str(data.get("legacyId", "")),
        price_value=data.get("priceValue"),
        price_details=data.get("priceDetails", ""),
        city=data.get("city", ""),
        district=data.get("district", ""),
        zip_code=data.get("zipCode", ""),
        property_type=data.get("propertyType", ""),
        is_private=data.get("isPrivate", False),
        phone=json.dumps(phone),
        epc=data.get("epc", ""),
        ges=data.get("ges", ""),
        is_new=data.get("isNew", False),
        is_exclusive=data.get("isExclusive", False),
        has_3d_visit=data.get("has3DVisit", False),
        creation_date=data.get("creationDate", ""),
        update_date=data.get("updateDate", ""),
        headline=data.get("headline", ""),
        photos=json.dumps(photos),
    )


@ParserRegistry.register
class SeLogerParser(BaseParser):
    """Scrape SeLoger via mobile iOS API (primary) + legacy fallback."""

    SOURCE_ID = "seloger"
    SOURCE_NAME = "SeLoger"
    SOURCE_DESCRIPTION = "SeLoger.com — API mobile iOS (bypass DataDome)"

    def scrape(self, criteria: dict, use_bff: bool = True) -> list[Listing]:
        """Execute le scraping — mobile API en priorité, fallback legacy."""
        try:
            return self._scrape_mobile(criteria)
        except Exception as e:
            logger.warning(f"[SeLoger] Mobile API échouée: {e}, fallback legacy...")

        try:
            return self._scrape_legacy(criteria, use_bff=use_bff)
        except Exception as e:
            logger.error(f"[SeLoger] Legacy scraping échoué: {e}")
            return []

    def _scrape_mobile(self, criteria: dict) -> list[Listing]:
        """Scrape via the mobile iOS API."""
        from scraper.seloger_mobile import scrape_mobile

        listings_data, all_ids, total = scrape_mobile(criteria)

        if not listings_data:
            raise ValueError("Mobile API: aucune annonce retournée")

        listings = [_dict_to_listing(d) for d in listings_data]

        seen: set[str] = set()
        unique: list[Listing] = []
        for li in listings:
            if li.listing_id not in seen:
                seen.add(li.listing_id)
                unique.append(li)

        logger.info(f"[SeLoger] Mobile API: {len(unique)} annonces uniques sur {total}")
        return unique

    def _scrape_legacy(self, criteria: dict, use_bff: bool = True) -> list[Listing]:
        """Scrape via the legacy BFF + HTML method."""
        from scraper.seloger import scrape as do_scrape

        detailed, all_ids, total = do_scrape(criteria, use_bff=use_bff)

        if not detailed:
            logger.warning(f"[SeLoger] Legacy: aucune donnée détaillée, fallback sur IDs seuls")
            listings = []
            for lid in all_ids:
                listings.append(Listing(
                    listing_id=f"sl_{lid}",
                    url=f"https://www.seloger.com/annonces/loc/{lid}.htm",
                    title=f"Annonce {lid}",
                    source="seloger",
                ))
            return listings

        listings = [_dict_to_listing(d) for d in detailed]

        seen: set[str] = set()
        unique: list[Listing] = []
        for li in listings:
            if li.listing_id not in seen:
                seen.add(li.listing_id)
                unique.append(li)

        logger.info(f"[SeLoger] Legacy: {len(unique)} annonces uniques")
        return unique

    def parse(self, html: str) -> list[Listing]:
        """Legacy: kept for interface compatibility but raises."""
        raise NotImplementedError(
            "SeLogerParser.parse() n'est plus supporté. Utilisez scrape(criteria) à la place."
        )

    def build_search_url(self, criteria: dict) -> str:
        """Reconstruct SeLoger search URL from criteria."""
        from scraper.seloger import build_search_url
        return build_search_url(criteria)
