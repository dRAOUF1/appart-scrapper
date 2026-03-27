"""SeLoger.com scraper using Selenium + undetected-chromedriver."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from typing import Optional

import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from loguru import logger
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from storage import Listing


def _generate_listing_id(url: str, title: str = "") -> str:
    """Generate a unique ID for a listing based on its URL."""
    # Extract the listing ID from the URL if possible
    # SeLoger URLs typically contain a numeric ID
    match = re.search(r'/(\d{5,})\.htm', url)
    if match:
        return f"sl_{match.group(1)}"

    # Fallback: hash the URL
    return f"sl_{hashlib.md5(url.encode()).hexdigest()[:12]}"


def _find_chrome_binary() -> str | None:
    """
    Find the Chrome/Chromium binary path.
    Checks environment variables first (CHROME_BIN, GOOGLE_CHROME_BIN),
    then common install locations (useful for Render, Docker, etc.).
    """
    # 1. Check environment variables
    for env_var in ("CHROME_BIN", "GOOGLE_CHROME_BIN", "CHROMIUM_BIN"):
        path = os.environ.get(env_var)
        if path and os.path.isfile(path):
            logger.info(f"Chrome trouvé via ${env_var}: {path}")
            return path

    # 2. Check common binary names in PATH
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        path = shutil.which(name)
        if path:
            logger.info(f"Chrome trouvé dans PATH: {path}")
            return path

    # 3. Check common absolute paths (Render, Docker, etc.)
    common_paths = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/lib/chromium/chromium",
        "/opt/google/chrome/chrome",
        "/opt/google/chrome/google-chrome",
        "/opt/render/project/.render/chrome/opt/google/chrome/google-chrome",
    ]
    for path in common_paths:
        if os.path.isfile(path):
            logger.info(f"Chrome trouvé: {path}")
            return path

    logger.warning("Aucun binaire Chrome/Chromium trouvé")
    return None


class SeLogerScraper:
    """Scrape listings from SeLoger.com search result pages."""

    # CSS selectors based on SeLoger's actual data-testid attributes.
    # Primary selectors use data-testid, fallbacks use class patterns.
    SELECTORS = {
        # Main listing card containers
        "cards": [
            "[data-testid^='classified-card-mfe']",
        ],
        # Price element within a card
        "price": [
            "[data-testid='cardmfe-price-testid']",
        ],
        # Title — we extract from the covering link's title attribute
        "title": [
            "[data-testid='cardmfe-description-box-text-test-id']",
        ],
        # Location
        "location": [
            "[data-testid='cardmfe-description-box-address']",
        ],
        # Key facts (rooms, surface, floor)
        "keyfacts": [
            "[data-testid='cardmfe-keyfacts-testid']",
        ],
        # Link to listing detail
        "link": [
            "a[data-testid='card-mfe-covering-link-testid']",
            "a[href*='/annonces/']",
        ],
        # Image
        "image": [
            "img",
        ],
        # Agency / advertiser
        "agency": [
            "[data-testid='cardmfe-card-bottom-strip-test-id']",
        ],
        # Description text
        "description": [
            "[data-testid='cardmfe-description-text-test-id']",
        ],
        # Cookie consent button
        "cookie_accept": [
            "button[id*='accept']",
            "button[class*='accept']",
            "#didomi-notice-agree-button",
            "button[aria-label*='accepter']",
            "button[aria-label*='Accept']",
        ],
    }

    def __init__(
        self,
        headless: bool = True,
        page_load_timeout: int = 30,
        action_delay: float = 2.0,
    ):
        self.headless = headless
        self.page_load_timeout = page_load_timeout
        self.action_delay = action_delay
        self.driver: Optional[uc.Chrome] = None

    def _get_chrome_version(self) -> int | None:
        """Auto-detect the installed Chrome major version."""
        import subprocess
        for cmd in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]:
            try:
                result = subprocess.run(
                    [cmd, "--version"], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    match = re.search(r"(\d+)\.", result.stdout)
                    if match:
                        version = int(match.group(1))
                        logger.debug(f"Chrome version détectée : {version}")
                        return version
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        return None

    def _create_driver(self) -> uc.Chrome:
        """Create a new undetected Chrome driver instance."""
        options = uc.ChromeOptions()

        if self.headless:
            options.add_argument("--headless=new")

        # Performance & stealth options
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--single-process")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=fr-FR")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )

        # Reduce resource usage
        prefs = {
            "profile.managed_default_content_settings.images": 1,
            "profile.default_content_setting_values.notifications": 2,
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        }
        options.add_experimental_option("prefs", prefs)

        # --- Locate Chrome binary (critical for Render / Docker) ---
        chrome_binary = _find_chrome_binary()
        if chrome_binary:
            options.binary_location = chrome_binary

        # --- Locate chromedriver (skip UC's auto-download which hangs in Docker) ---
        chromedriver_path = (
            os.environ.get("CHROMEDRIVER_PATH")
            or shutil.which("chromedriver")
            or shutil.which("chromium-driver")
        )
        # Check common Docker/Render paths
        if not chromedriver_path:
            for p in ["/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver"]:
                if os.path.isfile(p):
                    chromedriver_path = p
                    break

        if chromedriver_path:
            logger.info(f"Chromedriver trouvé: {chromedriver_path}")
        else:
            logger.warning("Chromedriver introuvable — UC va tenter de le télécharger")

        # Auto-detect Chrome version to avoid mismatch
        chrome_version = self._get_chrome_version()
        logger.info(f"Utilisation de Chrome version {chrome_version or 'auto'}")

        try:
            import concurrent.futures

            logger.info("Tentative de lancement avec undetected-chromedriver (timeout 20s)...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    uc.Chrome,
                    options=options,
                    version_main=chrome_version,
                    driver_executable_path=chromedriver_path,
                    browser_executable_path=chrome_binary,
                    no_sandbox=True,
                )
                driver = future.result(timeout=20)

        except Exception as e:
            if isinstance(e, concurrent.futures.TimeoutError):
                logger.warning("undetected-chromedriver a timeout après 20 secondes.")
            else:
                logger.warning(f"undetected-chromedriver a échoué: {e}")
            
            logger.info("Fallback vers Selenium standard...")
            from selenium import webdriver
            from selenium.webdriver.chrome.service import Service

            service = Service(executable_path=chromedriver_path) if chromedriver_path else Service()
            driver = webdriver.Chrome(options=options, service=service)

        driver.set_page_load_timeout(self.page_load_timeout)
        return driver

    def _ensure_driver(self) -> uc.Chrome:
        """Get or create the Chrome driver."""
        if self.driver is None:
            logger.info("Lancement du navigateur Chrome...")
            self.driver = self._create_driver()
        return self.driver

    def _find_element_multi(self, parent, selectors: list[str], attr: str = "text") -> str:
        """Try multiple CSS selectors and return the first match's text or attribute."""
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

    def _handle_cookie_consent(self, driver: uc.Chrome) -> None:
        """Try to accept cookie consent dialogs."""
        time.sleep(2)
        for selector in self.SELECTORS["cookie_accept"]:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, selector)
                if btn.is_displayed():
                    btn.click()
                    logger.debug("Cookie consent accepté")
                    time.sleep(1)
                    return
            except (NoSuchElementException, WebDriverException):
                continue

    def _scroll_page(self, driver: uc.Chrome) -> None:
        """Scroll down the page to trigger lazy loading."""
        last_height = driver.execute_script("return document.body.scrollHeight")
        scroll_attempts = 0
        max_scrolls = 5

        while scroll_attempts < max_scrolls:
            # Scroll down progressively
            driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
            time.sleep(self.action_delay)

            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height
            scroll_attempts += 1

        # Scroll back to top
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)

    def _extract_listings_from_html(self, html: str, search_url: str) -> list[Listing]:
        """Parse the HTML and extract listing data."""
        soup = BeautifulSoup(html, "lxml")
        listings = []

        # Try each card selector strategy
        cards = []
        for selector in self.SELECTORS["cards"]:
            cards = soup.select(selector)
            if cards:
                logger.debug(f"Trouvé {len(cards)} cartes avec le sélecteur: {selector}")
                break

        if not cards:
            # Fallback: try to find links that look like listing URLs
            logger.debug("Aucun sélecteur de carte n'a fonctionné, recherche par liens...")
            links = soup.find_all("a", href=re.compile(r"seloger\.com/annonces/"))
            for link in links:
                url = link.get("href", "")
                if url and not url.startswith("http"):
                    url = f"https://www.seloger.com{url}"

                listing_id = _generate_listing_id(url)
                title = link.get_text(strip=True) or ""

                listings.append(Listing(
                    listing_id=listing_id,
                    url=url,
                    title=title[:200],
                ))
            return listings

        for card in cards:
            try:
                # Extract the link — it has the URL and a title attribute with summary
                link_el = card.select_one("a[data-testid='card-mfe-covering-link-testid']")
                if not link_el:
                    link_el = card.select_one("a[href*='/annonces/']")
                if not link_el:
                    continue

                link_url = link_el.get("href", "")
                if link_url and not link_url.startswith("http"):
                    link_url = f"https://www.seloger.com{link_url}"

                if not link_url or "seloger.com" not in link_url:
                    continue

                listing_id = _generate_listing_id(link_url)

                # Extract data from card using data-testid selectors
                price = self._find_element_multi(card, self.SELECTORS["price"])
                location = self._find_element_multi(card, self.SELECTORS["location"])
                keyfacts = self._find_element_multi(card, self.SELECTORS["keyfacts"])
                agency = self._find_element_multi(card, self.SELECTORS["agency"])
                description = self._find_element_multi(card, self.SELECTORS["description"])
                image_url = self._find_element_multi(card, self.SELECTORS["image"], attr="src")

                # Parse keyfacts: "1 pièce·19 m²·1er étage"
                surface = ""
                rooms = ""
                if keyfacts:
                    parts = [p.strip() for p in keyfacts.replace("·", "|").split("|")]
                    for part in parts:
                        if "m²" in part or "m2" in part.lower():
                            surface = part
                        elif "pièce" in part.lower() or "piece" in part.lower():
                            rooms = part

                # Title: use covering link's title attribute as it has a clean summary
                # e.g. "Appartement à louer - Paris 13ème - 722 € - 1 pièce, 19 m²"
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
                ))

            except Exception as e:
                logger.debug(f"Erreur lors de l'extraction d'une carte : {e}")
                continue

        return listings

    def _extract_from_script_data(self, html: str) -> list[Listing]:
        """
        Try to extract listing data from embedded JSON in script tags.
        SeLoger often embeds data in window["initialData"] or __NEXT_DATA__.
        """
        listings = []
        soup = BeautifulSoup(html, "lxml")

        for script in soup.find_all("script"):
            script_text = script.string or ""

            # Look for JSON data patterns
            for pattern in [
                r'window\["initialData"\]\s*=\s*(\{.*?\});',
                r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});',
                r'__NEXT_DATA__.*?(\{.*?"props".*?\})',
            ]:
                match = re.search(pattern, script_text, re.DOTALL)
                if match:
                    try:
                        import json
                        data = json.loads(match.group(1))
                        # Navigate the JSON to find listings
                        listings.extend(self._parse_json_listings(data))
                        if listings:
                            logger.info(f"Extraites {len(listings)} annonces depuis les données JSON embarquées")
                            return listings
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.debug(f"Échec du parsing JSON embarqué : {e}")

        return listings

    def _parse_json_listings(self, data: dict, depth: int = 0) -> list[Listing]:
        """Recursively search for listing data in a JSON structure."""
        listings = []
        if depth > 10:
            return listings

        if isinstance(data, dict):
            # Check if this dict looks like a listing
            has_price = any(k in data for k in ["price", "prix", "pricing"])
            has_id = any(k in data for k in ["id", "listingId", "classifiedId"])

            if has_price and has_id:
                listing_id = str(
                    data.get("id") or data.get("listingId") or
                    data.get("classifiedId", "")
                )
                if listing_id:
                    # Extract pricing
                    price_data = data.get("price") or data.get("pricing", {})
                    if isinstance(price_data, dict):
                        price = price_data.get("price", price_data.get("value", ""))
                        price = f"{price} €" if price else ""
                    else:
                        price = str(price_data) if price_data else ""

                    url = data.get("url") or data.get("permalink") or data.get("classifiedURL", "")
                    if url and not url.startswith("http"):
                        url = f"https://www.seloger.com{url}"

                    # Extract agency info
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
                        image_url=str(data.get("photos", [""])[0] if data.get("photos") else ""),
                        agency=str(agency),
                    ))

            # Recurse into values
            for value in data.values():
                if isinstance(value, (dict, list)):
                    listings.extend(self._parse_json_listings(value, depth + 1))

        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    listings.extend(self._parse_json_listings(item, depth + 1))

        return listings

    def scrape(self, search_url: str, max_pages: int = 1) -> list[Listing]:
        """
        Scrape listings from a SeLoger search URL.

        Args:
            search_url: The SeLoger search results URL
            max_pages: Maximum number of pages to scrape

        Returns:
            List of extracted Listing objects
        """
        driver = self._ensure_driver()
        all_listings: list[Listing] = []
        seen_ids: set[str] = set()

        for page_num in range(1, max_pages + 1):
            # Build the page URL
            if page_num == 1:
                page_url = search_url
            else:
                # SeLoger uses LISTING-LISTpg=N or &pg=N for pagination
                if "pg=" in search_url:
                    page_url = re.sub(r'pg=\d+', f'pg={page_num}', search_url)
                elif "?" in search_url:
                    page_url = f"{search_url}&pg={page_num}"
                else:
                    page_url = f"{search_url}?pg={page_num}"

            logger.info(f"Scraping page {page_num}: {page_url[:80]}...")

            try:
                driver.get(page_url)
                time.sleep(self.action_delay + 1)

                # Handle cookie consent on first page
                if page_num == 1:
                    self._handle_cookie_consent(driver)

                # Wait for content to load
                try:
                    WebDriverWait(driver, self.page_load_timeout).until(
                        lambda d: d.execute_script("return document.readyState") == "complete"
                    )
                except TimeoutException:
                    logger.warning("Timeout en attendant le chargement de la page")

                # Additional wait for dynamic content
                time.sleep(self.action_delay)

                # Scroll to load lazy content
                self._scroll_page(driver)

                # Get the rendered HTML
                html = driver.page_source

                # Strategy 1: Try embedded JSON data (most reliable)
                page_listings = self._extract_from_script_data(html)

                # Strategy 2: Parse HTML elements
                if not page_listings:
                    page_listings = self._extract_listings_from_html(html, search_url)

                # Deduplicate within this scrape session
                for listing in page_listings:
                    if listing.listing_id not in seen_ids:
                        seen_ids.add(listing.listing_id)
                        all_listings.append(listing)

                logger.info(
                    f"Page {page_num}: {len(page_listings)} annonces trouvées "
                    f"({len(all_listings)} total unique)"
                )

                if not page_listings:
                    logger.info("Aucune annonce trouvée sur cette page, arrêt de la pagination")
                    break

                # Delay between pages
                if page_num < max_pages:
                    time.sleep(self.action_delay * 2)

            except TimeoutException:
                logger.error(f"Timeout lors du chargement de la page {page_num}")
                break
            except WebDriverException as e:
                logger.error(f"Erreur navigateur page {page_num}: {e}")
                break
            except Exception as e:
                logger.error(f"Erreur inattendue page {page_num}: {e}")
                break

        logger.info(f"Scraping terminé: {len(all_listings)} annonces uniques extraites")
        return all_listings

    def close(self) -> None:
        """Close the browser."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            logger.debug("Navigateur fermé")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
