"""CommuneGeo repository — cache des centres de communes (issue #26).

Le fallback géocodage (services/geocode_commune.py) interroge geo.api.gouv.fr
pour situer une annonce restée sans coordonnées : une fois connu, le centre
d'une commune ne change plus, d'où ce cache en base plutôt qu'un dict local au
process — même raison que repositories/bienici_geo_repo.py, dont ce module
reprend exactement la forme.

La clé (`area_key`) vaut « postal:<cp> » — un code postal est la seule
localisation fiable dont dispose une annonce sans coordonnées (voir
services.geocode_commune.area_cache_key). Contrairement aux caches de
résolution par source, seuls les SUCCÈS sont stockés ici : un CP non résolu
(code postal inexistant, API momentanément down) doit pouvoir réussir au
scrape suivant, sans délai de réessai.
"""

from __future__ import annotations

from repositories.base import BaseRepository


class CommuneGeoRepository(BaseRepository):
    def get_cached(self, area_key: str) -> dict | None:
        """Le centre mémorisé de ce périmètre, ou None s'il n'a jamais été
        résolu avec succès."""
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT area_key, latitude, longitude, resolved_at "
                    "FROM commune_centres WHERE area_key = %s",
                    (area_key,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._release_conn(conn)

    def set_cached(self, area_key: str, latitude: float, longitude: float) -> None:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO commune_centres (area_key, latitude, longitude, resolved_at)
                       VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (area_key) DO UPDATE
                           SET latitude = %s, longitude = %s, resolved_at = CURRENT_TIMESTAMP""",
                    (area_key, latitude, longitude, latitude, longitude),
                )
                conn.commit()
        finally:
            self._release_conn(conn)
