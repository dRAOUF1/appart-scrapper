"""Listing repository — CRUD for listings."""

from __future__ import annotations

import psycopg2
import psycopg2.extras
from loguru import logger
from psycopg2.extras import execute_values

from repositories.base import BaseRepository


def _clean_string(s: str) -> str:
    """Remove surrogate characters that can't be encoded to UTF-8."""
    if s is None:
        return None
    return s.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")


class ListingRepository(BaseRepository):
    """Listing CRUD operations."""

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
        if not listings:
            return [], []

        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                def clean(v):
                    return _clean_string(v) if isinstance(v, str) else v

                listing_data = [
                    (
                        item.listing_id, item.url, clean(item.title), clean(item.price), item.surface, item.rooms,
                        clean(item.location), clean(item.image_url), clean(item.description),
                        clean(item.agency), item.source,
                        item.legacy_id, item.price_value, clean(item.price_details),
                        clean(item.city), clean(item.district),
                        item.zip_code, clean(item.property_type), item.is_private, clean(item.phone),
                        item.epc, item.ges, item.is_new, item.is_exclusive, item.has_3d_visit,
                        item.creation_date, item.update_date, clean(item.headline), clean(item.photos),
                    )
                    for item in listings
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

                # notified=FALSE explicitly on every new link, regardless of the
                # column's DEFAULT TRUE (which exists only to backfill pre-existing
                # rows as "already notified" when the column was introduced).
                link_data = [(search_id, item.listing_id, False) for item in listings]
                inserted = execute_values(cur, """
                    INSERT INTO search_listings (search_id, listing_id, notified)
                    VALUES %s
                    ON CONFLICT DO NOTHING
                    RETURNING listing_id
                """, link_data, page_size=100, fetch=True)

                conn.commit()

            newly_linked_ids = {row[0] for row in inserted}
            new_for_search = [item for item in listings if item.listing_id in newly_linked_ids]
            already_linked = [item for item in listings if item.listing_id not in newly_linked_ids]
            return new_for_search, already_linked
        except Exception:
            conn.rollback()
            raise
        finally:
            self._release_conn(conn)

    def get_unnotified_listings_for_search(self, search_id: int) -> list:
        """Listings linked to a search but not yet successfully notified.

        Includes this run's new listings plus any left over from a previous
        scrape that crashed or failed to deliver the notification, so nothing
        is silently lost.
        """
        from models.listing import Listing

        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute("""
                    SELECT l.* FROM search_listings sl
                    JOIN listings l ON l.listing_id = sl.listing_id
                    WHERE sl.search_id = %s AND sl.notified = FALSE
                """, (search_id,))
                return [Listing.from_dict(dict(row)) for row in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def mark_listings_notified(self, search_id: int, listing_ids: list[str]) -> None:
        """Mark listings as successfully notified for this search."""
        if not listing_ids:
            return
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE search_listings SET notified = TRUE "
                    "WHERE search_id = %s AND listing_id = ANY(%s)",
                    (search_id, listing_ids),
                )
                conn.commit()
        finally:
            self._release_conn(conn)

    def _build_filter_clauses(self, filters: dict, params: list, prefix: str = "l.") -> str:
        clauses = []
        if filters.get("q"):
            val = f"%{filters['q']}%"
            clauses.append(
                f"({prefix}title ILIKE %s OR {prefix}location ILIKE %s"
                f" OR {prefix}agency ILIKE %s OR {prefix}description ILIKE %s)"
            )
            params.extend([val, val, val, val])
        if filters.get("price_min") is not None:
            clauses.append(f"{prefix}price_value >= %s")
            params.append(filters["price_min"])
        if filters.get("price_max") is not None:
            clauses.append(f"{prefix}price_value <= %s")
            params.append(filters["price_max"])
        if filters.get("surface_min") is not None:
            clauses.append(f"CAST(NULLIF(REGEXP_REPLACE({prefix}surface, '[^0-9.]', '', 'g'), '') AS NUMERIC) >= %s")
            params.append(filters["surface_min"])
        if filters.get("surface_max") is not None:
            clauses.append(f"CAST(NULLIF(REGEXP_REPLACE({prefix}surface, '[^0-9.]', '', 'g'), '') AS NUMERIC) <= %s")
            params.append(filters["surface_max"])
        if filters.get("rooms_min") is not None:
            clauses.append(f"CAST(NULLIF(REGEXP_REPLACE({prefix}rooms, '[^0-9.]', '', 'g'), '') AS NUMERIC) >= %s")
            params.append(filters["rooms_min"])
        if filters.get("rooms_max") is not None:
            clauses.append(f"CAST(NULLIF(REGEXP_REPLACE({prefix}rooms, '[^0-9.]', '', 'g'), '') AS NUMERIC) <= %s")
            params.append(filters["rooms_max"])
        if filters.get("city"):
            clauses.append(f"{prefix}city ILIKE %s")
            params.append(f"%{filters['city']}%")
        if filters.get("district"):
            clauses.append(f"{prefix}district ILIKE %s")
            params.append(f"%{filters['district']}%")
        if filters.get("zip_code"):
            clauses.append(f"{prefix}zip_code = %s")
            params.append(filters["zip_code"])
        if filters.get("property_type"):
            clauses.append(f"{prefix}property_type = %s")
            params.append(filters["property_type"])
        if filters.get("agency"):
            clauses.append(f"{prefix}agency = %s")
            params.append(filters["agency"])
        if filters.get("epc"):
            clauses.append(f"{prefix}epc = %s")
            params.append(filters["epc"])
        if filters.get("ges"):
            clauses.append(f"{prefix}ges = %s")
            params.append(filters["ges"])
        if filters.get("is_private") is not None:
            clauses.append(f"{prefix}is_private = %s")
            params.append(filters["is_private"])
        if filters.get("is_new") is not None:
            clauses.append(f"{prefix}is_new = %s")
            params.append(filters["is_new"])
        if filters.get("date_min"):
            clauses.append(f"{prefix}creation_date >= %s")
            params.append(filters["date_min"])
        if filters.get("blacklisted_agencies"):
            placeholders = ",".join(["%s"] * len(filters["blacklisted_agencies"]))
            clauses.append(f"{prefix}agency NOT IN ({placeholders})")
            params.extend(filters["blacklisted_agencies"])
        return " AND ".join(clauses)

    def _build_order_clause(self, sort: str = "found_at_desc") -> str:
        sort_map = {
            "found_at_desc": "sl.found_at DESC",
            "found_at_asc": "sl.found_at ASC",
            "price_asc": "l.price_value ASC NULLS LAST",
            "price_desc": "l.price_value DESC NULLS LAST",
            "surface_asc": "CAST(NULLIF(REGEXP_REPLACE(l.surface, '[^0-9.]', '', 'g'), '') AS NUMERIC) ASC NULLS LAST",
            "surface_desc": (
                "CAST(NULLIF(REGEXP_REPLACE(l.surface, '[^0-9.]', '', 'g'), '') AS NUMERIC) DESC NULLS LAST"
            ),
            "date_desc": "l.creation_date DESC NULLS LAST",
            "date_asc": "l.creation_date ASC NULLS LAST",
        }
        return sort_map.get(sort, "sl.found_at DESC")

    def get_listings_for_search(self, search_id: int, limit: int = 50, offset: int = 0,
                                blacklisted_agencies: list[str] | None = None,
                                filters: dict | None = None, sort: str = "found_at_desc") -> list[dict]:
        effective_filters = dict(filters) if filters else {}
        if blacklisted_agencies:
            effective_filters["blacklisted_agencies"] = blacklisted_agencies

        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                query = """SELECT l.*, sl.found_at
FROM listings l
JOIN search_listings sl ON sl.listing_id = l.listing_id
WHERE sl.search_id = %s"""
                params = [search_id]

                filter_clauses = self._build_filter_clauses(effective_filters, params)
                if filter_clauses:
                    query += " AND " + filter_clauses

                query += f" ORDER BY {self._build_order_clause(sort)} LIMIT %s OFFSET %s"
                params.extend([limit, offset])

                cur.execute(query, params)
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def count_listings_for_search(self, search_id: int, blacklisted_agencies: list[str] | None = None,
                                  filters: dict | None = None) -> int:
        effective_filters = dict(filters) if filters else {}
        if blacklisted_agencies:
            effective_filters["blacklisted_agencies"] = blacklisted_agencies

        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                query = (
                    "SELECT COUNT(*) AS cnt FROM search_listings sl"
                    " JOIN listings l ON l.listing_id = sl.listing_id WHERE sl.search_id = %s"
                )
                params = [search_id]

                filter_clauses = self._build_filter_clauses(effective_filters, params)
                if filter_clauses:
                    query += " AND " + filter_clauses

                cur.execute(query, params)
                return cur.fetchone()[0]
        finally:
            self._release_conn(conn)

    def get_filter_options(self, search_id: int) -> dict:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                result = {}
                for col in ("city", "district", "zip_code", "property_type", "agency", "epc", "ges"):
                    cur.execute(
                        f"""SELECT DISTINCT l.{col}
FROM listings l
JOIN search_listings sl ON sl.listing_id = l.listing_id
WHERE sl.search_id = %s AND l.{col} IS NOT NULL AND l.{col} != ''
ORDER BY l.{col}""",
                        (search_id,),
                    )
                    result[col] = [row[0] for row in cur.fetchall()]
                cur.execute(
                    """SELECT COUNT(*) FILTER (WHERE l.is_private = TRUE),
COUNT(*) FILTER (WHERE l.is_private = FALSE),
COUNT(*) FILTER (WHERE l.is_new = TRUE),
COUNT(*) FILTER (WHERE l.is_new = FALSE)
FROM listings l JOIN search_listings sl ON sl.listing_id = l.listing_id
WHERE sl.search_id = %s""",
                    (search_id,),
                )
                row = cur.fetchone()
                result["has_private"] = row[0] > 0
                result["has_non_private"] = row[1] > 0
                result["has_new"] = row[2] > 0
                result["has_not_new"] = row[3] > 0
                return result
        finally:
            self._release_conn(conn)

    def delete_old_listings(self, days: int = 4) -> int:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM listings WHERE first_seen < NOW() - make_interval(days => %s)",
                    (days,),
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

    def get_listing_detail(self, listing_id: str) -> dict | None:
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
