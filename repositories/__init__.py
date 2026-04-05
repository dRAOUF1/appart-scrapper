"""Database repositories — one per entity."""

from repositories.base import BaseRepository
from repositories.user_repo import UserRepository
from repositories.search_repo import SearchRepository
from repositories.listing_repo import ListingRepository
from repositories.scrape_log_repo import ScrapeLogRepository
from repositories.admin_repo import AdminRepository
from repositories.settings_repo import SettingsRepository

__all__ = [
    "BaseRepository",
    "UserRepository",
    "SearchRepository",
    "ListingRepository",
    "ScrapeLogRepository",
    "AdminRepository",
    "SettingsRepository",
]
