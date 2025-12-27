from abc import ABC, abstractmethod
from typing import List, Dict, Any
import logging

logger = logging.getLogger(__name__)

class BaseScraper(ABC):
    def __init__(self, config: Dict[str, Any]):
        self.config = config
    
    @abstractmethod
    def get_name(self) -> str:
        """Returns the unique name of the scraper."""
        pass

    @abstractmethod
    def get_start_url(self) -> str:
        """Returns the starting URL for the scraper."""
        pass

    @abstractmethod
    def parse_listings(self, page_content: str) -> List[Dict[str, Any]]:
        """Parses the HTML content and returns a list of listing dictionaries."""
        pass

    @abstractmethod
    def get_listing_id(self, listing: Dict[str, Any]) -> str:
        """Extracts a unique ID from a listing dictionary."""
        pass

    @abstractmethod
    def format_notification(self, listing: Dict[str, Any]) -> str:
        """Formats a listing into a notification message."""
        pass
    
    def scrape(self, page, stealth_mgr=None) -> List[Dict[str, Any]]:
        """
        Main method to execute the scraping logic using the provided Playwright Page object.
        Can be overridden if custom navigation/logic is needed (e.g. handling pagination).
        """
        url = self.get_start_url()
        logger.info(f"[{self.get_name()}] Navigating to {url}")
        
        # Navigation is usually handled here or in main, but let's handle it here so scraper controls it
        # Note: 'page' comes from main.py which handles the Stealth wrapper
        response = page.goto(url, wait_until="domcontentloaded")
        
        # Add basic wait logic if needed, e.g. for selectors
        # page.wait_for_selector('config_selector') 
        
        # Get content
        content = page.content()
        
        # Parse
        listings = self.parse_listings(content)
        logger.info(f"[{self.get_name()}] Found {len(listings)} listings")
        
        return listings
