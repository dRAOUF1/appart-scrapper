"""BienIci geo repository — cache persistant périmètre -> zoneIds.

Résoudre des zoneIds demande d'interroger res.bienici.com (voir
services/bienici_geocode.py) : une fois trouvés ils ne changent plus, d'où ce
cache en base plutôt qu'un dict local au process — même raison que
repositories/seloger_geo_repo.py, dont ce module reprend exactement la forme.

La clé (`area_key`) identifie un périmètre à n'importe quel niveau — code
INSEE de commune, "city:<insee>", "dept:<code>", "region:<code>" — voir
services.bienici_geocode.area_cache_key (même convention que SeLoger).

`zone_ids` est stocké en JSON (bienici peut renvoyer plusieurs zoneIds pour
un même périmètre, ex. les alias-department qui combinent plusieurs zones),
contrairement au `place_id` unique de SeLoger.
"""

from __future__ import annotations

import json

from repositories.base import BaseRepository


class BienIciGeoRepository(BaseRepository):
    def get_cached(self, area_key: str) -> dict | None:
        """La ligne de ce périmètre, ou None s'il n'a jamais été tenté.

        `zone_ids` vaut None quand une tentative précédente a échoué — un
        échec mémorisé, à distinguer de « pas encore essayé » (voir le délai
        de réessai dans resolve_zone_ids)."""
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT area_key, zone_ids, resolved_at FROM bienici_zone_ids WHERE area_key = %s",
                    (area_key,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                row = dict(row)
                row["zone_ids"] = json.loads(row["zone_ids"]) if row["zone_ids"] else None
                return row
        finally:
            self._release_conn(conn)

    def set_cached(self, area_key: str, zone_ids: list[str] | None) -> None:
        conn = self._get_conn_for_request()
        try:
            encoded = json.dumps(zone_ids) if zone_ids else None
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO bienici_zone_ids (area_key, zone_ids, resolved_at)
                       VALUES (%s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (area_key) DO UPDATE
                           SET zone_ids = %s, resolved_at = CURRENT_TIMESTAMP""",
                    (area_key, encoded, encoded),
                )
                conn.commit()
        finally:
            self._release_conn(conn)
