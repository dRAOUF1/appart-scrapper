from typing import List, Dict, Any, Tuple
from .base import BaseScraper
import logging
import re

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

logger = logging.getLogger(__name__)


class ValiereCortezScraper(BaseScraper):
    """Scraper for Valiere Cortez real estate website."""
    
    def get_name(self) -> str:
        return "valierecortez"

    def get_start_url(self, search_criteria: dict = None) -> str:
        """Build the search URL - Valiere Cortez uses path-based filtering."""
        # Base URL for rentals
        base_url = "https://www.valierecortez.com/type/a-louer/"
        
        # Valiere Cortez uses query params for filtering:
        # ?localisation=paris-11e,paris-16e
        # ?pieces=studio,2-pieces
        # ?superficie=moins-de-20m²,entre-20-et-30m²
        # ?loyer=entre-500-et-800e,entre-800-et-1200e
        
        # For now, we'll scrape all Paris rentals and filter client-side
        # since their filter system is complex with specific French text values
        return base_url

    def scrape_with_curl(self, session, config: dict) -> Tuple[List[Dict[str, Any]], bool]:
        """Scrape Valiere Cortez using curl_cffi session and HTML parsing."""
        if BeautifulSoup is None:
            logger.error(f"[{self.get_name()}] BeautifulSoup not installed")
            return [], False
        
        search_criteria = config.get('search_criteria', {})
        base_url = "https://www.valierecortez.com/type/a-louer/"
        ajax_url = "https://www.valierecortez.com/wp-admin/admin-ajax.php"
        
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "fr-FR,fr;q=0.9",
            "referer": base_url,
        }
        
        try:
            all_listings = []
            
            # First, fetch the main page to get initial listings
            logger.info(f"[{self.get_name()}] Fetching: {base_url}")
            response = session.get(base_url, headers=headers, timeout=30)
            
            if response.status_code != 200:
                logger.warning(f"[{self.get_name()}] HTTP {response.status_code}")
                return [], True
            
            content = response.text
            
            # Check for anti-bot blocking
            if len(content) < 1000:
                logger.warning(f"[{self.get_name()}] Anti-bot page detected")
                return [], True
            
            # Parse initial listings
            initial_listings = self.parse_listings(content)
            all_listings.extend(initial_listings)
            logger.info(f"[{self.get_name()}] Initial page: {len(initial_listings)} listings")
            
            # Now fetch additional pages using AJAX endpoint
            # The initial page is "offset 1", so we start fetching from offset 2
            offset = 2
            max_pages = 10  # Safety limit
            
            while offset <= max_pages:
                ajax_params = f"?action=load_more_posts&cat_slug=a-louer&post_type=bien&cat_tax=type&offset={offset}"
                ajax_full_url = ajax_url + ajax_params
                
                try:
                    ajax_response = session.get(ajax_full_url, headers=headers, timeout=30)
                    
                    if ajax_response.status_code != 200:
                        break
                    
                    ajax_content = ajax_response.text.strip()
                    
                    # If response is empty or very short, we've reached the end
                    if not ajax_content or len(ajax_content) < 100:
                        break
                    
                    # Parse AJAX response (it returns li elements directly)
                    ajax_listings = self.parse_ajax_listings(ajax_content)
                    
                    if ajax_listings:
                        all_listings.extend(ajax_listings)
                        logger.info(f"[{self.get_name()}] AJAX offset {offset}: {len(ajax_listings)} more listings")
                    
                    offset += 1
                    
                except Exception as e:
                    logger.warning(f"[{self.get_name()}] AJAX error at offset {offset}: {e}")
                    break
            
            logger.info(f"[{self.get_name()}] Total raw listings: {len(all_listings)}")
            
            # Client-side filtering by criteria
            price_max = search_criteria.get('price_max')
            price_min = search_criteria.get('price_min')
            surface_min = search_criteria.get('surface_min')
            
            # Get allowed zipcodes from config
            allowed_zipcodes = self.config.get('filters', {}).get('allowed_zipcodes', [])
            
            original_count = len(all_listings)
            filtered_listings = []
            
            for listing in all_listings:
                price = listing.get('price_value', 0)
                surface = float(listing.get('surface', 0) or 0)
                zipcode = listing.get('zipcode', '')
                
                # Check price max
                if price_max and price > price_max:
                    continue
                # Check price min
                if price_min and price < price_min:
                    continue
                # Check surface min
                if surface_min and surface < surface_min:
                    continue
                # Check allowed zipcodes
                if allowed_zipcodes and not any(zipcode.startswith(prefix) for prefix in allowed_zipcodes):
                    continue
                    
                filtered_listings.append(listing)
            
            if len(filtered_listings) < original_count:
                logger.info(f"[{self.get_name()}] Filtered out {original_count - len(filtered_listings)} listings not matching criteria")
            
            logger.info(f"[{self.get_name()}] Found {len(filtered_listings)} listings")
            
            return filtered_listings, False
            
        except Exception as e:
            logger.error(f"[{self.get_name()}] Error: {e}", exc_info=True)
            return [], False

    def parse_ajax_listings(self, ajax_content: str) -> List[Dict[str, Any]]:
        """Parse the AJAX response HTML (li elements) and extract listings."""
        # Wrap in a ul tag for proper parsing
        wrapped_html = f"<ul>{ajax_content}</ul>"
        return self.parse_listings(wrapped_html)

    def parse_listings(self, page_content: str) -> List[Dict[str, Any]]:
        """Parse the HTML page and extract listings."""
        listings = []
        soup = BeautifulSoup(page_content, 'html.parser')
        
        # Find all listing links containing figures
        # Structure: <a href="..."><figure>...</figure></a>
        listing_links = soup.find_all('a', href=lambda x: x and '/bien/' in x)
        
        for link in listing_links:
            try:
                href = link.get('href', '')
                
                # Skip rented or reserved listings
                link_text = link.get_text()
                if 'loué' in link_text.lower() or 'en cours de signature' in link_text.lower():
                    continue
                
                # Skip commercial properties
                if 'local commercial' in link_text.lower() or 'boutique' in link_text.lower():
                    continue
                
                # Extract listing ID from URL slug
                # URL format: https://www.valierecortez.com/bien/studio-meuble-paris-11/
                listing_id = href.rstrip('/').split('/')[-1]
                
                if not listing_id:
                    continue
                
                # Find the figure element
                figure = link.find('figure')
                if not figure:
                    continue
                
                figcaption = figure.find('figcaption')
                if not figcaption:
                    # Try to get text from figure directly
                    figcaption = figure
                
                # Get all text content
                text_content = figcaption.get_text(separator='\n')
                lines = [line.strip() for line in text_content.split('\n') if line.strip()]
                
                # Extract title from h2
                title_el = figcaption.find('h2')
                title = title_el.get_text(strip=True) if title_el else ""
                
                # Extract price (look for € pattern)
                price_value = 0
                price_str = "Prix non indiqué"
                for line in lines:
                    # Handle various space types
                    clean_line = re.sub(r'[\s\xa0\u202f]+', '', line)
                    price_match = re.search(r'(\d+)€', clean_line)
                    if price_match:
                        price_value = int(price_match.group(1))
                        price_str = f"{price_value}€/mois"
                        break
                
                # Extract surface (look for "Surface X m2" pattern)
                surface = ""
                for line in lines:
                    surface_match = re.search(r'Surface\s*([\d,\.]+)\s*m', line, re.IGNORECASE)
                    if surface_match:
                        surface = surface_match.group(1).replace(',', '.')
                        break
                
                # Extract rooms (look for "X pièces" or "Studio")
                rooms = ""
                for line in lines:
                    if 'studio' in line.lower():
                        rooms = "1"
                        break
                    rooms_match = re.search(r'(\d+)\s*pièces?', line, re.IGNORECASE)
                    if rooms_match:
                        rooms = rooms_match.group(1)
                        break
                
                # Extract location/arrondissement from title
                location = ""
                zipcode = ""
                # Pattern: "PARIS 11" or "PARIS 16"
                arr_match = re.search(r'PARIS\s*(\d+)', title, re.IGNORECASE)
                if arr_match:
                    arr_num = arr_match.group(1)
                    # Convert to zipcode (e.g., "11" -> "75011")
                    zipcode = f"75{arr_num.zfill(3)}" if len(arr_num) < 3 else f"75{arr_num}"
                    location = f"Paris {arr_num}e"
                else:
                    # Check for cities outside Paris
                    city_match = re.search(r',\s*([A-Za-zÀ-ÿ\s-]+)$', title)
                    if city_match:
                        location = city_match.group(1).strip()
                
                # Build details string
                details_parts = []
                if rooms:
                    room_label = "Studio" if rooms == "1" else f"{rooms} pièces"
                    details_parts.append(room_label)
                if surface:
                    details_parts.append(f"{surface} m²")
                details = " · ".join(details_parts) if details_parts else ""
                
                listings.append({
                    "id": listing_id,
                    "price": price_str,
                    "price_value": price_value,
                    "location": location,
                    "zipcode": zipcode,
                    "url": href,
                    "title": title,
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
            f"🏠 Valière Cortez\n"
            f"💰 {listing.get('price')}\n"
            f"📍 {listing.get('location')}\n"
            f"📐 {listing.get('details')}\n"
            f"🔗 {listing.get('url')}"
        )
        return msg
