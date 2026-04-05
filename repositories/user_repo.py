"""User repository — CRUD for users."""

from __future__ import annotations

import secrets
from typing import Optional

import psycopg2
import psycopg2.extras

from repositories.base import BaseRepository


class UserRepository(BaseRepository):
    """User CRUD operations."""

    def create_user(self, username: str) -> dict:
        api_token = secrets.token_urlsafe(32)
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "INSERT INTO users (username, api_token) VALUES (%s, %s) RETURNING id",
                    (username, api_token),
                )
                row = cur.fetchone()
                conn.commit()
                return {
                    "id": row["id"],
                    "username": username,
                    "api_token": api_token,
                }
        except psycopg2.IntegrityError:
            conn.rollback()
            raise ValueError(f"Le nom d'utilisateur '{username}' est déjà pris")
        finally:
            self._release_conn(conn)

    def get_user_by_token(self, token: str) -> Optional[dict]:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT id, username, api_token, created_at FROM users WHERE api_token = %s",
                    (token,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._release_conn(conn)

    def get_user_by_username(self, username: str) -> Optional[dict]:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT id, username, api_token, created_at FROM users WHERE username = %s",
                    (username,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._release_conn(conn)

    def get_all_users(self) -> list[dict]:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    """SELECT u.id, u.username, u.api_token, u.created_at,
                              (SELECT COUNT(*) FROM searches WHERE user_id = u.id) AS search_count,
                              (SELECT COUNT(DISTINCT sl.listing_id)
                               FROM search_listings sl
                               JOIN searches s ON s.id = sl.search_id
                               WHERE s.user_id = u.id) AS listing_count
                       FROM users u
                       ORDER BY u.created_at DESC"""
                )
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def get_user_detail(self, user_id: int) -> Optional[dict]:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
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
        finally:
            self._release_conn(conn)

    def delete_user(self, user_id: int) -> bool:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
                conn.commit()
                return cur.rowcount > 0
        finally:
            self._release_conn(conn)

    def reset_user_token(self, user_id: int) -> str:
        new_token = secrets.token_urlsafe(32)
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET api_token = %s WHERE id = %s",
                    (new_token, user_id),
                )
                conn.commit()
            return new_token
        finally:
            self._release_conn(conn)

    def get_user_stats(self, user_id: int) -> dict:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute("""
                    SELECT
                        (SELECT COUNT(*) FROM searches WHERE user_id = %s) AS searches,
                        (SELECT COUNT(DISTINCT sl.listing_id)
                         FROM search_listings sl JOIN searches s ON s.id = sl.search_id
                         WHERE s.user_id = %s) AS total_listings,
                        (SELECT COUNT(DISTINCT sl.listing_id)
                         FROM search_listings sl JOIN searches s ON s.id = sl.search_id
                         WHERE s.user_id = %s AND sl.found_at >= CURRENT_DATE) AS new_today
                """, (user_id, user_id, user_id))
                row = cur.fetchone()
                return {
                    "searches": row["searches"],
                    "total_listings": row["total_listings"],
                    "new_today": row["new_today"],
                }
        finally:
            self._release_conn(conn)

    def get_dashboard_data(self, user_id: int) -> dict:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute("""
                    SELECT
                        (SELECT COUNT(*) FROM searches WHERE user_id = %s) AS searches,
                        (SELECT COUNT(DISTINCT sl.listing_id)
                         FROM search_listings sl JOIN searches s ON s.id = sl.search_id
                         WHERE s.user_id = %s) AS total_listings,
                        (SELECT COUNT(DISTINCT sl.listing_id)
                         FROM search_listings sl JOIN searches s ON s.id = sl.search_id
                         WHERE s.user_id = %s AND sl.found_at >= CURRENT_DATE) AS new_today
                """, (user_id, user_id, user_id))
                stats = dict(cur.fetchone())

                cur.execute("""
                    SELECT s.id, s.label, s.ntfy_topic, s.source, s.criteria,
                           s.scrape_interval, s.last_scraped, s.created_at, s.is_active,
                           (SELECT COUNT(*) FROM search_listings WHERE search_id = s.id) AS listing_count
                    FROM searches s
                    WHERE s.user_id = %s
                    ORDER BY s.created_at DESC
                """, (user_id,))
                searches = []
                for r in cur.fetchall():
                    d = dict(r)
                    self._parse_json_column(d, "criteria")
                    searches.append(d)

                cur.execute("""
                    SELECT l.listing_id, l.title, l.price, l.surface, l.rooms,
                           l.location, l.url, l.image_url, l.agency, l.city,
                           sl.found_at, s.label AS search_label
                    FROM search_listings sl
                    JOIN searches s ON s.id = sl.search_id
                    JOIN listings l ON l.listing_id = sl.listing_id
                    WHERE s.user_id = %s
                    ORDER BY sl.found_at DESC LIMIT 10
                """, (user_id,))
                recent = [dict(r) for r in cur.fetchall()]

            return {"stats": stats, "searches": searches, "recent": recent}
        finally:
            self._release_conn(conn)
