"""PostgreSQL storage for multi-user listing tracking."""

from __future__ import annotations

import json
import secrets
from typing import Optional

import psycopg2
import psycopg2.extras
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
        legacy_id: str = "",
        price_value: float | None = None,
        price_details: str = "",
        city: str = "",
        district: str = "",
        zip_code: str = "",
        property_type: str = "",
        is_private: bool = False,
        phone: str = "[]",
        epc: str = "",
        ges: str = "",
        is_new: bool = False,
        is_exclusive: bool = False,
        has_3d_visit: bool = False,
        creation_date: str = "",
        update_date: str = "",
        headline: str = "",
        photos: str = "[]",
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
        self.legacy_id = legacy_id
        self.price_value = price_value
        self.price_details = price_details
        self.city = city
        self.district = district
        self.zip_code = zip_code
        self.property_type = property_type
        self.is_private = is_private
        self.phone = phone
        self.epc = epc
        self.ges = ges
        self.is_new = is_new
        self.is_exclusive = is_exclusive
        self.has_3d_visit = has_3d_visit
        self.creation_date = creation_date
        self.update_date = update_date
        self.headline = headline
        self.photos = photos

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
            "legacy_id": self.legacy_id,
            "price_value": self.price_value,
            "price_details": self.price_details,
            "city": self.city,
            "district": self.district,
            "zip_code": self.zip_code,
            "property_type": self.property_type,
            "is_private": self.is_private,
            "phone": self.phone,
            "epc": self.epc,
            "ges": self.ges,
            "is_new": self.is_new,
            "is_exclusive": self.is_exclusive,
            "has_3d_visit": self.has_3d_visit,
            "creation_date": self.creation_date,
            "update_date": self.update_date,
            "headline": self.headline,
            "photos": self.photos,
        }


class Storage:
    """PostgreSQL-based storage with multi-user support."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._conn = psycopg2.connect(database_url)
        self._init_db()

    def _init_db(self) -> None:
        """Create all tables. Uses advisory lock to prevent deadlocks with multiple workers."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(987654321)")
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
                CREATE INDEX IF NOT EXISTS idx_listings_first_seen
                    ON listings(first_seen);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_search_listings_search
                    ON search_listings(search_id);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_searches_user
                    ON searches(user_id);
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
                CREATE INDEX IF NOT EXISTS idx_admin_logs_created
                    ON admin_logs(created_at);
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_admin_logs_action
                    ON admin_logs(action);
            """)

            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS legacy_id TEXT DEFAULT '';
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS price_value FLOAT;
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS price_details TEXT DEFAULT '';
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS city TEXT DEFAULT '';
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS district TEXT DEFAULT '';
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS zip_code TEXT DEFAULT '';
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS property_type TEXT DEFAULT '';
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_private BOOLEAN DEFAULT FALSE;
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS phone JSONB DEFAULT '[]';
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS epc TEXT DEFAULT '';
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS ges TEXT DEFAULT '';
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_new BOOLEAN DEFAULT FALSE;
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_exclusive BOOLEAN DEFAULT FALSE;
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS has_3d_visit BOOLEAN DEFAULT FALSE;
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS creation_date TEXT DEFAULT '';
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS update_date TEXT DEFAULT '';
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS headline TEXT DEFAULT '';
            """)
            cur.execute("""
                ALTER TABLE listings ADD COLUMN IF NOT EXISTS photos JSONB DEFAULT '[]';
            """)
            cur.execute("""
                ALTER TABLE searches ADD COLUMN IF NOT EXISTS criteria JSONB DEFAULT '{}';
            """)
            cur.execute("""
                ALTER TABLE searches ADD COLUMN IF NOT EXISTS scrape_interval INTEGER DEFAULT 5;
            """)
            cur.execute("""
                ALTER TABLE searches ADD COLUMN IF NOT EXISTS last_scraped TIMESTAMP;
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
                CREATE INDEX IF NOT EXISTS idx_scrape_logs_search
                    ON scrape_logs(search_id);
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_scrape_logs_started
                    ON scrape_logs(started_at DESC);
            """)

            cur.execute("""
                ALTER TABLE scrape_logs ADD COLUMN IF NOT EXISTS raw_logs TEXT;
            """)

        self._conn.commit()
        logger.debug("Tables PostgreSQL initialisées")

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def create_user(self, username: str) -> dict:
        """Create a new user and return {id, username, api_token}."""
        api_token = secrets.token_urlsafe(32)
        try:
            with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "INSERT INTO users (username, api_token) VALUES (%s, %s) RETURNING id",
                    (username, api_token),
                )
                row = cur.fetchone()
                self._conn.commit()
                return {
                    "id": row["id"],
                    "username": username,
                    "api_token": api_token,
                }
        except psycopg2.IntegrityError:
            self._conn.rollback()
            raise ValueError(f"Le nom d'utilisateur '{username}' est déjà pris")

    def get_user_by_token(self, token: str) -> Optional[dict]:
        """Look up a user by API token."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, api_token, created_at FROM users WHERE api_token = %s",
                (token,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_user_by_username(self, username: str) -> Optional[dict]:
        """Look up a user by username."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, api_token, created_at FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # Searches
    # ------------------------------------------------------------------

    def create_search(self, user_id: int, label: str, ntfy_topic: str, source: str = "seloger", criteria: dict | None = None, scrape_interval: int = 5) -> dict:
        """Create a search configuration for a user."""
        criteria_json = json.dumps(criteria or {})
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO searches (user_id, label, ntfy_topic, source, criteria, scrape_interval) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (user_id, label, ntfy_topic, source, criteria_json, scrape_interval),
            )
            row = cur.fetchone()
            self._conn.commit()
            return {
                "id": row["id"],
                "user_id": user_id,
                "label": label,
                "ntfy_topic": ntfy_topic,
                "source": source,
                "criteria": criteria or {},
                "scrape_interval": scrape_interval,
            }

    def update_search_criteria(self, search_id: int, criteria: dict) -> bool:
        """Update the criteria for a search."""
        criteria_json = json.dumps(criteria)
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE searches SET criteria = %s WHERE id = %s",
                (criteria_json, search_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_search(self, search_id: int, user_id: int, label: str | None = None, ntfy_topic: str | None = None, criteria: dict | None = None, scrape_interval: int | None = None) -> bool:
        """Update multiple fields of a search at once."""
        fields = []
        params = []
        if label is not None:
            fields.append("label = %s")
            params.append(label)
        if ntfy_topic is not None:
            fields.append("ntfy_topic = %s")
            params.append(ntfy_topic)
        if criteria is not None:
            fields.append("criteria = %s")
            params.append(json.dumps(criteria))
        if scrape_interval is not None:
            fields.append("scrape_interval = %s")
            params.append(scrape_interval)
        if not fields:
            return False
        params.extend([search_id, user_id])
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE searches SET {', '.join(fields)} WHERE id = %s AND user_id = %s",
                params,
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_scrape_interval(self, search_id: int, interval_minutes: int) -> bool:
        """Update the scrape interval for a search."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE searches SET scrape_interval = %s WHERE id = %s",
                (interval_minutes, search_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_last_scraped(self, search_id: int) -> bool:
        """Update the last scraped timestamp."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE searches SET last_scraped = CURRENT_TIMESTAMP WHERE id = %s",
                (search_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_user_searches(self, user_id: int) -> list[dict]:
        """Get all searches for a user."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT s.id, s.label, s.ntfy_topic, s.source, s.criteria,
                          s.scrape_interval, s.last_scraped, s.created_at,
                          COUNT(sl.listing_id) AS listing_count
                   FROM searches s
                   LEFT JOIN search_listings sl ON sl.search_id = s.id
                   WHERE s.user_id = %s
                   GROUP BY s.id
                   ORDER BY s.created_at DESC""",
                (user_id,),
            )
            rows = cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get("criteria"), str):
                    d["criteria"] = json.loads(d["criteria"])
                result.append(d)
            return result

    def get_search(self, search_id: int) -> Optional[dict]:
        """Get a single search by id (including user_id for auth checks)."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, user_id, label, ntfy_topic, source, criteria, scrape_interval, last_scraped, created_at FROM searches WHERE id = %s",
                (search_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            if isinstance(d.get("criteria"), str):
                d["criteria"] = json.loads(d["criteria"])
            return d

    def delete_search(self, search_id: int) -> bool:
        """Delete a search and its listing links (cascades)."""
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM searches WHERE id = %s", (search_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def get_all_searches(self, user_filter="", source_filter="") -> list[dict]:
        """Get all searches with user info and listing counts."""
        query = """SELECT s.id, s.label, s.ntfy_topic, s.source, s.criteria,
                          s.scrape_interval, s.last_scraped, s.created_at,
                          u.id AS user_id, u.username,
                          COUNT(sl.listing_id) AS listing_count
                   FROM searches s
                   JOIN users u ON u.id = s.user_id
                   LEFT JOIN search_listings sl ON sl.search_id = s.id"""
        conditions = []
        params = []

        if user_filter:
            conditions.append("u.username ILIKE %s")
            params.append(f"%{user_filter}%")
        if source_filter:
            conditions.append("s.source = %s")
            params.append(source_filter)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " GROUP BY s.id, u.id ORDER BY s.created_at DESC"

        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            result = []
            for r in cur.fetchall():
                d = dict(r)
                if isinstance(d.get("criteria"), str):
                    d["criteria"] = json.loads(d["criteria"])
                result.append(d)
            return result

    def get_search_detail(self, search_id: int) -> Optional[dict]:
        """Get a search with full details including user info and recent listings."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT s.*, u.username
                   FROM searches s
                   JOIN users u ON u.id = s.user_id
                   WHERE s.id = %s""",
                (search_id,),
            )
            search = cur.fetchone()
            if not search:
                return None
            result = dict(search)
            if isinstance(result.get("criteria"), str):
                result["criteria"] = json.loads(result["criteria"])

            cur.execute(
                """SELECT l.*, sl.found_at
                   FROM listings l
                   JOIN search_listings sl ON sl.listing_id = l.listing_id
                   WHERE sl.search_id = %s
                   ORDER BY sl.found_at DESC LIMIT 10""",
                (search_id,),
            )
            result["recent_listings"] = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM search_listings WHERE search_id = %s",
                (search_id,),
            )
            result["total_listings"] = cur.fetchone()["cnt"]
            return result

    # ------------------------------------------------------------------
    # Scrape Logs
    # ------------------------------------------------------------------

    def create_scrape_log(self, search_id: int, status: str, listings_found: int = 0, new_listings: int = 0, error_message: str = "", details: dict | None = None, started_at = None) -> int:
        """Create a scrape log entry. Returns the log id."""
        import datetime
        now = started_at or datetime.datetime.utcnow()
        completed_at = datetime.datetime.utcnow()
        duration = (completed_at - now).total_seconds() if started_at else 0
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO scrape_logs
                   (search_id, started_at, completed_at, status, listings_found, new_listings, error_message, details, duration_sec)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (search_id, now, completed_at, status, listings_found, new_listings, error_message, json.dumps(details or {}), duration),
            )
            row = cur.fetchone()
            self._conn.commit()
            return row["id"]

    def get_scrape_logs(self, search_id: int, limit: int = 50, offset: int = 0, status_filter: str = "") -> list[dict]:
        """Get scrape logs for a search."""
        query = "SELECT * FROM scrape_logs WHERE search_id = %s"
        params = [search_id]
        if status_filter:
            query += " AND status = %s"
            params.append(status_filter)
        query += " ORDER BY started_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            result = []
            for r in cur.fetchall():
                d = dict(r)
                if isinstance(d.get("details"), str):
                    d["details"] = json.loads(d["details"])
                result.append(d)
            return result

    def count_scrape_logs(self, search_id: int, status_filter: str = "") -> int:
        """Count scrape logs for a search."""
        query = "SELECT COUNT(*) AS cnt FROM scrape_logs WHERE search_id = %s"
        params = [search_id]
        if status_filter:
            query += " AND status = %s"
            params.append(status_filter)
        with self._conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()[0]

    def get_scrape_stats(self, search_id: int) -> dict:
        """Get scrape statistics for a search."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE status = 'success') AS success_count,
                          COUNT(*) FILTER (WHERE status = 'error') AS error_count,
                          COUNT(*) FILTER (WHERE status = 'partial') AS partial_count,
                          COALESCE(AVG(listings_found), 0) AS avg_listings,
                          COALESCE(AVG(new_listings), 0) AS avg_new,
                          COALESCE(AVG(duration_sec), 0) AS avg_duration
                   FROM scrape_logs WHERE search_id = %s""",
                (search_id,),
            )
            row = cur.fetchone()

            cur.execute(
                """SELECT status, started_at, error_message
                   FROM scrape_logs WHERE search_id = %s
                   ORDER BY started_at DESC LIMIT 1""",
                (search_id,),
            )
            last = cur.fetchone()

        stats = dict(row) if row else {}
        stats["last_scrape"] = dict(last) if last else None
        return stats

    def update_scrape_log_raw(self, log_id: int, raw_logs: str) -> bool:
        """Save raw log text to a scrape log entry."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE scrape_logs SET raw_logs = %s WHERE id = %s",
                (raw_logs, log_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_scrape_log_raw(self, log_id: int, user_id: int | None = None) -> dict | None:
        """Get raw logs for a scrape log entry."""
        query = "SELECT id, search_id, status, started_at, completed_at, raw_logs FROM scrape_logs WHERE id = %s"
        params = [log_id]
        if user_id is not None:
            query += " AND search_id IN (SELECT id FROM searches WHERE user_id = %s)"
            params.append(user_id)
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return dict(row) if row else None

    def get_latest_scrape_log_id(self, search_id: int) -> int | None:
        """Get the most recent scrape log id for a search."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM scrape_logs WHERE search_id = %s ORDER BY started_at DESC LIMIT 1",
                (search_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    # ------------------------------------------------------------------
    # Listings
    # ------------------------------------------------------------------

    def save_listing(self, listing: Listing) -> bool:
        """
        Insert a listing if it doesn't already exist.
        Returns True if new, False if already known.
        """
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO listings
                       (listing_id, url, title, price, surface, rooms,
                        location, image_url, description, agency, source,
                        legacy_id, price_value, price_details, city, district,
                        zip_code, property_type, is_private, phone,
                        epc, ges, is_new, is_exclusive, has_3d_visit,
                        creation_date, update_date, headline, photos)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        listing.listing_id, listing.url, listing.title,
                        listing.price, listing.surface, listing.rooms,
                        listing.location, listing.image_url,
                        listing.description, listing.agency, listing.source,
                        listing.legacy_id, listing.price_value, listing.price_details,
                        listing.city, listing.district, listing.zip_code,
                        listing.property_type, listing.is_private, listing.phone,
                        listing.epc, listing.ges, listing.is_new,
                        listing.is_exclusive, listing.has_3d_visit,
                        listing.creation_date, listing.update_date,
                        listing.headline, listing.photos,
                    ),
                )
                self._conn.commit()
                return True
        except psycopg2.IntegrityError:
            self._conn.rollback()
            return False

    def link_listing_to_search(self, search_id: int, listing_id: str) -> bool:
        """
        Associate a listing with a search.
        Returns True if newly linked, False if already linked.
        """
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO search_listings (search_id, listing_id) VALUES (%s, %s)",
                    (search_id, listing_id),
                )
                self._conn.commit()
                return True
        except psycopg2.IntegrityError:
            self._conn.rollback()
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
            self.save_listing(listing)
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
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT l.*, sl.found_at
                   FROM listings l
                   JOIN search_listings sl ON sl.listing_id = l.listing_id
                   WHERE sl.search_id = %s
                   ORDER BY sl.found_at DESC
                   LIMIT %s OFFSET %s""",
                (search_id, limit, offset),
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def count_listings_for_search(self, search_id: int) -> int:
        """Count total listings for a search."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM search_listings WHERE search_id = %s",
                (search_id,),
            )
            row = cur.fetchone()
            return row["cnt"]

    def get_user_stats(self, user_id: int) -> dict:
        """Get statistics for a user."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM searches WHERE user_id = %s",
                (user_id,),
            )
            searches = cur.fetchone()["cnt"]

            cur.execute(
                """SELECT COUNT(DISTINCT sl.listing_id) AS cnt
                   FROM search_listings sl
                   JOIN searches s ON s.id = sl.search_id
                   WHERE s.user_id = %s""",
                (user_id,),
            )
            total = cur.fetchone()["cnt"]

            cur.execute(
                """SELECT COUNT(DISTINCT sl.listing_id) AS cnt
                   FROM search_listings sl
                   JOIN searches s ON s.id = sl.search_id
                   WHERE s.user_id = %s AND DATE(sl.found_at) = CURRENT_DATE""",
                (user_id,),
            )
            today = cur.fetchone()["cnt"]

        return {
            "searches": searches,
            "total_listings": total,
            "new_today": today,
        }

    # ------------------------------------------------------------------
    # Admin
    # ------------------------------------------------------------------

    def get_admin_stats(self) -> dict:
        """Global platform statistics."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS c FROM users")
            users = cur.fetchone()["c"]

            cur.execute("SELECT COUNT(*) AS c FROM searches")
            searches = cur.fetchone()["c"]

            cur.execute("SELECT COUNT(*) AS c FROM listings")
            listings = cur.fetchone()["c"]

            cur.execute(
                "SELECT COUNT(*) AS c FROM search_listings WHERE DATE(found_at) = CURRENT_DATE"
            )
            today = cur.fetchone()["c"]

        return {
            "users": users,
            "searches": searches,
            "total_listings": listings,
            "new_today": today,
        }

    def get_all_users(self) -> list[dict]:
        """List all users with their search and listing counts."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT u.id, u.username, u.api_token, u.created_at,
                          COUNT(DISTINCT s.id)          AS search_count,
                          COUNT(DISTINCT sl.listing_id) AS listing_count
                   FROM users u
                   LEFT JOIN searches s  ON s.user_id = u.id
                   LEFT JOIN search_listings sl ON sl.search_id = s.id
                   GROUP BY u.id
                   ORDER BY u.created_at DESC"""
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def delete_old_listings(self, days: int = 4) -> int:
        """Delete listings older than N days. Returns count of deleted listings."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM listings WHERE first_seen < NOW() - INTERVAL '{days} days'",
            )
            self._conn.commit()
            deleted = cur.rowcount
        if deleted:
            logger.info(f"Supprimé {deleted} anciennes annonces (>{days} jours)")
        return deleted

    def delete_listing(self, listing_id: str) -> bool:
        """Delete a single listing by ID. Cascades to search_listings."""
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM listings WHERE listing_id = %s", (listing_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def get_orphan_listings_count(self) -> int:
        """Count listings not linked to any search."""
        with self._conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS cnt FROM listings l
                   LEFT JOIN search_listings sl ON sl.listing_id = l.listing_id
                   WHERE sl.listing_id IS NULL"""
            )
            return cur.fetchone()[0]

    def delete_orphan_listings(self) -> int:
        """Delete all listings not linked to any search. Returns count."""
        with self._conn.cursor() as cur:
            cur.execute(
                """DELETE FROM listings WHERE listing_id IN (
                    SELECT l.listing_id FROM listings l
                    LEFT JOIN search_listings sl ON sl.listing_id = l.listing_id
                    WHERE sl.listing_id IS NULL
                )"""
            )
            self._conn.commit()
            deleted = cur.rowcount
        if deleted:
            logger.info(f"Supprimé {deleted} annonces orphelines")
        return deleted

    def get_all_listings(self, limit=50, offset=0, search_term="", source_filter="") -> list[dict]:
        """Get all listings with pagination and filters."""
        query = """SELECT l.*, COUNT(sl.search_id) AS linked_searches
                   FROM listings l
                   LEFT JOIN search_listings sl ON sl.listing_id = l.listing_id"""
        conditions = []
        params = []

        if search_term:
            conditions.append("(l.title ILIKE %s OR l.location ILIKE %s OR l.description ILIKE %s)")
            params.extend([f"%{search_term}%", f"%{search_term}%", f"%{search_term}%"])
        if source_filter:
            conditions.append("l.source = %s")
            params.append(source_filter)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " GROUP BY l.listing_id ORDER BY l.first_seen DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    def count_all_listings(self, search_term="", source_filter="") -> int:
        """Count all listings with filters."""
        query = "SELECT COUNT(DISTINCT l.listing_id) AS cnt FROM listings l"
        conditions = []
        params = []

        if search_term:
            conditions.append("(l.title ILIKE %s OR l.location ILIKE %s OR l.description ILIKE %s)")
            params.extend([f"%{search_term}%", f"%{search_term}%", f"%{search_term}%"])
        if source_filter:
            conditions.append("l.source = %s")
            params.append(source_filter)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        with self._conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()[0]

    def get_listing_detail(self, listing_id: str) -> Optional[dict]:
        """Get a single listing with its linked searches."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM listings WHERE listing_id = %s", (listing_id,))
            listing = cur.fetchone()
            if not listing:
                return None
            result = dict(listing)

            cur.execute(
                """SELECT s.id, s.label, s.source, u.username
                   FROM search_listings sl
                   JOIN searches s ON s.id = sl.search_id
                   JOIN users u ON u.id = s.user_id
                   WHERE sl.listing_id = %s""",
                (listing_id,),
            )
            result["linked_searches"] = [dict(r) for r in cur.fetchall()]
            return result

    def delete_search_admin(self, search_id: int) -> bool:
        """Delete a search as admin. Returns True if deleted."""
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM searches WHERE id = %s", (search_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def delete_user(self, user_id: int) -> bool:
        """Delete a user and all associated data (cascades)."""
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def reset_user_token(self, user_id: int) -> str:
        """Generate a new API token for a user. Returns the new token."""
        new_token = secrets.token_urlsafe(32)
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET api_token = %s WHERE id = %s",
                (new_token, user_id),
            )
            self._conn.commit()
        return new_token

    def get_user_detail(self, user_id: int) -> Optional[dict]:
        """Get a user with full stats and recent activity."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, api_token, created_at FROM users WHERE id = %s",
                (user_id,),
            )
            user = cur.fetchone()
            if not user:
                return None
            result = dict(user)

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM searches WHERE user_id = %s",
                (user_id,),
            )
            result["search_count"] = cur.fetchone()["cnt"]

            cur.execute(
                """SELECT COUNT(DISTINCT sl.listing_id) AS cnt
                   FROM search_listings sl
                   JOIN searches s ON s.id = sl.search_id
                   WHERE s.user_id = %s""",
                (user_id,),
            )
            result["listing_count"] = cur.fetchone()["cnt"]

            cur.execute(
                """SELECT s.id, s.label, s.source, s.created_at,
                          COUNT(sl.listing_id) AS listing_count
                   FROM searches s
                   LEFT JOIN search_listings sl ON sl.search_id = s.id
                   WHERE s.user_id = %s
                   GROUP BY s.id
                   ORDER BY s.created_at DESC""",
                (user_id,),
            )
            result["searches"] = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """SELECT l.title, l.price, l.location, l.first_seen, s.label AS search_label
                   FROM search_listings sl
                   JOIN searches s ON s.id = sl.search_id
                   JOIN listings l ON l.listing_id = sl.listing_id
                   WHERE s.user_id = %s
                   ORDER BY sl.found_at DESC LIMIT 10""",
                (user_id,),
            )
            result["recent_listings"] = [dict(r) for r in cur.fetchall()]
            return result

    def get_enhanced_admin_stats(self) -> dict:
        """Get comprehensive admin statistics."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS c FROM users")
            users = cur.fetchone()["c"]

            cur.execute("SELECT COUNT(*) AS c FROM searches")
            searches = cur.fetchone()["c"]

            cur.execute("SELECT COUNT(*) AS c FROM listings")
            listings = cur.fetchone()["c"]

            cur.execute("SELECT COUNT(*) AS c FROM search_listings")
            search_listings = cur.fetchone()["c"]

            cur.execute(
                "SELECT COUNT(*) AS c FROM search_listings WHERE DATE(found_at) = CURRENT_DATE"
            )
            today = cur.fetchone()["c"]

            cur.execute(
                """SELECT COUNT(DISTINCT l.listing_id) AS c FROM listings l
                   LEFT JOIN search_listings sl ON sl.listing_id = l.listing_id
                   WHERE sl.listing_id IS NULL"""
            )
            orphans = cur.fetchone()["c"]

            cur.execute(
                "SELECT COALESCE(AVG(cnt), 0) AS avg_listings FROM (SELECT COUNT(*) AS cnt FROM search_listings GROUP BY search_id) sub"
            )
            avg_listings_per_search = round(cur.fetchone()["avg_listings"], 1)

            cur.execute(
                """SELECT u.username, COUNT(DISTINCT sl.listing_id) AS listing_count
                   FROM users u
                   JOIN searches s ON s.user_id = u.id
                   JOIN search_listings sl ON sl.search_id = s.id
                   GROUP BY u.id, u.username
                   ORDER BY listing_count DESC LIMIT 5"""
            )
            top_users = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """SELECT s.id, s.label, u.username, COUNT(sl.listing_id) AS listing_count
                   FROM searches s
                   JOIN users u ON u.id = s.user_id
                   JOIN search_listings sl ON sl.search_id = s.id
                   GROUP BY s.id, u.username
                   ORDER BY listing_count DESC LIMIT 5"""
            )
            top_searches = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """SELECT DATE(sl.found_at) AS day, COUNT(DISTINCT sl.listing_id) AS count
                   FROM search_listings sl
                   WHERE sl.found_at >= CURRENT_DATE - INTERVAL '7 days'
                   GROUP BY DATE(sl.found_at)
                   ORDER BY day"""
            )
            activity_7d = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """SELECT source, COUNT(*) AS cnt FROM searches
                   GROUP BY source ORDER BY cnt DESC"""
            )
            sources_breakdown = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT COUNT(*) AS c FROM users WHERE id NOT IN (SELECT DISTINCT user_id FROM searches)"
            )
            users_without_searches = cur.fetchone()["c"]

        return {
            "users": users,
            "searches": searches,
            "total_listings": listings,
            "search_listings": search_listings,
            "new_today": today,
            "orphan_listings": orphans,
            "avg_listings_per_search": avg_listings_per_search,
            "top_users": top_users,
            "top_searches": top_searches,
            "activity_7d": activity_7d,
            "sources_breakdown": sources_breakdown,
            "users_without_searches": users_without_searches,
        }

    def log_admin_action(self, action: str, details: str = "", performed_by: str = "") -> None:
        """Log an admin action."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO admin_logs (action, details, performed_by) VALUES (%s, %s, %s)",
                    (action, details, performed_by),
                )
                self._conn.commit()
        except Exception as e:
            logger.error(f"Failed to log admin action: {e}")

    def get_admin_logs(self, limit=50, offset=0, action_filter="", date_from="", date_to="") -> list[dict]:
        """Get admin activity logs."""
        query = "SELECT * FROM admin_logs"
        conditions = []
        params = []

        if action_filter:
            conditions.append("action = %s")
            params.append(action_filter)
        if date_from:
            conditions.append("created_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= %s")
            params.append(date_to)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    def count_admin_logs(self, action_filter="", date_from="", date_to="") -> int:
        """Count admin logs with filters."""
        query = "SELECT COUNT(*) AS cnt FROM admin_logs"
        conditions = []
        params = []

        if action_filter:
            conditions.append("action = %s")
            params.append(action_filter)
        if date_from:
            conditions.append("created_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= %s")
            params.append(date_to)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        with self._conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()[0]

    def purge_old_logs(self, days: int = 30) -> int:
        """Delete logs older than N days. Returns count."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM admin_logs WHERE created_at < NOW() - INTERVAL '{days} days'"
            )
            self._conn.commit()
            deleted = cur.rowcount
        if deleted:
            logger.info(f"Purgé {deleted} anciens logs admin")
        return deleted

    def get_db_stats(self) -> dict:
        """Get database size and per-table statistics."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database())) AS size")
            db_size = cur.fetchone()["size"]

            cur.execute(
                """SELECT relname AS table_name,
                          n_live_tup AS row_count,
                          pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
                          pg_size_pretty(pg_relation_size(relid)) AS data_size
                   FROM pg_stat_user_tables
                   ORDER BY pg_total_relation_size(relid) DESC"""
            )
            tables = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """SELECT schemaname, tablename, indexname
                   FROM pg_indexes
                   WHERE schemaname = 'public'
                   ORDER BY tablename, indexname"""
            )
            raw_indexes = cur.fetchall()

            indexes = []
            for idx in raw_indexes:
                idx_dict = dict(idx)
                cur.execute(
                    "SELECT pg_size_pretty(pg_relation_size(%s::regclass)) AS size",
                    (idx_dict["indexname"],),
                )
                size_row = cur.fetchone()
                idx_dict["index_size"] = size_row["size"] if size_row else "N/A"
                indexes.append(idx_dict)

        return {
            "db_size": db_size,
            "tables": tables,
            "indexes": indexes,
        }

    def get_table_details(self, table_name: str) -> dict:
        """Get columns, constraints, and indexes for a specific table."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT column_name, data_type, is_nullable, column_default,
                          character_maximum_length
                   FROM information_schema.columns
                   WHERE table_name = %s AND table_schema = 'public'
                   ORDER BY ordinal_position""",
                (table_name,),
            )
            columns = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """SELECT indexname, indexdef
                   FROM pg_indexes
                   WHERE tablename = %s AND schemaname = 'public'""",
                (table_name,),
            )
            indexes = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """SELECT conname AS constraint_name, contype AS constraint_type,
                          pg_get_constraintdef(oid) AS definition
                   FROM pg_constraint
                   WHERE conrelid = %s::regclass""",
                (table_name,),
            )
            constraints = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT pg_size_pretty(pg_total_relation_size(%s::regclass)) AS total_size",
                (table_name,),
            )
            size_row = cur.fetchone()
            total_size = size_row["total_size"] if size_row else "N/A"

        return {
            "columns": columns,
            "indexes": indexes,
            "constraints": constraints,
            "total_size": total_size,
        }

    def execute_query(self, sql: str) -> tuple[list[dict], int, Optional[str]]:
        """Execute a SQL query. Returns (rows, row_count, error)."""
        try:
            with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                self._conn.commit()
                if cur.description:
                    rows = cur.fetchall()
                    row_count = cur.rowcount
                    return [dict(r) for r in rows], row_count, None
                else:
                    return [], cur.rowcount, None
        except Exception as e:
            self._conn.rollback()
            return [], 0, str(e)

    def get_active_connections(self) -> list[dict]:
        """Get active PostgreSQL connections."""
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT pid, usename, application_name, client_addr,
                          backend_start, state, query, query_start
                   FROM pg_stat_activity
                   WHERE datname = current_database() AND pid != pg_backend_pid()
                   ORDER BY backend_start"""
            )
            return [dict(r) for r in cur.fetchall()]

    def truncate_table(self, table_name: str) -> bool:
        """Truncate a table. Returns True if successful."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(f"TRUNCATE TABLE {table_name} CASCADE")
                self._conn.commit()
                return True
        except Exception as e:
            self._conn.rollback()
            logger.error(f"Failed to truncate {table_name}: {e}")
            return False

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
