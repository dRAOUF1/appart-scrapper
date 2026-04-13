"""Listing repository — CRUD for listings."""

from __future__ import annotations

from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values
from loguru import logger

from repositories.base import BaseRepository


def _clean_string(s: str) -> str:
    """Remove surrogate characters that can't be encoded to UTF-8."""
    if s is None:
        return None
    return s.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")


class ListingRepository(BaseRepository):
    """Listing CRUD operations."""

    def save_listing(self, listing) -> bool:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                def clean(v):
                    return _clean_string(v) if isinstance(v, str) else v

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
                        listing.listing_id, listing.url, clean(listing.title),
                        clean(listing.price), listing.surface, listing.rooms,
                        clean(listing.location), clean(listing.image_url),
                        clean(listing.description), clean(listing.agency), listing.source,
                        listing.legacy_id, listing.price_value, clean(listing.price_details),
                        clean(listing.city), clean(listing.district), listing.zip_code,
                        clean(listing.property_type), listing.is_private, clean(listing.phone),
                        listing.epc, listing.ges, listing.is_new,
                        listing.is_exclusive, listing.has_3d_visit,
                        listing.creation_date, listing.update_date,
                        clean(listing.headline), clean(listing.photos),
                    ),
                )
                conn.commit()
                return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False
        finally:
            self._release_conn(conn)

    def link_listing_to_search(self, search_id: int, listing_id: str) -> bool:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO search_listings (search_id, listing_id) VALUES (%s, %s)",
                    (search_id, listing_id),
                )
                conn.commit()
                return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False
        finally:
            self._release_conn(conn)

    def save_and_link(self, listings, search_id: int) -> tuple:
        """Save listings and link them to a search — batch insert via execute_values.

        Returns:
            (new_listings, already_linked)
        """
        from datetime import datetime, timedelta

        if not listings:
            return [], []

        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                def clean(v):
                    return _clean_string(v) if isinstance(v, str) else v

                listing_data = [
                    (
                        l.listing_id, l.url, clean(l.title), clean(l.price), l.surface, l.rooms,
                        clean(l.location), clean(l.image_url), clean(l.description), clean(l.agency), l.source,
                        l.legacy_id, l.price_value, clean(l.price_details), clean(l.city), clean(l.district),
                        l.zip_code, clean(l.property_type), l.is_private, clean(l.phone),
                        l.epc, l.ges, l.is_new, l.is_exclusive, l.has_3d_visit,
                        l.creation_date, l.update_date, clean(l.headline), clean(l.photos),
                    )
                    for l in listings
                ]

                execute_values(cur, """
                    INSERT INTO listings (
                        listing_id, url, title, price, surface, rooms, location, image_url,
                        description, agency, source, legacy_id, price_value, price_details,
                        city, district, zip_code, property_type, is_private, phone,
                        epc, ges, is_new, is_exclusive, has_3d_visit, creation_date,
                        update_date, headline, photos
                    ) VALUES %s
                    ON CONFLICT (listing_id) DO NOTHING
                """, listing_data, page_size=100)

                link_data = [(search_id, l.listing_id) for l in listings]
                execute_values(cur, """
                    INSERT INTO search_listings (search_id, listing_id)
                    VALUES %s
                    ON CONFLICT DO NOTHING
                """, link_data, page_size=100)

                conn.commit()

                threshold = datetime.utcnow() - timedelta(seconds=30)
                cur.execute(
                    "SELECT listing_id FROM search_listings WHERE search_id = %s AND found_at >= %s",
                    (search_id, threshold),
                )
                linked_ids = {row[0] for row in cur.fetchall()}

            new_for_search = [l for l in listings if l.listing_id in linked_ids]
            already_linked = [l for l in listings if l.listing_id not in linked_ids]
            return new_for_search, already_linked
        except Exception:
            conn.rollback()
            raise
        finally:
            self._release_conn(conn)

    def get_listings_for_search(self, search_id: int, limit: int = 50, offset: int = 0, blacklisted_agencies: list[str] | None = None) -> list[dict]:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                query = """SELECT l.*, sl.found_at
                       FROM listings l
                       JOIN search_listings sl ON sl.listing_id = l.listing_id
                       WHERE sl.search_id = %s"""
                params = [search_id]

                if blacklisted_agencies:
                    placeholders = ",".join(["%s"] * len(blacklisted_agencies))
                    query += f" AND l.agency NOT IN ({placeholders})"

                query += " ORDER BY sl.found_at DESC LIMIT %s OFFSET %s"
                params.extend(blacklisted_agencies if blacklisted_agencies else [])
                params.extend([limit, offset])

                cur.execute(query, params)
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def count_listings_for_search(self, search_id: int, blacklisted_agencies: list[str] | None = None) -> int:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                query = "SELECT COUNT(*) AS cnt FROM search_listings sl JOIN listings l ON l.listing_id = sl.listing_id WHERE sl.search_id = %s"
                params = [search_id]

                if blacklisted_agencies:
                    placeholders = ",".join(["%s"] * len(blacklisted_agencies))
                    query += f" AND l.agency NOT IN ({placeholders})"
                    params.extend(blacklisted_agencies)

                cur.execute(query, params)
                return cur.fetchone()[0]
        finally:
            self._release_conn(conn)

    def delete_old_listings(self, days: int = 4) -> int:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM listings WHERE first_seen < NOW() - INTERVAL '%s days'",
                    (str(days),),
                )
                conn.commit()
                deleted = cur.rowcount
            if deleted:
                logger.info(f"Supprimé {deleted} anciennes annonces (>{days} jours)")
            return deleted
        finally:
            self._release_conn(conn)

    def delete_listing(self, listing_id: str) -> bool:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM listings WHERE listing_id = %s", (listing_id,))
                conn.commit()
                return cur.rowcount > 0
        finally:
            self._release_conn(conn)

    def get_orphan_listings_count(self) -> int:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*) AS cnt FROM listings l
                       LEFT JOIN search_listings sl ON sl.listing_id = l.listing_id
                       WHERE sl.listing_id IS NULL"""
                )
                return cur.fetchone()[0]
        finally:
            self._release_conn(conn)

    def delete_orphan_listings(self) -> int:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM listings WHERE listing_id IN (
                        SELECT l.listing_id FROM listings l
                        LEFT JOIN search_listings sl ON sl.listing_id = l.listing_id
                        WHERE sl.listing_id IS NULL
                    )"""
                )
                conn.commit()
                deleted = cur.rowcount
            if deleted:
                logger.info(f"Supprimé {deleted} annonces orphelines")
            return deleted
        finally:
            self._release_conn(conn)

    def get_all_listings(self, limit=50, offset=0, search_term="", source_filter="") -> list[dict]:
        query = """SELECT l.*,
                          (SELECT COUNT(*) FROM search_listings WHERE listing_id = l.listing_id) AS linked_searches
                   FROM listings l"""
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

        query += " ORDER BY l.first_seen DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(query, params)
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def count_all_listings(self, search_term="", source_filter="") -> int:
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

        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchone()[0]
        finally:
            self._release_conn(conn)

    def get_listing_detail(self, listing_id: str) -> Optional[dict]:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
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
        finally:
            self._release_conn(conn)

    def get_unique_agencies_for_user(self, user_id: int) -> list[str]:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT DISTINCT l.agency
                       FROM listings l
                       JOIN search_listings sl ON sl.listing_id = l.listing_id
                       JOIN searches s ON s.id = sl.search_id
                       WHERE s.user_id = %s AND l.agency IS NOT NULL AND l.agency != ''
                       ORDER BY l.agency""",
                    (user_id,),
                )
                return [row[0] for row in cur.fetchall()]
        finally:
            self._release_conn(conn)
