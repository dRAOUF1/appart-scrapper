"""Scrape log repository — CRUD for scrape execution logs."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import psycopg2.extras

from repositories.base import BaseRepository


class ScrapeLogRepository(BaseRepository):
    """Scrape log CRUD operations."""

    def create_scrape_log(self, search_id: int, status: str, listings_found: int = 0, new_listings: int = 0, error_message: str = "", details: dict | None = None, started_at=None) -> int:
        now = started_at or datetime.utcnow()
        completed_at = datetime.utcnow()
        duration = (completed_at - now).total_seconds() if started_at else 0
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    """INSERT INTO scrape_logs
                       (search_id, started_at, completed_at, status, listings_found, new_listings, error_message, details, duration_sec)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (search_id, now, completed_at, status, listings_found, new_listings, error_message, json.dumps(details or {}), duration),
                )
                row = cur.fetchone()
                conn.commit()
                return row["id"]
        finally:
            self._release_conn(conn)

    def get_scrape_logs(self, search_id: int, limit: int = 50, offset: int = 0, status_filter: str = "") -> list[dict]:
        query = "SELECT * FROM scrape_logs WHERE search_id = %s"
        params = [search_id]
        if status_filter:
            query += " AND status = %s"
            params.append(status_filter)
        query += " ORDER BY started_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(query, params)
                result = []
                for r in cur.fetchall():
                    d = dict(r)
                    self._parse_json_column(d, "details")
                    result.append(d)
                return result
        finally:
            self._release_conn(conn)

    def count_scrape_logs(self, search_id: int, status_filter: str = "") -> int:
        query = "SELECT COUNT(*) AS cnt FROM scrape_logs WHERE search_id = %s"
        params = [search_id]
        if status_filter:
            query += " AND status = %s"
            params.append(status_filter)
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchone()[0]
        finally:
            self._release_conn(conn)

    def get_scrape_stats(self, search_id: int) -> dict:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    """SELECT COUNT(*) AS total,
                              COUNT(*) FILTER (WHERE status = 'success') AS success_count,
                              COUNT(*) FILTER (WHERE status = 'error') AS error_count,
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
        finally:
            self._release_conn(conn)

    def update_scrape_log_raw(self, log_id: int, raw_logs: str) -> bool:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE scrape_logs SET raw_logs = %s WHERE id = %s",
                    (raw_logs, log_id),
                )
                conn.commit()
                return cur.rowcount > 0
        finally:
            self._release_conn(conn)

    def get_scrape_log_raw(self, log_id: int, user_id: int | None = None) -> dict | None:
        query = "SELECT id, search_id, status, started_at, completed_at, raw_logs FROM scrape_logs WHERE id = %s"
        params = [log_id]
        if user_id is not None:
            query += " AND search_id IN (SELECT id FROM searches WHERE user_id = %s)"
            params.append(user_id)
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(query, params)
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._release_conn(conn)

    def get_latest_scrape_log_id(self, search_id: int) -> int | None:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM scrape_logs WHERE search_id = %s ORDER BY started_at DESC LIMIT 1",
                    (search_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            self._release_conn(conn)
