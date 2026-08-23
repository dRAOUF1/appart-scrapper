"""Foncia geo repository — cache persistant périmètre -> slug de localité.

Résoudre le slug d'une localité Foncia demande d'interroger l'API géo du site
(voir services/foncia_geocode.py), une requête réseau qu'on ne veut pas refaire
à chaque scrape : une fois trouvée, la correspondance ne change plus, d'où ce
cache en base plutôt qu'un dict local au process — même raison que
repositories/orpi_geo_repo.py, dont ce module reprend exactement la forme.

La clé (`area_key`) identifie un périmètre à n'importe quel niveau — code
INSEE de commune, "city:<insee>", "city_name:<ville>", "dept:<code>",
"region:<code>" — voir services.foncia_geocode.area_cache_key (même
convention que SeLoger, bienici, Century 21, PAP et Orpi).

Le slug (`slug_id`) est le slug de localité Foncia renvoyé par son API géo
(`vannes-56000`, `haute-garonne-31`, `occitanie`). C'est une valeur unique,
comme le slug_id d'Orpi ou de Century 21, pas une liste comme les zoneIds de
bienici.
"""

from __future__ import annotations

from repositories.base import BaseRepository


class FonciaGeoRepository(BaseRepository):
    def get_cached(self, area_key: str) -> dict | None:
        """La ligne de ce périmètre, ou None s'il n'a jamais été tenté.

        `slug_id` vaut None quand une tentative précédente a échoué (endpoint
        modifié, blocage, localisation inconnue) — un échec mémorisé, à
        distinguer de « pas encore essayé » (voir le délai de réessai dans
        resolve_slug_id)."""
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT area_key, slug_id, resolved_at FROM foncia_geo_ids WHERE area_key = %s",
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
                    """INSERT INTO foncia_geo_ids (area_key, slug_id, resolved_at)
                       VALUES (%s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (area_key) DO UPDATE
                           SET slug_id = %s, resolved_at = CURRENT_TIMESTAMP""",
                    (area_key, slug_id, slug_id),
                )
                conn.commit()
        finally:
            self._release_conn(conn)
