from abc import ABC, abstractmethod
from typing import List, Dict, Any, Tuple
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
    
    @abstractmethod
    def scrape_with_curl(self, session, config: dict) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Scrape using curl_cffi session.
        
        Args:
            session: curl_cffi Session object with impersonation
            config: Full application config dict
            
        Returns:
            Tuple of (listings, is_blocked)
        """
        pass
