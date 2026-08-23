"""Guy Hoquet geo repository — cache persistant périmètre -> slug d'URL.

Résoudre le slug de localisation d'un périmètre demande d'interroger
l'autocomplete de guy-hoquet.com (voir services/guyhoquet_geocode.py),
une requête réseau qu'on ne veut pas refaire à chaque scrape : une fois
trouvé, le slug ne change plus, d'où ce cache en base plutôt qu'un dict
local au process — même raison que repositories/century21_geo_repo.py,
dont ce module reprend exactement la forme.

La clé (`area_key`) identifie un périmètre à n'importe quel niveau — code
INSEE de commune, "city:<insee>", "city_name:<ville>" — voir
services.guyhoquet_geocode.area_cache_key (même convention que SeLoger,
bienici et Century 21). Les départements et régions ne sont jamais mis en
cache : leur slug se dérive purement du code (`31_c2`, `76_c1`), sans
réseau.

Le slug (`slug_id`) est l'identifiant de localisation propre à Guy Hoquet
renvoyé par son autocomplete (`toulouse-31000_c3`, `lyon-69123_c4`,
`31_c2`, `76_c1`, ...). C'est une valeur unique, comme le placeId de
SeLoger, pas une liste comme les zoneIds de bienici.
"""

from __future__ import annotations

from repositories.base import BaseRepository


class GuyHoquetGeoRepository(BaseRepository):
    def get_cached(self, area_key: str) -> dict | None:
        """La ligne de ce périmètre, ou None s'il n'a jamais été tenté.

        `slug_id` vaut None quand une tentative précédente a échoué (endpoint
        modifié, blocage, localisation inconnue) — un échec mémorisé, à
        distinguer de « pas encore essayé » (voir le délai de réessai dans
        resolve_slug)."""
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT area_key, slug_id, resolved_at FROM guyhoquet_geo_ids WHERE area_key = %s",
                    (area_key,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._release_conn(conn)

    def set_cached(self, area_key: str, slug_id: str | None) -> None:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO guyhoquet_geo_ids (area_key, slug_id, resolved_at)
                       VALUES (%s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (area_key) DO UPDATE
                           SET slug_id = %s, resolved_at = CURRENT_TIMESTAMP""",
                    (area_key, slug_id, slug_id),
                )
                conn.commit()
        finally:
            self._release_conn(conn)
