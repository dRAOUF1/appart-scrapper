"""SeLoger geo repository — cache persistant périmètre -> placeId.

Résoudre un placeId demande d'interroger le site de SeLoger (voir
services/seloger_geocode.py), ce qui est lent et protégé contre les robots —
une fois trouvé il ne change plus, d'où ce cache en base plutôt qu'un dict
local au process (contrairement au cache INSEE de core.geocode) : il survit
aux redémarrages et aux déploiements, et n'est pas reconstruit par chaque
worker.

La clé (`area_key`) identifie un périmètre à n'importe quel niveau — code
INSEE de commune, "city:<insee>", "dept:<code>", "region:<code>" — voir
services.seloger_geocode.area_cache_key.
"""

from __future__ import annotations

from repositories.base import BaseRepository


class SelogerGeoRepository(BaseRepository):
    def get_cached(self, area_key: str) -> dict | None:
        """La ligne de ce périmètre, ou None s'il n'a jamais été tenté.

        `place_id` vaut None quand une tentative précédente a échoué (site
        modifié, blocage anti-bot, page introuvable) — un échec mémorisé, à
        distinguer de « pas encore essayé » (voir le délai de réessai dans
        resolve_place_id).
        """
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT area_key, place_id, resolved_at FROM seloger_place_ids WHERE area_key = %s",
                    (area_key,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._release_conn(conn)

    def set_cached(self, area_key: str, place_id: str | None) -> None:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO seloger_place_ids (area_key, place_id, resolved_at)
                       VALUES (%s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (area_key) DO UPDATE
                           SET place_id = %s, resolved_at = CURRENT_TIMESTAMP""",
                    (area_key, place_id, place_id),
                )
                conn.commit()
        finally:
            self._release_conn(conn)
