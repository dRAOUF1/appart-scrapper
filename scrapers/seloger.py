from typing import List, Dict, Any, Tuple
from .base import BaseScraper
from bs4 import BeautifulSoup
import logging
import re

logger = logging.getLogger(__name__)


class SeLogerScraper(BaseScraper):
    def get_name(self) -> str:
        return "seloger"

    def get_start_url(self) -> str:
        if 'url' in self.config and 'filters' not in self.config:
            return self.config['url']
            
        base_url = self.config.get('base_url', "https://www.seloger.com/classified-search")
        filters = self.config.get('filters', {})
        
        params = []
        params.append("distributionTypes=Rent")
        params.append("estateTypes=House,Apartment")
        params.append("locationsInBuildingExcluded=Groundfloor")
        
        if 'locations' in filters:
            params.append(f"locations={filters['locations']}")
        if 'price_min' in filters:
            params.append(f"priceMin={filters['price_min']}")
        if 'price_max' in filters:
            params.append(f"priceMax={filters['price_max']}")
        if 'surface_min' in filters:
            params.append(f"spaceMin={filters['surface_min']}")
            
        sort_order = filters.get('sort_order', "DateDesc")
        params.append(f"order={sort_order}")
        
        return f"{base_url}?{'&'.join(params)}"

    def scrape_with_curl(self, session, config: dict) -> Tuple[List[Dict[str, Any]], bool]:
        """Scrape SeLoger using curl_cffi session."""
        url = self.get_start_url()
        logger.info(f"[{self.get_name()}] Fetching {url}")
        
        headers = {
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "fr-FR,fr;q=0.9",
        }
        
        try:
            response = session.get(url, headers=headers, timeout=30)
            
            # Debug: save response
            with open("debug_seloger.html", "w", encoding="utf-8") as f:
                f.write(response.text)
            logger.debug("Saved response to debug_seloger.html")
            
            # Check for blocking
            if response.status_code != 200:
                logger.warning(f"[{self.get_name()}] HTTP {response.status_code}")
                return [], True
            
            content = response.text
            
            # Check for CAPTCHA/blocking indicators
            blocking_indicators = [
                ("captcha" in content.lower() and "enable JS" in content and len(content) < 5000),
                ("captcha-delivery.com" in content and "cardmfe" not in content),
                ("robot" in content.lower() and "detected" in content.lower() and "cardmfe" not in content),
            ]
            
            if any(blocking_indicators):
                logger.warning(f"[{self.get_name()}] BLOCKED - Bot detection triggered!")
                return [], True
            
            # Parse listings
            listings = self.parse_listings(content)
            logger.info(f"[{self.get_name()}] Found {len(listings)} listings")
            
            return listings, False
            
        except Exception as e:
            logger.error(f"[{self.get_name()}] Error: {e}", exc_info=True)
            return [], False

    def parse_listings(self, page_content: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(page_content, 'html.parser')
        listings = []
        
        # Check blocking
        if "captcha" in page_content.lower() and "Please enable JS" in page_content:
            logger.warning(f"[{self.get_name()}] CAPTCHA detected")
            return []
        
        # New SeLoger selectors based on data-testid attributes
        cards = soup.select('[data-testid="cardmfe-container--test-id"]')
        if not cards:
            cards = soup.select('[data-testid="serp-core-classified-card-testid"]')
        if not cards:
            # Fallback to old selectors
            cards = soup.find_all("div", attrs={"data-test": "sl.card-container"})
        if not cards:
            cards = soup.select('div[class*="Card__Content"]')
        
        logger.info(f"[{self.get_name()}] Parsed {len(cards)} cards")

        for i, card in enumerate(cards):
            try:
                # ID - from link or generate
                link_node = card.select_one('[data-testid="card-mfe-covering-link-testid"]')
                if not link_node:
                    link_node = card.select_one('a[href*="/annonces/"]')
                
                url = link_node.get('href', '') if link_node else ''
                if url and not url.startswith('http'):
                    url = "https://www.seloger.com" + url
                
                # Extract ID from URL (format: .../257128377.htm)
                listing_id = f"gen_{i}"
                if url:
                    # Match pattern like /257128377.htm
                    match = re.search(r'/(\d{6,12})\.htm', url)
                    if match:
                        listing_id = match.group(1)
                
                # Price
                price = "N/A"
                price_node = card.select_one('[data-testid="cardmfe-price-testid"]')
                if not price_node:
                    price_node = card.select_one('[data-test="sl.price-label"]')
                if price_node:
                    price = price_node.get_text(strip=True)
                
                # Address/Location
                location = "Unknown"
                loc_node = card.select_one('[data-testid="cardmfe-description-box-address"]')
                if not loc_node:
                    loc_node = card.select_one('[data-test="sl.address"]')
                if loc_node:
                    location = loc_node.get_text(strip=True)
                
                # Key facts (surface, rooms, etc.)
                keyfacts = ""
                keyfacts_node = card.select_one('[data-testid="cardmfe-keyfacts-testid"]')
                if keyfacts_node:
                    keyfacts = keyfacts_node.get_text(strip=True)
                
                # Agency name - multiple locations possible
                agency = ""
                
                # 1. Try text-based title (e.g. "Particulier")
                agency_title = card.select_one('[data-testid="cardmfe-agency-title-test-id"]')
                if agency_title:
                    agency = agency_title.get_text(strip=True)
                
                # 2. Try span inside publisher container (XL cards)
                if not agency:
                    for container_type in ['xl', 'large', 'medium']:
                        container = card.select_one(f'[data-testid="cardmfe-agency-publisher-{container_type}-test-id"]')
                        if container:
                            # Check for span text first
                            span = container.select_one('span')
                            if span:
                                text = span.get_text(strip=True)
                                if text:
                                    agency = text
                                    break
                            # Then check img alt
                            img = container.select_one('img[alt]')
                            if img:
                                agency = img.get('alt', '')
                                break
                
                listings.append({
                    "id": listing_id,
                    "price": price,
                    "location": location,
                    "url": url,
                    "title": f"Logement à {location}",
                    "details": keyfacts,
                    "agency": agency
                })
            except Exception as e:
                logger.warning(f"Parse error: {e}")
                continue
                
        return listings

    def get_listing_id(self, listing: Dict[str, Any]) -> str:
        return listing.get("id")

    def format_notification(self, listing: Dict[str, Any]) -> str:
        agency = listing.get('agency', '')
        msg = (
            f"🏠 SeLoger\n"
            f"💰 {listing.get('price')}\n"
            f"📍 {listing.get('location')}\n"
        )
        if agency:
            msg += f"🏢 {agency}\n"
        msg += f"🔗 {listing.get('url')}"
        return msg
