"""SQLite storage for tracking seen listings."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger


class Listing:
    """Represents a single real estate listing."""

    def __init__(
        self,
        listing_id: str,
        url: str,
        title: str = "",
        price: str = "",
        surface: str = "",
        rooms: str = "",
        location: str = "",
        image_url: str = "",
        description: str = "",
        agency: str = "",
    ):
        self.listing_id = listing_id
        self.url = url
        self.title = title
        self.price = price
        self.surface = surface
        self.rooms = rooms
        self.location = location
        self.image_url = image_url
        self.description = description
        self.agency = agency

    def __repr__(self) -> str:
        return f"Listing({self.listing_id}, {self.title}, {self.price})"

    def to_dict(self) -> dict:
        return {
            "listing_id": self.listing_id,
            "url": self.url,
            "title": self.title,
            "price": self.price,
            "surface": self.surface,
            "rooms": self.rooms,
            "location": self.location,
            "image_url": self.image_url,
            "description": self.description,
            "agency": self.agency,
        }


class Storage:
    """SQLite-based storage for persisting seen listings."""

    def __init__(self, db_path: str = "listings.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self) -> None:
        """Create the database and tables if they don't exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                listing_id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT,
                price TEXT,
                surface TEXT,
                rooms TEXT,
                location TEXT,
                image_url TEXT,
                description TEXT,
                agency TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                search_url TEXT
            )
        """)
        # Add agency column if upgrading from old schema
        try:
            self._conn.execute("ALTER TABLE listings ADD COLUMN agency TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # Column already exists
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_first_seen
            ON listings(first_seen)
        """)
        self._conn.commit()
        logger.debug(f"Base de données initialisée : {self.db_path}")

    def is_new(self, listing_id: str) -> bool:
        """Check if a listing has never been seen before."""
        cursor = self._conn.execute(
            "SELECT 1 FROM listings WHERE listing_id = ?",
            (listing_id,),
        )
        return cursor.fetchone() is None

    def save(self, listing: Listing, search_url: str = "") -> bool:
        """
        Save a listing to the database.
        Returns True if it was new (inserted), False if already existed.
        """
        if not self.is_new(listing.listing_id):
            return False

        self._conn.execute(
            """
            INSERT OR IGNORE INTO listings
            (listing_id, url, title, price, surface, rooms, location,
             image_url, description, agency, search_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.listing_id,
                listing.url,
                listing.title,
                listing.price,
                listing.surface,
                listing.rooms,
                listing.location,
                listing.image_url,
                listing.description,
                listing.agency,
                search_url,
            ),
        )
        self._conn.commit()
        logger.debug(f"Nouvelle annonce sauvegardée : {listing.listing_id}")
        return True

    def save_batch(self, listings: list[Listing], search_url: str = "") -> list[Listing]:
        """
        Save a batch of listings. Returns only the new ones.
        """
        new_listings = []
        for listing in listings:
            if self.save(listing, search_url):
                new_listings.append(listing)
        return new_listings

    def get_stats(self) -> dict:
        """Get statistics about stored listings."""
        total = self._conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        today = self._conn.execute(
            "SELECT COUNT(*) FROM listings WHERE DATE(first_seen) = DATE('now')"
        ).fetchone()[0]
        return {"total": total, "new_today": today}

    def get_all_ids(self) -> set[str]:
        """Get all known listing IDs."""
        rows = self._conn.execute("SELECT listing_id FROM listings").fetchall()
        return {row[0] for row in rows}

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
