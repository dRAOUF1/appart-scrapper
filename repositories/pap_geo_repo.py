"""PAP geo repository — cache persistant périmètre -> identifiant numérique.

Résoudre l'identifiant de lieu d'un périmètre demande d'interroger
l'autocomplete de pap.fr (voir services/pap_geocode.py), une requête réseau
qu'on ne veut pas refaire à chaque scrape : une fois trouvé, l'identifiant ne
change plus, d'où ce cache en base plutôt qu'un dict local au process — même
raison que repositories/century21_geo_repo.py, dont ce module reprend
exactement la forme.

La clé (`area_key`) identifie un périmètre à n'importe quel niveau — code
INSEE de commune, "city:<insee>", "city_name:<ville>", "dept:<code>",
"region:<code>" — voir services.pap_geocode.area_cache_key (même convention
que SeLoger, bienici et Century 21).

L'identifiant (`geo_id`) est l'entier opaque propre à pap.fr renvoyé par son
autocomplete (`439` pour Paris, `37782` pour Paris 15e, `397` pour la
Gironde). C'est une valeur unique, comme le place_id de SeLoger ou le slug_id
de Century 21, pas une liste comme les zoneIds de bienici.
"""

from __future__ import annotations

from repositories.base import BaseRepository


class PapGeoRepository(BaseRepository):
    def get_cached(self, area_key: str) -> dict | None:
        """La ligne de ce périmètre, ou None s'il n'a jamais été tenté.

        `geo_id` vaut None quand une tentative précédente a échoué (endpoint
        modifié, localisation inconnue) — un échec mémorisé, à distinguer de
        « pas encore essayé » (voir le délai de réessai dans resolve_geo_id)."""
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT area_key, geo_id, resolved_at FROM pap_geo_ids WHERE area_key = %s",
                    (area_key,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._release_conn(conn)

    def set_cached(self, area_key: str, geo_id: str | None) -> None:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO pap_geo_ids (area_key, geo_id, resolved_at)
                       VALUES (%s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (area_key) DO UPDATE
                           SET geo_id = %s, resolved_at = CURRENT_TIMESTAMP""",
                    (area_key, geo_id, geo_id),
                )
                conn.commit()
        finally:
            self._release_conn(conn)
