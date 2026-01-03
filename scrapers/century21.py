from typing import List, Dict, Any, Tuple
from .base import BaseScraper
import logging
import re

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

logger = logging.getLogger(__name__)


class Century21Scraper(BaseScraper):
    """Scraper for Century21 real estate website."""
    
    def get_name(self) -> str:
        return "century21"

    def get_start_url(self, search_criteria: dict = None) -> str:
        """Build the search URL with filters from config."""
        filters = self.config.get('filters', {})
        
        # Merge global search_criteria with local filters (local takes precedence)
        criteria = {**(search_criteria or {}), **filters}
        
        # Build URL parts
        # Original URL format: /annonces/f/location-appartement/v-paris/s-19-/st-0-/b-0-850/
        city = criteria.get('city', 'paris')
        surface_min = criteria.get('surface_min', 0)
        price_max = criteria.get('price_max', 850)
        
        # Note: Century21 uses b-0-{max} format, not b-{min}-{max}
        url = f"https://www.century21.fr/annonces/f/location-appartement/v-{city}/s-{surface_min}-/st-0-/b-0-{price_max}/"
        
        return url

    def scrape_with_curl(self, session, config: dict) -> Tuple[List[Dict[str, Any]], bool]:
        """Scrape Century21 using curl_cffi session and HTML parsing."""
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
            
            content = response.text
            content_lower = content.lower()
            
            # Check for anti-bot blocking
            is_blocked = (
                ("cf-browser-verification" in content_lower) or
                ("ray id" in content_lower and "cloudflare" in content_lower) or
                ("<title>access denied" in content_lower) or
                ("<title>403" in content_lower) or
                (len(content) < 5000)
            )
            
            if is_blocked:
                logger.warning(f"[{self.get_name()}] Anti-bot page detected")
                with open("debug_century21.html", "w", encoding="utf-8") as f:
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
        """Parse the HTML page and extract listings."""
        listings = []
        soup = BeautifulSoup(page_content, 'html.parser')
        
        # Find all listing cards
        cards = soup.find_all('div', class_='c-the-property-thumbnail-with-content')
        
        for card in cards:
            try:
                # Get listing ID from data-uid attribute
                listing_id = card.get('data-uid', '')
                
                if not listing_id:
                    # Try to find it in a parent or child
                    parent = card.find_parent(attrs={'data-uid': True})
                    if parent:
                        listing_id = parent.get('data-uid', '')
                
                if not listing_id:
                    continue
                
                # Build listing URL
                listing_url = f"https://www.century21.fr/trouver_logement/detail/{listing_id}/"
                
                # Extract price from .c-text-theme-heading-1
                price_el = card.find(class_='c-text-theme-heading-1')
                price_text = price_el.get_text(strip=True) if price_el else ""
                # Extract numeric value from price (e.g., "801 €" -> 801)
                price_match = re.search(r'(\d+)\s*€', price_text.replace('\xa0', ' '))
                price_value = int(price_match.group(1)) if price_match else 0
                price_str = f"{price_value}€/mois" if price_value else "Prix non indiqué"
                
                # Extract details from .c-text-theme-heading-4
                # This element contains two lines separated by <br>:
                # Line 1: City and zipcode (e.g., "PARIS 75013")
                # Line 2: Surface and rooms (e.g., "19,25 m2, 1 pièce")
                details_el = card.find(class_='c-text-theme-heading-4')
                
                city = ""
                zipcode = ""
                surface = ""
                rooms = ""
                
                if details_el:
                    # The HTML structure has <br> separating lines and <sup>2</sup> for m²
                    # Replace <sup>2</sup> with ² before processing
                    for sup in details_el.find_all('sup'):
                        sup.replace_with('²')
                    
                    # Get text splitting by <br> tags
                    # First, replace <br> with a unique separator
                    html_str = str(details_el)
                    html_str = re.sub(r'<br\s*/?>', '|||', html_str)
                    temp_soup = BeautifulSoup(html_str, 'html.parser')
                    full_text = temp_soup.get_text()
                    lines = [line.strip() for line in full_text.split('|||') if line.strip()]
                    
                    if len(lines) >= 1:
                        # First line: City and zipcode (e.g., "PARIS 75013")
                        first_line = lines[0]
                        zipcode_match = re.search(r'\b(\d{5})\b', first_line)
                        if zipcode_match:
                            zipcode = zipcode_match.group(1)
                            # City is everything before the zipcode
                            city = first_line[:first_line.find(zipcode)].strip()
                            # Clean up non-breaking spaces
                            city = city.replace('\xa0', ' ').strip()
                    
                    if len(lines) >= 2:
                        # Second line: Surface and rooms (e.g., "19,25 m², 1 pièce")
                        second_line = lines[1]
                        # Extract surface (handle both m² and m2)
                        surface_match = re.search(r'([\d,]+)\s*m[²2]', second_line)
                        if surface_match:
                            surface = surface_match.group(1).replace(',', '.')
                        # Extract rooms
                        rooms_match = re.search(r'(\d+)\s*pièce', second_line)
                        if rooms_match:
                            rooms = rooms_match.group(1)
                
                # Build location string
                location = f"{city} ({zipcode})" if city and zipcode else f"{city}{zipcode}"
                
                # Build details string
                details_parts = []
                if rooms:
                    details_parts.append(f"{rooms} pièce{'s' if int(rooms) > 1 else ''}")
                if surface:
                    details_parts.append(f"{surface} m²")
                details = " · ".join(details_parts) if details_parts else ""
                
                listings.append({
                    "id": listing_id,
                    "price": price_str,
                    "price_value": price_value,
                    "location": location,
                    "zipcode": zipcode,
                    "url": listing_url,
                    "title": f"Appartement {rooms} pièce{'s' if rooms and int(rooms) > 1 else ''} - {surface} m²" if rooms and surface else "Appartement",
                    "details": details,
                    "surface": surface,
                    "rooms": rooms,
                })
                
            except Exception as e:
                logger.warning(f"[{self.get_name()}] Parse error: {e}")
                continue
        
        return listings

    def get_listing_id(self, listing: Dict[str, Any]) -> str:
        return listing.get("id", "")

    def format_notification(self, listing: Dict[str, Any]) -> str:
        msg = (
            f"🏠 Century21\n"
            f"💰 {listing.get('price')}\n"
            f"📍 {listing.get('location')}\n"
            f"📐 {listing.get('details')}\n"
            f"🔗 {listing.get('url')}"
        )
        return msg
