"""SeLoger.com listing scraper wrapper.

Au lieu de parser du HTML, ce wrapper appelle le scraper API
qui récupère directement les données depuis les endpoints SeLoger.
"""

from __future__ import annotations

import json
from loguru import logger

from parsers.base import BaseParser
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


class SeLogerParser(BaseParser):
    """Scrape SeLoger via API BFF + classified-search."""

    SOURCE_ID = "seloger"
    SOURCE_NAME = "SeLoger"
    SOURCE_DESCRIPTION = "SeLoger.com — Scraping automatique via API"

    def scrape(self, criteria: dict) -> list[Listing]:
        """Execute le scraping avec les critères donnés et retourne les listings."""
        from scraper.seloger import scrape as do_scrape

        detailed, all_ids, total = do_scrape(criteria)

        if not detailed:
            logger.warning(f"[SeLoger] Aucune donnée détaillée, fallback sur IDs seuls")
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

        logger.info(f"[SeLoger] Scraping terminé : {len(unique)} annonces uniques")
        return unique

    def parse(self, html: str) -> list[Listing]:
        """Legacy: kept for interface compatibility but raises."""
        raise NotImplementedError(
            "SeLogerParser.parse() n'est plus supporté. Utilisez scrape(criteria) à la place."
        )
