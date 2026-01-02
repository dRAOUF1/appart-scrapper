from typing import List, Dict, Any, Tuple
from .base import BaseScraper
import logging
import json
import urllib.parse

logger = logging.getLogger(__name__)


class BienIciScraper(BaseScraper):
    def get_name(self) -> str:
        return "bienici"

    def get_start_url(self, search_criteria: dict = None) -> str:
        """Build the API URL with filters from config."""
        base_url = "https://www.bienici.com/realEstateAds.json"
        filters = self.config.get('filters', {})
        
        # Merge global search_criteria with local filters (local takes precedence)
        criteria = {**(search_criteria or {}), **filters}
        
        # Build filters JSON
        filter_obj = {
            "size": 24,
            "from": 0,
            "filterType": "rent",
            "propertyType": criteria.get('property_types', ["flat"]),
            "page": 1,
            "sortBy": "publicationDate",
            "sortOrder": "desc",
            "onTheMarket": [True],
            "mapMode": "enabled",
        }
        
        # Add optional filters
        if 'price_max' in criteria:
            filter_obj["maxPrice"] = criteria['price_max']
        if 'price_min' in criteria:
            filter_obj["minPrice"] = criteria['price_min']
        if 'surface_min' in criteria:
            filter_obj["minArea"] = criteria['surface_min']
        if 'zone_ids' in criteria:
            filter_obj["zoneIdsByTypes"] = {"zoneIds": criteria['zone_ids']}
        
        # Encode filters as JSON
        filters_json = json.dumps(filter_obj, separators=(',', ':'))
        
        # Build query params
        params = {
            "filters": filters_json,
            "extensionType": "extendedIfNoResult",
        }
        
        query = urllib.parse.urlencode(params)
        return f"{base_url}?{query}"

    def scrape_with_curl(self, session, config: dict) -> Tuple[List[Dict[str, Any]], bool]:
        """Scrape BienIci using their JSON API."""
        search_criteria = config.get('search_criteria', {})
        url = self.get_start_url(search_criteria)
        logger.info(f"[{self.get_name()}] Fetching API: {url[:100]}...")
        
        headers = {
            "accept": "application/json",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "fr-FR,fr;q=0.9",
        }
        
        try:
            response = session.get(url, headers=headers, timeout=30)
            
            # Check for blocking
            if response.status_code != 200:
                logger.warning(f"[{self.get_name()}] HTTP {response.status_code}")
                return [], True
            
            # Parse JSON response
            try:
                data = response.json()
            except json.JSONDecodeError as e:
                logger.error(f"[{self.get_name()}] Invalid JSON: {e}")
                # Save debug file
                with open("debug_bienici.html", "w", encoding="utf-8") as f:
                    f.write(response.text)
                return [], True
            
            # Extract listings
            listings = self.parse_listings(data)
            logger.info(f"[{self.get_name()}] Found {len(listings)} listings (total: {data.get('total', '?')})")
            
            return listings, False
            
        except Exception as e:
            logger.error(f"[{self.get_name()}] Error: {e}", exc_info=True)
            return [], False

    def parse_listings(self, data: dict) -> List[Dict[str, Any]]:
        """Parse the JSON API response."""
        listings = []
        
        ads = data.get('realEstateAds', [])
        
        for ad in ads:
            try:
                listing_id = ad.get('id', '')
                
                # Build URL (BienIci format)
                city_slug = ad.get('city', '').lower().replace(' ', '-')
                ad_url = f"https://www.bienici.com/annonce/location/{city_slug}/{listing_id}"
                
                # Price (can be number or with charges info)
                price = ad.get('price', 0)
                charges = ad.get('charges', 0)
                if charges:
                    price_str = f"{price}€/mois + {charges}€ charges"
                else:
                    price_str = f"{price}€/mois"
                
                # Location
                city = ad.get('city', 'Unknown')
                postal_code = ad.get('postalCode', '')
                district = ad.get('district', {})
                if isinstance(district, dict):
                    district_name = district.get('name', '')
                else:
                    district_name = ''
                
                location = f"{city} ({postal_code})"
                if district_name:
                    location = f"{district_name}, {location}"
                
                # Details
                surface = ad.get('surfaceArea', 0)
                rooms = ad.get('roomsQuantity', 0)
                bedrooms = ad.get('bedroomsQuantity', 0)
                floor = ad.get('floor', '')
                
                details_parts = []
                if rooms:
                    details_parts.append(f"{rooms} pièce{'s' if rooms > 1 else ''}")
                if bedrooms:
                    details_parts.append(f"{bedrooms} ch.")
                if surface:
                    details_parts.append(f"{surface} m²")
                if floor:
                    details_parts.append(f"Étage {floor}")
                
                details = " · ".join(details_parts)
                
                # Agency
                agency = ad.get('accountDisplayName', '')
                
                listings.append({
                    "id": listing_id,
                    "price": price_str,
                    "location": location,
                    "url": ad_url,
                    "title": ad.get('title', f"Logement à {city}"),
                    "details": details,
                    "agency": agency,
                    "furnished": ad.get('isFurnished', False),
                    "energy_class": ad.get('energyClassification', ''),
                })
                
            except Exception as e:
                logger.warning(f"[{self.get_name()}] Parse error: {e}")
                continue
        
        return listings

    def get_listing_id(self, listing: Dict[str, Any]) -> str:
        return listing.get("id")

    def format_notification(self, listing: Dict[str, Any]) -> str:
        agency = listing.get('agency', '')
        msg = (
            f"🏠 BienIci\n"
            f"💰 {listing.get('price')}\n"
            f"📍 {listing.get('location')}\n"
        )
        if agency:
            msg += f"🏢 {agency}\n"
        msg += f"🔗 {listing.get('url')}"
        return msg
