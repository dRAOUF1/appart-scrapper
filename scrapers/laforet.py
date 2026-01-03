from typing import List, Dict, Any, Tuple
from .base import BaseScraper
import logging
import urllib.parse

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

logger = logging.getLogger(__name__)


class LaforetScraper(BaseScraper):
    """Scraper for Laforêt real estate website."""
    
    def get_name(self) -> str:
        return "laforet"

    def get_start_url(self, search_criteria: dict = None) -> str:
        """Build the search URL with filters from config."""
        base_url = "https://www.laforet.com/louer/rechercher"
        filters = self.config.get('filters', {})
        
        # Merge global search_criteria with local filters (local takes precedence)
        criteria = {**(search_criteria or {}), **filters}
        
        # Build query parameters
        params = {}
        
        # Property types (apartment by default)
        property_types = criteria.get('property_types', ['apartment'])
        for i, ptype in enumerate(property_types):
            params[f'filter[types][{i}]'] = ptype
        
        # Cities (Paris = 75056)
        cities = criteria.get('cities', [])
        for i, city in enumerate(cities):
            params[f'filter[cities][{i}]'] = city
        
        # Price range
        if 'price_min' in criteria:
            params['filter[min]'] = criteria['price_min']
        if 'price_max' in criteria:
            params['filter[max]'] = criteria['price_max']
        
        # Surface
        if 'surface_min' in criteria:
            params['filter[surface]'] = criteria['surface_min']
        
        query = urllib.parse.urlencode(params)
        return f"{base_url}?{query}"

    def scrape_with_curl(self, session, config: dict) -> Tuple[List[Dict[str, Any]], bool]:
        """Scrape Laforêt using curl_cffi session and HTML parsing."""
        if BeautifulSoup is None:
            logger.error(f"[{self.get_name()}] BeautifulSoup not installed. Run: pip install beautifulsoup4")
            return [], False
        
        search_criteria = config.get('search_criteria', {})
        url = self.get_start_url(search_criteria)
        logger.info(f"[{self.get_name()}] Fetching: {url}")
        
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "fr-FR,fr;q=0.9",
        }
        
        try:
            response = session.get(url, headers=headers, timeout=30)
            
            # Check for blocking
            if response.status_code != 200:
                logger.warning(f"[{self.get_name()}] HTTP {response.status_code}")
                return [], True
            
            # Check for actual anti-bot blocking page
            # Note: Laforet has reCAPTCHA scripts for forms, but that's not blocking
            content = response.text
            content_lower = content.lower()
            
            # Real blocking indicators (complete page takeover)
            is_blocked = (
                # Cloudflare challenge
                ("cf-browser-verification" in content_lower) or
                ("ray id" in content_lower and "cloudflare" in content_lower) or
                # Access denied pages
                ("<title>access denied" in content_lower) or
                ("<title>403" in content_lower) or
                # No actual content - just a challenge
                ("data-gtm-item-id-param" not in content and len(content) < 5000)
            )
            
            if is_blocked:
                logger.warning(f"[{self.get_name()}] Anti-bot page detected")
                with open("debug_laforet.html", "w", encoding="utf-8") as f:
                    f.write(content)
                return [], True
            
            # Parse HTML
            listings = self.parse_listings(content)
            
            # Filter by allowed zipcodes if configured
            allowed_zipcodes = self.config.get('filters', {}).get('allowed_zipcodes', [])
            if allowed_zipcodes:
                original_count = len(listings)
                listings = [
                    l for l in listings 
                    if any(l.get('zipcode', '').startswith(prefix) for prefix in allowed_zipcodes)
                ]
                filtered_count = original_count - len(listings)
                if filtered_count > 0:
                    logger.info(f"[{self.get_name()}] Filtered out {filtered_count} listings outside allowed zipcodes")
            
            logger.info(f"[{self.get_name()}] Found {len(listings)} listings")
            
            return listings, False
            
        except Exception as e:
            logger.error(f"[{self.get_name()}] Error: {e}", exc_info=True)
            return [], False

    def parse_listings(self, page_content: str) -> List[Dict[str, Any]]:
        """Parse the HTML page and extract listings using data-gtm-* attributes."""
        listings = []
        soup = BeautifulSoup(page_content, 'html.parser')
        
        # Find all article elements (listing cards)
        articles = soup.find_all('article')
        
        for article in articles:
            try:
                # Find the favorite button which contains the data-gtm-* attributes
                fav_button = article.find(attrs={"data-gtm-item-id-param": True})
                
                if not fav_button:
                    continue
                
                # Extract data from data-gtm-* attributes
                listing_id = fav_button.get('data-gtm-item-id-param', '')
                price = fav_button.get('data-gtm-item-price-param', '')
                zipcode = fav_button.get('data-gtm-item-zipcode-param', '')
                surface = fav_button.get('data-gtm-item-size-param', '')
                rooms = fav_button.get('data-gtm-item-rooms-nb-param', '')
                furnished = fav_button.get('data-gtm-item-meuble-param', 'false') == 'true'
                
                if not listing_id:
                    continue
                
                # Find the link to the listing
                link = article.find('a', href=True)
                listing_url = ""
                if link:
                    href = link.get('href', '')
                    if href.startswith('/'):
                        listing_url = f"https://www.laforet.com{href}"
                    else:
                        listing_url = href
                
                # Extract city from URL or zipcode
                city = self._extract_city_from_url(listing_url) or f"({zipcode})"
                
                # Build location string
                location = f"{city} ({zipcode})" if zipcode else city
                
                # Build details string
                details_parts = []
                if rooms:
                    details_parts.append(f"{rooms} pièce{'s' if int(rooms) > 1 else ''}")
                if surface:
                    details_parts.append(f"{surface} m²")
                if furnished:
                    details_parts.append("Meublé")
                details = " · ".join(details_parts)
                
                # Price formatting
                price_str = f"{price}€/mois" if price else "Prix non indiqué"
                
                listings.append({
                    "id": listing_id,
                    "price": price_str,
                    "price_value": int(float(price)) if price else 0,
                    "location": location,
                    "zipcode": zipcode,
                    "url": listing_url,
                    "title": f"Appartement {rooms} pièce{'s' if rooms and int(rooms) > 1 else ''} - {surface} m²",
                    "details": details,
                    "furnished": furnished,
                    "surface": surface,
                    "rooms": rooms,
                })
                
            except Exception as e:
                logger.warning(f"[{self.get_name()}] Parse error: {e}")
                continue
        
        return listings

    def _extract_city_from_url(self, url: str) -> str:
        """Extract city name from Laforet URL."""
        try:
            # URL format: .../louer/{city}/...
            parts = url.split('/louer/')
            if len(parts) > 1:
                city_part = parts[1].split('/')[0]
                # Convert slug to readable city name
                city = city_part.replace('-', ' ').title()
                return city
        except Exception:
            pass
        return ""

    def get_listing_id(self, listing: Dict[str, Any]) -> str:
        return listing.get("id", "")

    def format_notification(self, listing: Dict[str, Any]) -> str:
        msg = (
            f"🏠 Laforêt\n"
            f"💰 {listing.get('price')}\n"
            f"📍 {listing.get('location')}\n"
            f"📐 {listing.get('details')}\n"
            f"🔗 {listing.get('url')}"
        )
        return msg
