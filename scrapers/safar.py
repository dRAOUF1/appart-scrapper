from typing import List, Dict, Any, Tuple
from .base import BaseScraper
import logging
import re
import urllib.parse

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

logger = logging.getLogger(__name__)


class SafarScraper(BaseScraper):
    """Scraper for Safar real estate website (safar.fr)."""
    
    def get_name(self) -> str:
        return "safar"

    def get_start_url(self, search_criteria: dict = None) -> str:
        """Build the search URL with filters from config."""
        filters = self.config.get('filters', {})
        
        # Merge global search_criteria with local filters
        criteria = {**(search_criteria or {}), **filters}
        
        # Get Paris zip codes from config or use defaults
        paris_zipcodes_raw = filters.get('paris_zipcodes', [
            "75001", "75002", "75003", "75004", "75005", "75006", "75008",
            "75010", "75011", "75012", "75013", "75014", "75015", "75016",
            "75017", "75018", "75020", "75116"
        ])
        # Format as "75001+PARIS" for the URL
        paris_zipcodes = [f"{z}+PARIS" for z in paris_zipcodes_raw]
        
        # Build base URL with required parameters
        base_url = "https://www.safar.fr/catalog/advanced_search_result.php"
        
        # Build query string manually to handle the complex C_65 parameter
        params = []
        params.append("action=update_search")
        params.append("C_28_search=EGAL")
        params.append("C_28_type=UNIQUE")
        params.append("C_28=Location")
        params.append("C_65_search=CONTIENT")
        params.append("C_65_type=TEXT")
        params.append("C_65=" + "%2C".join(paris_zipcodes))  # Join with encoded comma
        
        # Add individual C_65_tmp parameters
        for zipcode in paris_zipcodes:
            params.append(f"C_65_tmp={zipcode}")
        
        params.append("C_27_search=EGAL")
        params.append("C_27_type=TEXT")
        params.append("C_27=1")  # Property type: apartment
        
        # Add surface min
        if 'surface_min' in criteria:
            params.append(f"C_34_MIN={criteria['surface_min']}")
        params.append("C_34_search=COMPRIS")
        params.append("C_34_type=NUMBER")
        
        # Add price max (note: Safar may not respect this, we filter client-side too)
        if 'price_max' in criteria:
            params.append(f"C_30_MAX={criteria['price_max']}")
        params.append("C_30_search=COMPRIS")
        params.append("C_30_type=NUMBER")
        
        query = "&".join(params)
        return f"{base_url}?{query}"

    def scrape_with_curl(self, session, config: dict) -> Tuple[List[Dict[str, Any]], bool]:
        """Scrape Safar using curl_cffi session and HTML parsing."""
        if BeautifulSoup is None:
            logger.error(f"[{self.get_name()}] BeautifulSoup not installed")
            return [], False
        
        search_criteria = config.get('search_criteria', {})
        url = self.get_start_url(search_criteria)
        logger.info(f"[{self.get_name()}] Fetching: {url}")
        
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "fr-FR,fr;q=0.9",
            "referer": "https://www.safar.fr/",
        }
        
        try:
            response = session.get(url, headers=headers, timeout=30)
            
            if response.status_code not in [200, 202]:
                logger.warning(f"[{self.get_name()}] HTTP {response.status_code}")
                return [], True
            
            content = response.text
            content_lower = content.lower()
            
            # Check for anti-bot blocking
            is_blocked = (
                ("cf-browser-verification" in content_lower) or
                ("<title>access denied" in content_lower) or
                (len(content) < 1000)
            )
            
            if is_blocked:
                logger.warning(f"[{self.get_name()}] Anti-bot page detected")
                with open("debug_safar.html", "w", encoding="utf-8") as f:
                    f.write(content)
                return [], True
            
            # Save debug file for inspection
            with open("debug_safar.html", "w", encoding="utf-8") as f:
                f.write(content)
            
            # Parse HTML
            listings = self.parse_listings(content)
            
            # Client-side filtering (Safar doesn't always respect URL filters)
            search_criteria = config.get('search_criteria', {})
            price_max = search_criteria.get('price_max')
            price_min = search_criteria.get('price_min')
            surface_min = search_criteria.get('surface_min')
            
            original_count = len(listings)
            filtered_listings = []
            
            for listing in listings:
                price = listing.get('price_value', 0)
                surface = float(listing.get('surface', 0) or 0)
                
                # Check price max
                if price_max and price > price_max:
                    continue
                # Check price min
                if price_min and price < price_min:
                    continue
                # Check surface min
                if surface_min and surface < surface_min:
                    continue
                    
                filtered_listings.append(listing)
            
            if len(filtered_listings) < original_count:
                logger.info(f"[{self.get_name()}] Filtered out {original_count - len(filtered_listings)} listings not matching criteria")
            
            listings = filtered_listings
            
            logger.info(f"[{self.get_name()}] Found {len(listings)} listings")
            
            return listings, False
            
        except Exception as e:
            logger.error(f"[{self.get_name()}] Error: {e}", exc_info=True)
            return [], False

    def parse_listings(self, page_content: str) -> List[Dict[str, Any]]:
        """Parse the HTML page and extract listings."""
        listings = []
        soup = BeautifulSoup(page_content, 'html.parser')
        
        # Find all listing cards - try multiple selectors
        cards = soup.find_all('div', class_='product-container')
        if not cards:
            cards = soup.find_all('div', class_='listing-item')
        if not cards:
            cards = soup.find_all('div', class_='cell-product')
        
        for card in cards:
            try:
                # Get listing ID from data-productid attribute
                listing_id = ""
                fav_btn = card.find(attrs={'data-productid': True})
                if fav_btn:
                    listing_id = fav_btn.get('data-productid', '')
                
                # Try to extract ID from link if not found
                if not listing_id:
                    link = card.find('a', href=True)
                    if link:
                        href = link.get('href', '')
                        # Extract ID from URL pattern like "59627143"
                        id_match = re.search(r'_(\d+)/', href)
                        if id_match:
                            listing_id = id_match.group(1)
                
                if not listing_id:
                    continue
                
                # Build listing URL
                link_el = card.find('a', href=True)
                listing_url = ""
                if link_el:
                    href = link_el.get('href', '')
                    if href.startswith('../'):
                        listing_url = f"https://www.safar.fr/catalog/{href[3:]}"
                    elif href.startswith('/'):
                        listing_url = f"https://www.safar.fr{href}"
                    elif not href.startswith('http'):
                        listing_url = f"https://www.safar.fr/{href}"
                    else:
                        listing_url = href
                
                # Extract price - handle various space characters (regular, nbsp, narrow nbsp)
                price_el = card.find(class_='product-price')
                price_text = price_el.get_text(strip=True) if price_el else ""
                # Remove all types of spaces: regular, nbsp (\xa0), narrow nbsp (\u202f)
                price_clean = re.sub(r'[\s\xa0\u202f]+', '', price_text)
                price_match = re.search(r'(\d+)€', price_clean)
                price_value = int(price_match.group(1)) if price_match else 0
                price_str = f"{price_value}€/mois" if price_value else "Prix non indiqué"
                
                # Extract surface
                surface = ""
                surface_el = card.find(class_='data-list__item--Surface')
                if surface_el:
                    value_el = surface_el.find(class_='data-list__item--value')
                    if value_el:
                        surface = value_el.get_text(strip=True).replace(',', '.')
                
                # Extract rooms
                rooms = ""
                rooms_el = card.find(class_='data-list__item--NbPiece')
                if rooms_el:
                    value_el = rooms_el.find(class_='data-list__item--value')
                    if value_el:
                        rooms = value_el.get_text(strip=True)
                
                # Extract title/location
                title_el = card.find(class_='product-name')
                title_text = title_el.get_text(strip=True) if title_el else ""
                
                # Extract zipcode from title (5-digit pattern)
                zipcode = ""
                zipcode_match = re.search(r'\b(75\d{3})\b', title_text)
                if zipcode_match:
                    zipcode = zipcode_match.group(1)
                
                # Build location
                location = f"Paris ({zipcode})" if zipcode else "Paris"
                
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
            f"🏠 Safar\n"
            f"💰 {listing.get('price')}\n"
            f"📍 {listing.get('location')}\n"
            f"📐 {listing.get('details')}\n"
            f"🔗 {listing.get('url')}"
        )
        return msg
