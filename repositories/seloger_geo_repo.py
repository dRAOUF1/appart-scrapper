"""SeLoger geo repository — persistent cache of INSEE code -> placeId.

Resolving a placeId means crawling SeLoger's own site (see
services/seloger_geocode.py), which is slow and anti-bot-guarded — once
found, it never changes, so it's cached here instead of a process-local
dict (unlike core.geocode's INSEE cache) so it survives restarts/deploys
and isn't re-crawled by every worker process.
"""

from __future__ import annotations

from repositories.base import BaseRepository


class SelogerGeoRepository(BaseRepository):
    def get_cached(self, insee_code: str) -> dict | None:
        """Row for this INSEE code, or None if never attempted.

        `place_id` is None when a previous crawl attempt failed (site
        changed, blocked by DataDome, city page not found) — a cached
        failure, not "not yet tried" (see resolve_place_id's retry cooldown).
        """
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT insee_code, place_id, resolved_at FROM seloger_place_ids WHERE insee_code = %s",
                    (insee_code,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._release_conn(conn)

    def set_cached(self, insee_code: str, place_id: str | None) -> None:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO seloger_place_ids (insee_code, place_id, resolved_at)
                       VALUES (%s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (insee_code) DO UPDATE
                           SET place_id = %s, resolved_at = CURRENT_TIMESTAMP""",
                    (insee_code, place_id, place_id),
                )
                conn.commit()
        finally:
            self._release_conn(conn)
