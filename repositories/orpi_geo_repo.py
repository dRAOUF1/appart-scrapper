"""Orpi geo repository — cache persistant périmètre -> slug d'URL.

Résoudre le slug de localisation d'un périmètre demande d'interroger
l'autocomplete de orpi.com (voir services/orpi_geocode.py), une requête
réseau qu'on ne veut pas refaire à chaque scrape : une fois trouvé, le slug
ne change plus, d'où ce cache en base plutôt qu'un dict local au process —
même raison que repositories/seloger_geo_repo.py, dont ce module reprend
exactement la forme.

La clé (`area_key`) identifie un périmètre à n'importe quel niveau — code
INSEE de commune, "city:<insee>", "city_name:<ville>" — voir
services.orpi_geocode.area_cache_key (même convention que SeLoger, bienici,
Century 21 et PAP).

Le slug (`slug_id`) est l'identifiant d'URL propre à Orpi renvoyé par son
autocomplete (`rosny-sous-bois`, `gironde`, `ile-de-france`). C'est une
valeur unique, comme le place_id de SeLoger ou le slug_id de Century 21,
pas une liste comme les zoneIds de bienici.
"""

from __future__ import annotations

from repositories.base import BaseRepository


class OrpiGeoRepository(BaseRepository):
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
                    "SELECT area_key, slug_id, resolved_at FROM orpi_geo_ids WHERE area_key = %s",
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
                    """INSERT INTO orpi_geo_ids (area_key, slug_id, resolved_at)
                       VALUES (%s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (area_key) DO UPDATE
                           SET slug_id = %s, resolved_at = CURRENT_TIMESTAMP""",
                    (area_key, slug_id, slug_id),
                )
                conn.commit()
        finally:
            self._release_conn(conn)
