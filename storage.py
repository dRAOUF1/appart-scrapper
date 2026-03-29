"""SQLite storage for multi-user listing tracking."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime
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
        source: str = "",
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
        self.source = source

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
            "source": self.source,
        }


class Storage:
    """SQLite-based storage with multi-user support."""

    def __init__(self, db_path: str = "listings.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_db()

    def _init_db(self) -> None:
        """Create all tables."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT UNIQUE NOT NULL,
                api_token   TEXT UNIQUE NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS searches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                label       TEXT NOT NULL,
                ntfy_topic  TEXT NOT NULL,
                source      TEXT NOT NULL DEFAULT 'seloger',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS listings (
                listing_id  TEXT PRIMARY KEY,
                url         TEXT NOT NULL,
                title       TEXT,
                price       TEXT,
                surface     TEXT,
                rooms       TEXT,
                location    TEXT,
                image_url   TEXT,
                description TEXT,
                agency      TEXT,
                source      TEXT DEFAULT '',
                first_seen  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS search_listings (
                search_id   INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
                listing_id  TEXT NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
                found_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (search_id, listing_id)
            );

            CREATE INDEX IF NOT EXISTS idx_listings_first_seen
                ON listings(first_seen);
            CREATE INDEX IF NOT EXISTS idx_search_listings_search
                ON search_listings(search_id);
            CREATE INDEX IF NOT EXISTS idx_searches_user
                ON searches(user_id);
        """)
        self._conn.commit()
        logger.debug(f"Base de données initialisée : {self.db_path}")

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def create_user(self, username: str) -> dict:
        """Create a new user and return {id, username, api_token}."""
        api_token = secrets.token_urlsafe(32)
        try:
            cur = self._conn.execute(
                "INSERT INTO users (username, api_token) VALUES (?, ?)",
                (username, api_token),
            )
            self._conn.commit()
            return {
                "id": cur.lastrowid,
                "username": username,
                "api_token": api_token,
            }
        except sqlite3.IntegrityError:
            raise ValueError(f"Le nom d'utilisateur '{username}' est déjà pris")

    def get_user_by_token(self, token: str) -> Optional[dict]:
        """Look up a user by API token."""
        row = self._conn.execute(
            "SELECT id, username, api_token, created_at FROM users WHERE api_token = ?",
            (token,),
        ).fetchone()
        return dict(row) if row else None

    def get_user_by_username(self, username: str) -> Optional[dict]:
        """Look up a user by username."""
        row = self._conn.execute(
            "SELECT id, username, api_token, created_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Searches
    # ------------------------------------------------------------------

    def create_search(self, user_id: int, label: str, ntfy_topic: str, source: str = "seloger") -> dict:
        """Create a search configuration for a user."""
        cur = self._conn.execute(
            "INSERT INTO searches (user_id, label, ntfy_topic, source) VALUES (?, ?, ?, ?)",
            (user_id, label, ntfy_topic, source),
        )
        self._conn.commit()
        return {
            "id": cur.lastrowid,
            "user_id": user_id,
            "label": label,
            "ntfy_topic": ntfy_topic,
            "source": source,
        }

    def get_user_searches(self, user_id: int) -> list[dict]:
        """Get all searches for a user."""
        rows = self._conn.execute(
            """SELECT s.id, s.label, s.ntfy_topic, s.source, s.created_at,
                      COUNT(sl.listing_id) AS listing_count
               FROM searches s
               LEFT JOIN search_listings sl ON sl.search_id = s.id
               WHERE s.user_id = ?
               GROUP BY s.id
               ORDER BY s.created_at DESC""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_search(self, search_id: int) -> Optional[dict]:
        """Get a single search by id (including user_id for auth checks)."""
        row = self._conn.execute(
            "SELECT id, user_id, label, ntfy_topic, source, created_at FROM searches WHERE id = ?",
            (search_id,),
        ).fetchone()
        return dict(row) if row else None

    def delete_search(self, search_id: int) -> bool:
        """Delete a search and its listing links (cascades)."""
        cur = self._conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Listings
    # ------------------------------------------------------------------

    def save_listing(self, listing: Listing) -> bool:
        """
        Insert a listing if it doesn't already exist.
        Returns True if new, False if already known.
        """
        try:
            self._conn.execute(
                """INSERT INTO listings
                   (listing_id, url, title, price, surface, rooms,
                    location, image_url, description, agency, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    listing.listing_id, listing.url, listing.title,
                    listing.price, listing.surface, listing.rooms,
                    listing.location, listing.image_url,
                    listing.description, listing.agency, listing.source,
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # already exists

    def link_listing_to_search(self, search_id: int, listing_id: str) -> bool:
        """
        Associate a listing with a search.
        Returns True if newly linked, False if already linked.
        """
        try:
            self._conn.execute(
                "INSERT INTO search_listings (search_id, listing_id) VALUES (?, ?)",
                (search_id, listing_id),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def save_and_link(
        self, listings: list[Listing], search_id: int
    ) -> tuple[list[Listing], list[Listing]]:
        """
        Save listings and link them to a search.

        Returns:
            (new_listings, already_linked) — 'new_listings' are those that were
            NOT previously linked to this particular search.
        """
        new_for_search: list[Listing] = []
        already_linked: list[Listing] = []

        for listing in listings:
            # Insert listing (may already exist globally)
            self.save_listing(listing)
            # Link to this search
            is_new_link = self.link_listing_to_search(search_id, listing.listing_id)
            if is_new_link:
                new_for_search.append(listing)
            else:
                already_linked.append(listing)

        return new_for_search, already_linked

    def get_listings_for_search(
        self, search_id: int, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """Get all listings linked to a search, most recent first."""
        rows = self._conn.execute(
            """SELECT l.*, sl.found_at
               FROM listings l
               JOIN search_listings sl ON sl.listing_id = l.listing_id
               WHERE sl.search_id = ?
               ORDER BY sl.found_at DESC
               LIMIT ? OFFSET ?""",
            (search_id, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_listings_for_search(self, search_id: int) -> int:
        """Count total listings for a search."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM search_listings WHERE search_id = ?",
            (search_id,),
        ).fetchone()
        return row["cnt"]

    def get_user_stats(self, user_id: int) -> dict:
        """Get statistics for a user."""
        searches = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM searches WHERE user_id = ?",
            (user_id,),
        ).fetchone()["cnt"]

        total = self._conn.execute(
            """SELECT COUNT(DISTINCT sl.listing_id) AS cnt
               FROM search_listings sl
               JOIN searches s ON s.id = sl.search_id
               WHERE s.user_id = ?""",
            (user_id,),
        ).fetchone()["cnt"]

        today = self._conn.execute(
            """SELECT COUNT(DISTINCT sl.listing_id) AS cnt
               FROM search_listings sl
               JOIN searches s ON s.id = sl.search_id
               WHERE s.user_id = ? AND DATE(sl.found_at) = DATE('now')""",
            (user_id,),
        ).fetchone()["cnt"]

        return {
            "searches": searches,
            "total_listings": total,
            "new_today": today,
        }

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
