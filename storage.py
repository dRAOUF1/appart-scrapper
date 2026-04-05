"""PostgreSQL storage for multi-user listing tracking.

This module provides a backward-compatible facade over the new repository layer.
All DB operations are delegated to dedicated repositories:
  - UserRepository
  - SearchRepository
  - ListingRepository
  - ScrapeLogRepository
  - AdminRepository
  - SettingsRepository

The Listing class is kept here for backward compatibility with parsers/scraper.
"""

from __future__ import annotations

import threading
from typing import Optional

from loguru import logger

from repositories.user_repo import UserRepository
from repositories.search_repo import SearchRepository
from repositories.listing_repo import ListingRepository
from repositories.scrape_log_repo import ScrapeLogRepository
from repositories.admin_repo import AdminRepository
from repositories.settings_repo import SettingsRepository

# Re-export Listing for backward compatibility with parsers/scraper
from storage_legacy import Listing  # noqa: F401 — kept for import compatibility


class Storage:
    """PostgreSQL storage facade — delegates to repositories.

    Maintains 100% backward compatibility with the old monolithic API
    while the actual work is done by specialized repositories.
    """

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._local = threading.local()
        self.users = UserRepository(database_url)
        self.searches = SearchRepository(database_url)
        self.listings = ListingRepository(database_url)
        self.scrape_logs = ScrapeLogRepository(database_url)
        self.admin = AdminRepository(database_url)
        self.settings = SettingsRepository(database_url)
        self._init_db()

    # ------------------------------------------------------------------
    # Connection management (shared by all repos via inheritance)
    # ------------------------------------------------------------------

    def _get_conn(self):
        return self.users._get_conn()

    def _get_conn_for_request(self):
        return self.users._get_conn_for_request()

    def _release_conn(self, conn):
        return self.users._release_conn(conn)

    def _get_ddl_conn(self):
        return self.users._get_ddl_conn()

    def _close_conn(self, conn):
        return self.users._close_conn(conn)

    def _init_db(self) -> None:
        """Verify tables exist. Never does DDL at runtime."""
        import time
        total_start = time.monotonic()

        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_name = 'users'
                    )
                """)
                tables_exist = cur.fetchone()[0]
            if not tables_exist:
                logger.error("Tables DB manquantes — exécutez les migrations manuellement")
                raise RuntimeError("Database tables not found. Run migrations first.")
        finally:
            self._release_conn(conn)

        elapsed = time.monotonic() - total_start
        logger.debug(f"DB init check: {elapsed:.3f}s (tables OK)")

    def _run_ddl_migrations(self) -> None:
        """Run DDL migrations — execute ONCE at first deployment."""
        import time
        total_start = time.monotonic()
        phase_start = total_start

        def _log_phase(name: str):
            elapsed = time.monotonic() - phase_start
            logger.debug(f"  DB phase '{name}': {elapsed:.2f}s")
            return time.monotonic()

        conn = self._get_ddl_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id          SERIAL PRIMARY KEY,
                        username    TEXT UNIQUE NOT NULL,
                        api_token   TEXT UNIQUE NOT NULL,
                        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS searches (
                        id              SERIAL PRIMARY KEY,
                        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        label           TEXT NOT NULL,
                        ntfy_topic      TEXT NOT NULL,
                        source          TEXT NOT NULL DEFAULT 'seloger',
                        criteria        JSONB DEFAULT '{}',
                        scrape_interval INTEGER DEFAULT 5,
                        last_scraped    TIMESTAMP,
                        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS listings (
                        listing_id      TEXT PRIMARY KEY,
                        url             TEXT NOT NULL,
                        title           TEXT,
                        price           TEXT,
                        surface         TEXT,
                        rooms           TEXT,
                        location        TEXT,
                        image_url       TEXT,
                        description     TEXT,
                        agency          TEXT,
                        source          TEXT DEFAULT '',
                        legacy_id       TEXT DEFAULT '',
                        price_value     FLOAT,
                        price_details   TEXT DEFAULT '',
                        city            TEXT DEFAULT '',
                        district        TEXT DEFAULT '',
                        zip_code        TEXT DEFAULT '',
                        property_type   TEXT DEFAULT '',
                        is_private      BOOLEAN DEFAULT FALSE,
                        phone           JSONB DEFAULT '[]',
                        epc             TEXT DEFAULT '',
                        ges             TEXT DEFAULT '',
                        is_new          BOOLEAN DEFAULT FALSE,
                        is_exclusive    BOOLEAN DEFAULT FALSE,
                        has_3d_visit    BOOLEAN DEFAULT FALSE,
                        creation_date   TEXT DEFAULT '',
                        update_date     TEXT DEFAULT '',
                        headline        TEXT DEFAULT '',
                        photos          JSONB DEFAULT '[]',
                        first_seen      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS search_listings (
                        search_id   INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
                        listing_id  TEXT NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
                        found_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (search_id, listing_id)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scrape_logs (
                        id              SERIAL PRIMARY KEY,
                        search_id       INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
                        started_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        completed_at    TIMESTAMP,
                        status          TEXT NOT NULL,
                        listings_found  INTEGER DEFAULT 0,
                        new_listings    INTEGER DEFAULT 0,
                        error_message   TEXT,
                        details         JSONB DEFAULT '{}',
                        duration_sec    FLOAT,
                        raw_logs        TEXT
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS admin_logs (
                        id            SERIAL PRIMARY KEY,
                        action        TEXT NOT NULL,
                        details       TEXT,
                        performed_by  TEXT,
                        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS app_settings (
                        key   TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                """)
                cur.execute("""
                    INSERT INTO app_settings (key, value) VALUES ('use_bff_api', 'true')
                    ON CONFLICT (key) DO NOTHING;
                """)
                phase_start = _log_phase("create_tables")

                conn.commit()

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_listings_first_seen
                        ON listings(first_seen);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_search_listings_search
                        ON search_listings(search_id);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_search_listings_found_at
                        ON search_listings(found_at DESC);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_search_listings_search_found
                        ON search_listings(search_id, found_at DESC);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_search_listings_listing
                        ON search_listings(listing_id);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_searches_user
                        ON searches(user_id);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_searches_user_active
                        ON searches(user_id, is_active);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_searches_source
                        ON searches(source);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_listings_source
                        ON listings(source);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_admin_logs_created
                        ON admin_logs(created_at);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_admin_logs_action
                        ON admin_logs(action);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_scrape_logs_search
                        ON scrape_logs(search_id);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_scrape_logs_started
                        ON scrape_logs(started_at DESC);
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_scrape_logs_status
                        ON scrape_logs(status);
                """)
                phase_start = _log_phase("indexes")

                cur.execute("""
                    ALTER TABLE listings
                        ADD COLUMN IF NOT EXISTS legacy_id TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS price_value FLOAT,
                        ADD COLUMN IF NOT EXISTS price_details TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS city TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS district TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS zip_code TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS property_type TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS is_private BOOLEAN DEFAULT FALSE,
                        ADD COLUMN IF NOT EXISTS phone JSONB DEFAULT '[]',
                        ADD COLUMN IF NOT EXISTS epc TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS ges TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS is_new BOOLEAN DEFAULT FALSE,
                        ADD COLUMN IF NOT EXISTS is_exclusive BOOLEAN DEFAULT FALSE,
                        ADD COLUMN IF NOT EXISTS has_3d_visit BOOLEAN DEFAULT FALSE,
                        ADD COLUMN IF NOT EXISTS creation_date TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS update_date TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS headline TEXT DEFAULT '',
                        ADD COLUMN IF NOT EXISTS photos JSONB DEFAULT '[]';
                """)
                phase_start = _log_phase("alter_listings")

                cur.execute("""
                    ALTER TABLE searches
                        ADD COLUMN IF NOT EXISTS criteria JSONB DEFAULT '{}',
                        ADD COLUMN IF NOT EXISTS scrape_interval INTEGER DEFAULT 5,
                        ADD COLUMN IF NOT EXISTS last_scraped TIMESTAMP,
                        ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
                """)
                cur.execute("""
                    ALTER TABLE scrape_logs
                        ADD COLUMN IF NOT EXISTS raw_logs TEXT;
                """)
                phase_start = _log_phase("alter_other")

                conn.commit()

            total_elapsed = time.monotonic() - total_start
            logger.info(f"Tables PostgreSQL initialisées en {total_elapsed:.2f}s")
        finally:
            self._release_conn(conn)

    # ------------------------------------------------------------------
    # Facade methods — delegate to repositories
    # ------------------------------------------------------------------

    # Users
    def create_user(self, username: str) -> dict:
        return self.users.create_user(username)

    def get_user_by_token(self, token: str) -> Optional[dict]:
        return self.users.get_user_by_token(token)

    def get_user_by_username(self, username: str) -> Optional[dict]:
        return self.users.get_user_by_username(username)

    def get_all_users(self) -> list[dict]:
        return self.users.get_all_users()

    def get_user_detail(self, user_id: int) -> Optional[dict]:
        return self.users.get_user_detail(user_id)

    def delete_user(self, user_id: int) -> bool:
        return self.users.delete_user(user_id)

    def reset_user_token(self, user_id: int) -> str:
        return self.users.reset_user_token(user_id)

    def get_user_stats(self, user_id: int) -> dict:
        return self.users.get_user_stats(user_id)

    def get_dashboard_data(self, user_id: int) -> dict:
        return self.users.get_dashboard_data(user_id)

    # Searches
    def create_search(self, user_id: int, label: str, ntfy_topic: str, source: str = "seloger", criteria: dict | None = None, scrape_interval: int = 5, is_active: bool = True) -> dict:
        return self.searches.create_search(user_id, label, ntfy_topic, source, criteria, scrape_interval, is_active)

    def update_search_criteria(self, search_id: int, criteria: dict) -> bool:
        return self.searches.update_search_criteria(search_id, criteria)

    def update_search(self, search_id: int, user_id: int, label: str | None = None, ntfy_topic: str | None = None, criteria: dict | None = None, scrape_interval: int | None = None, is_active: bool | None = None) -> bool:
        return self.searches.update_search(search_id, user_id, label, ntfy_topic, criteria, scrape_interval, is_active)

    def update_scrape_interval(self, search_id: int, interval_minutes: int) -> bool:
        return self.searches.update_scrape_interval(search_id, interval_minutes)

    def update_last_scraped(self, search_id: int) -> bool:
        return self.searches.update_last_scraped(search_id)

    def get_user_searches(self, user_id: int) -> list[dict]:
        return self.searches.get_user_searches(user_id)

    def get_search(self, search_id: int) -> Optional[dict]:
        return self.searches.get_search(search_id)

    def delete_search(self, search_id: int) -> bool:
        return self.searches.delete_search(search_id)

    def toggle_search_active(self, search_id: int) -> bool | None:
        return self.searches.toggle_search_active(search_id)

    def get_all_searches(self, user_filter="", source_filter="") -> list[dict]:
        return self.searches.get_all_searches(user_filter, source_filter)

    def get_search_detail(self, search_id: int) -> Optional[dict]:
        return self.searches.get_search_detail(search_id)

    # Listings
    def save_listing(self, listing) -> bool:
        return self.listings.save_listing(listing)

    def link_listing_to_search(self, search_id: int, listing_id: str) -> bool:
        return self.listings.link_listing_to_search(search_id, listing_id)

    def save_and_link(self, listings: list, search_id: int) -> tuple:
        return self.listings.save_and_link(listings, search_id)

    def get_listings_for_search(self, search_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
        return self.listings.get_listings_for_search(search_id, limit, offset)

    def count_listings_for_search(self, search_id: int) -> int:
        return self.listings.count_listings_for_search(search_id)

    def delete_old_listings(self, days: int = 4) -> int:
        return self.listings.delete_old_listings(days)

    def delete_listing(self, listing_id: str) -> bool:
        return self.listings.delete_listing(listing_id)

    def get_orphan_listings_count(self) -> int:
        return self.listings.get_orphan_listings_count()

    def delete_orphan_listings(self) -> int:
        return self.listings.delete_orphan_listings()

    def get_all_listings(self, limit=50, offset=0, search_term="", source_filter="") -> list[dict]:
        return self.listings.get_all_listings(limit, offset, search_term, source_filter)

    def count_all_listings(self, search_term="", source_filter="") -> int:
        return self.listings.count_all_listings(search_term, source_filter)

    def get_listing_detail(self, listing_id: str) -> Optional[dict]:
        return self.listings.get_listing_detail(listing_id)

    # Scrape Logs
    def create_scrape_log(self, search_id: int, status: str, listings_found: int = 0, new_listings: int = 0, error_message: str = "", details: dict | None = None, started_at=None) -> int:
        return self.scrape_logs.create_scrape_log(search_id, status, listings_found, new_listings, error_message, details, started_at)

    def get_scrape_logs(self, search_id: int, limit: int = 50, offset: int = 0, status_filter: str = "") -> list[dict]:
        return self.scrape_logs.get_scrape_logs(search_id, limit, offset, status_filter)

    def count_scrape_logs(self, search_id: int, status_filter: str = "") -> int:
        return self.scrape_logs.count_scrape_logs(search_id, status_filter)

    def get_scrape_stats(self, search_id: int) -> dict:
        return self.scrape_logs.get_scrape_stats(search_id)

    def update_scrape_log_raw(self, log_id: int, raw_logs: str) -> bool:
        return self.scrape_logs.update_scrape_log_raw(log_id, raw_logs)

    def get_scrape_log_raw(self, log_id: int, user_id: int | None = None) -> dict | None:
        return self.scrape_logs.get_scrape_log_raw(log_id, user_id)

    def get_latest_scrape_log_id(self, search_id: int) -> int | None:
        return self.scrape_logs.get_latest_scrape_log_id(search_id)

    # Settings
    def get_setting(self, key: str, default: str = "") -> str:
        return self.settings.get_setting(key, default)

    def set_setting(self, key: str, value: str) -> bool:
        return self.settings.set_setting(key, value)

    # Admin
    def get_admin_stats(self) -> dict:
        return self.admin.get_admin_stats()

    def get_enhanced_admin_stats(self) -> dict:
        return self.admin.get_enhanced_admin_stats()

    def log_admin_action(self, action: str, details: str = "", performed_by: str = "") -> None:
        self.admin.log_admin_action(action, details, performed_by)

    def get_admin_logs(self, limit=50, offset=0, action_filter="", date_from="", date_to="") -> list[dict]:
        return self.admin.get_admin_logs(limit, offset, action_filter, date_from, date_to)

    def count_admin_logs(self, action_filter="", date_from="", date_to="") -> int:
        return self.admin.count_admin_logs(action_filter, date_from, date_to)

    def purge_old_logs(self, days: int = 30) -> int:
        return self.admin.purge_old_logs(days)

    def get_db_stats(self) -> dict:
        return self.admin.get_db_stats()

    def get_table_details(self, table_name: str) -> dict:
        return self.admin.get_table_details(table_name)

    def execute_query(self, sql: str) -> tuple:
        return self.admin.execute_query(sql)

    def get_active_connections(self) -> list[dict]:
        return self.admin.get_active_connections()

    def truncate_table(self, table_name: str) -> bool:
        return self.admin.truncate_table(table_name)

    def close(self) -> None:
        pass
