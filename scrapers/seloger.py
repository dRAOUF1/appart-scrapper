from typing import List, Dict, Any
from .base import BaseScraper
from bs4 import BeautifulSoup
import logging
import time
import random

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

    def scrape(self, page, stealth_mgr=None) -> List[Dict[str, Any]]:
        """Legacy Playwright scrape method."""
        return []

    def scrape_with_uc(self, driver, config: dict) -> tuple[List[Dict[str, Any]], bool]:
        """Scrape using undetected-chromedriver. Returns (listings, is_blocked)."""
        url = self.get_start_url()
        logger.info(f"[{self.get_name()}] Navigating to {url}")
        is_blocked = False
        
        try:
            # First visit homepage to establish session
            logger.info(f"[{self.get_name()}] Visiting homepage first...")
            driver.get("https://www.seloger.com/")
            
            # Wait for page to load
            time.sleep(random.uniform(3, 6))
            
            # Simulate human behavior
            self._simulate_human(driver)
            
            # Handle cookie consent
            self._handle_cookies(driver)
            
            # Small delay
            time.sleep(random.uniform(2, 4))
            
            # Navigate to search URL
            logger.info(f"[{self.get_name()}] Navigating to search page...")
            driver.get(url)
            
            # Wait for content
            time.sleep(random.uniform(4, 7))
            
            # Simulate scrolling
            self._simulate_human(driver)
            
            # Get page content
            content = driver.page_source
            
            # Debug: save HTML
            with open("debug_seloger.html", "w", encoding="utf-8") as f:
                f.write(content)
            logger.debug("Saved page to debug_seloger.html")
            
            # First try to parse - if we have listings, we're not blocked
            listings = self.parse_listings(content)
            
            # Only check for blocking if we found no listings
            if len(listings) == 0:
                # Check for actual blocking indicators (not just datadome scripts on normal pages)
                blocking_indicators = [
                    # CAPTCHA page with JS requirement
                    ("captcha" in content.lower() and "enable JS" in content and len(content) < 5000),
                    # DataDome interstitial (small page with just captcha)
                    ("captcha-delivery.com" in content and "cardmfe" not in content),
                    # Bot detection message
                    ("robot" in content.lower() and "detected" in content.lower() and "cardmfe" not in content),
                ]
                
                if any(blocking_indicators):
                    is_blocked = True
                    logger.warning(f"[{self.get_name()}] BLOCKED - Bot detection triggered!")
                    try:
                        driver.save_screenshot("debug_seloger.png")
                        logger.info("Debug screenshot saved.")
                    except:
                        pass
                    return [], True
            
            logger.info(f"[{self.get_name()}] Found {len(listings)} listings")
            return listings, False
            
        except Exception as e:
            logger.error(f"[{self.get_name()}] Error: {e}", exc_info=True)
            return [], False

    def _simulate_human(self, driver):
        """Simulate human scrolling."""
        try:
            for _ in range(random.randint(2, 4)):
                scroll = random.randint(200, 500)
                driver.execute_script(f"window.scrollBy(0, {scroll})")
                time.sleep(random.uniform(0.3, 0.8))
        except:
            pass

    def _handle_cookies(self, driver):
        """Handle cookie consent."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        
        selectors = [
            (By.ID, "didomi-notice-agree-button"),
            (By.XPATH, "//button[contains(text(), 'Accepter')]"),
            (By.XPATH, "//button[contains(text(), 'Tout accepter')]"),
        ]
        
        for by, sel in selectors:
            try:
                elem = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((by, sel))
                )
                elem.click()
                logger.info(f"[{self.get_name()}] Clicked consent: {sel}")
                time.sleep(random.uniform(0.5, 1.5))
                return
            except:
                continue

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
                    import re
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
                
                # Agency name - it's in the img alt attribute, not in text
                agency = ""
                # First try the text-based title
                agency_node = card.select_one('[data-testid="cardmfe-agency-title-test-id"]')
                if agency_node:
                    agency = agency_node.get_text(strip=True)
                
                # If no text, try to get from image alt attribute
                if not agency:
                    agency_container = card.select_one('[data-testid*="cardmfe-agency-publisher"]')
                    if agency_container:
                        img = agency_container.select_one('img[alt]')
                        if img:
                            agency = img.get('alt', '')
                
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
        msg = (
            f"🏠 Nouveau Logement SeLoger!\n"
            f"💰 Prix: {listing.get('price')}\n"
            f"📍 Lieu: {listing.get('location')}\n"
        )
        if listing.get('details'):
            msg += f"📐 Détails: {listing.get('details')}\n"
        if listing.get('agency'):
            msg += f"🏢 Agence: {listing.get('agency')}\n"
        msg += f"🔗 {listing.get('url')}"
        return msg
