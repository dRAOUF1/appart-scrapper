"""Search repository — CRUD for searches."""

from __future__ import annotations

import json
from typing import Optional

from repositories.base import BaseRepository


class SearchRepository(BaseRepository):
    """Search CRUD operations."""

    def _load_criteria(self, d: dict) -> dict:
        """Parse la colonne JSON `criteria` puis la ramène au vocabulaire
        canonique (core.criteria).

        C'est le point unique de normalisation à la lecture : les recherches
        créées avant l'unification sont stockées dans l'ancien vocabulaire
        (celui de SeLoger : distributionTypes/estateTypes/placeIds) et ne
        sont volontairement PAS migrées en base. Tout ce qui lit une
        recherche — scraper, reconstruction d'URL, formulaire d'édition,
        admin — passe par ici et ne voit donc que du canonique.
        """
        from core.criteria import normalize_criteria

        self._parse_json_column(d, "criteria")
        d["criteria"] = normalize_criteria(d.get("criteria"))
        return d

    @staticmethod
    def _normalize_sources(d: dict) -> dict:
        """Rows created before multi-source support have `sources IS NULL`
        (the DDL backfill covers existing rows, but stay defensive here too)."""
        if isinstance(d.get("sources"), str):
            try:
                d["sources"] = json.loads(d["sources"])
            except (json.JSONDecodeError, ValueError):
                d["sources"] = None
        if not d.get("sources"):
            d["sources"] = [d.get("source", "seloger")]
        return d

    def create_search(self, user_id: int, label: str, ntfy_topic: str, source: str = "seloger", criteria: dict | None = None, scrape_interval: int = 5, is_active: bool = True, sources: list[str] | None = None) -> dict:
        criteria_json = json.dumps(criteria or {})
        sources = sources or [source]
        sources_json = json.dumps(sources)
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "INSERT INTO searches (user_id, label, ntfy_topic, source, criteria, scrape_interval, is_active, sources) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (user_id, label, ntfy_topic, source, criteria_json, scrape_interval, is_active, sources_json),
                )
                row = cur.fetchone()
                conn.commit()
                return {
                    "id": row["id"],
                    "user_id": user_id,
                    "label": label,
                    "ntfy_topic": ntfy_topic,
                    "source": source,
                    "sources": sources,
                    "criteria": criteria or {},
                    "scrape_interval": scrape_interval,
                    "is_active": is_active,
                }
        finally:
            self._release_conn(conn)

    def update_search_criteria(self, search_id: int, criteria: dict) -> bool:
        criteria_json = json.dumps(criteria)
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE searches SET criteria = %s WHERE id = %s",
                    (criteria_json, search_id),
                )
                conn.commit()
                return cur.rowcount > 0
        finally:
            self._release_conn(conn)

    def update_search(self, search_id: int, user_id: int, label: str | None = None, ntfy_topic: str | None = None, criteria: dict | None = None, scrape_interval: int | None = None, is_active: bool | None = None, sources: list[str] | None = None) -> bool:
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
        if is_active is not None:
            fields.append("is_active = %s")
            params.append(is_active)
        if sources is not None:
            fields.append("sources = %s")
            params.append(json.dumps(sources))
            fields.append("source = %s")
            params.append(sources[0] if sources else "seloger")
        if not fields:
            return False
        params.extend([search_id, user_id])
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE searches SET {', '.join(fields)} WHERE id = %s AND user_id = %s",
                    params,
                )
                conn.commit()
                return cur.rowcount > 0
        finally:
            self._release_conn(conn)

    def update_scrape_interval(self, search_id: int, interval_minutes: int) -> bool:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE searches SET scrape_interval = %s WHERE id = %s",
                    (interval_minutes, search_id),
                )
                conn.commit()
                return cur.rowcount > 0
        finally:
            self._release_conn(conn)

    def update_last_scraped(self, search_id: int) -> bool:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE searches SET last_scraped = CURRENT_TIMESTAMP WHERE id = %s",
                    (search_id,),
                )
                conn.commit()
                return cur.rowcount > 0
        finally:
            self._release_conn(conn)

    def get_user_searches(self, user_id: int) -> list[dict]:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    """SELECT s.id, s.label, s.ntfy_topic, s.source, s.sources, s.criteria,
                              s.scrape_interval, s.last_scraped, s.created_at, s.is_active,
                              s.blacklisted_agencies, s.blacklist_mode,
                              (SELECT COUNT(*) FROM search_listings WHERE search_id = s.id) AS listing_count
                       FROM searches s
                       WHERE s.user_id = %s
                       ORDER BY s.created_at DESC""",
                    (user_id,),
                )
                result = []
                for r in cur.fetchall():
                    d = dict(r)
                    self._load_criteria(d)
                    self._normalize_sources(d)
                    result.append(d)
                return result
        finally:
            self._release_conn(conn)

    def get_search(self, search_id: int) -> Optional[dict]:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT id, user_id, label, ntfy_topic, source, sources, criteria, scrape_interval, last_scraped, created_at, is_active, blacklisted_agencies, blacklist_mode FROM searches WHERE id = %s",
                    (search_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                d = dict(row)
                self._load_criteria(d)
                self._normalize_sources(d)
                return d
        finally:
            self._release_conn(conn)

    def delete_search(self, search_id: int) -> bool:
        from scrape_logs.storage import delete_search_logs
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM searches WHERE id = %s", (search_id,))
                conn.commit()
                deleted = cur.rowcount > 0
                if deleted:
                    delete_search_logs(search_id)
                return deleted
        finally:
            self._release_conn(conn)

    def toggle_search_active(self, search_id: int) -> bool | None:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "UPDATE searches SET is_active = NOT is_active WHERE id = %s RETURNING is_active",
                    (search_id,),
                )
                row = cur.fetchone()
                conn.commit()
                return row["is_active"] if row else None
        finally:
            self._release_conn(conn)

    def get_all_searches(self, user_filter="", source_filter="") -> list[dict]:
        query = """SELECT s.id, s.label, s.ntfy_topic, s.source, s.sources, s.criteria,
                          s.scrape_interval, s.last_scraped, s.created_at, s.is_active,
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

        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(query, params)
                result = []
                for r in cur.fetchall():
                    d = dict(r)
                    self._load_criteria(d)
                    self._normalize_sources(d)
                    result.append(d)
                return result
        finally:
            self._release_conn(conn)

    def get_search_detail(self, search_id: int) -> Optional[dict]:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
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
                self._load_criteria(result)
                self._normalize_sources(result)

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
        finally:
            self._release_conn(conn)

    def update_blacklisted_agencies(self, search_id: int, agencies: list[str]) -> bool:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE searches SET blacklisted_agencies = %s WHERE id = %s",
                    (agencies, search_id),
                )
                conn.commit()
                return cur.rowcount > 0
        finally:
            self._release_conn(conn)

    def update_blacklist_mode(self, search_id: int, mode: str) -> bool:
        if mode not in ("exclude", "no_notify"):
            return False
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE searches SET blacklist_mode = %s WHERE id = %s",
                    (mode, search_id),
                )
                conn.commit()
                return cur.rowcount > 0
        finally:
            self._release_conn(conn)
