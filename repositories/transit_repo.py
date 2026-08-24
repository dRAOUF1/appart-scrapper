"""TransitRepository — référentiel des transports franciliens (issue #28).

Les tables `transit_lines` / `transit_stops` / `transit_line_stops` sont
remplies par scripts/import_transit.py (GTFS ÎDF Mobilités), JAMAIS par
l'application : ce repository ne fait que LIRE le référentiel et tenir le
cache des communes∩rayon (pattern `area_key`, succès-seuls, comme
repositories/commune_geo_repo.py dont la forme est reprise).

Le cache stocke, pour une station et un rayon donnés, les communes dont le
centre tombe dans le rayon — un résultat ne dépend que de données qui ne
bougent qu'à l'import suivant, d'où un cache en base plutôt qu'en mémoire :
il survit aux redémarrages et sert toutes les recherches.
"""

from __future__ import annotations

import json

from repositories.base import BaseRepository


class TransitRepository(BaseRepository):
    # ------------------------------------------------------------------
    # Référentiel lignes/stations
    # ------------------------------------------------------------------

    def search_lines(self, query: str, mode: str | None = None, limit: int = 10) -> list[dict]:
        """Lignes dont le code ou le nom contient `query`, triées par mode
        puis code (métros d'abord : c'est l'ordre du formulaire)."""
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT id, mode, code_ligne, nom_ligne
                    FROM transit_lines
                    WHERE (code_ligne ILIKE %s OR nom_ligne ILIKE %s)
                      AND (%s IS NULL OR mode = %s)
                    ORDER BY mode, code_ligne
                    LIMIT %s
                    """,
                    (f"%{query}%", f"%{query}%", mode, mode, limit),
                )
                return [dict(row) for row in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def get_line(self, line_id: str) -> dict | None:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT id, mode, code_ligne, nom_ligne FROM transit_lines WHERE id = %s",
                    (line_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._release_conn(conn)

    def get_line_stops(self, line_id: str) -> list[dict]:
        """Toutes les stations commerciales d'une ligne, par ordre alphabétique."""
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT s.id, s.nom, s.lat, s.lon
                    FROM transit_line_stops ls
                    JOIN transit_stops s ON s.id = ls.stop_id
                    WHERE ls.line_id = %s
                    ORDER BY s.nom, s.id
                    """,
                    (line_id,),
                )
                return [dict(row) for row in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def get_stops(self, stop_ids: list[str]) -> list[dict]:
        """Stations par identifiants, ordre alphabétique (l'ordre demandé par
        l'utilisateur n'est pas significatif : l'expansion trie elle-même)."""
        if not stop_ids:
            return []
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT id, nom, lat, lon FROM transit_stops WHERE id = ANY(%s)"
                    " ORDER BY nom, id",
                    (list(stop_ids),),
                )
                return [dict(row) for row in cur.fetchall()]
        finally:
            self._release_conn(conn)

    # ------------------------------------------------------------------
    # Cache communes ∩ rayon (« station:<stop_id>:<rayon>m »)
    # ------------------------------------------------------------------

    def get_communes_cache(self, area_key: str) -> list[dict] | None:
        """Les communes mémorisées pour ce périmètre, None si jamais calculé.

        Un cache VIDE ([] stocké) est un succès légitime retourné tel quel :
        aucune commune dans le rayon ≠ « pas encore calculé ».
        """
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    "SELECT communes FROM transit_communes_rayon WHERE area_key = %s",
                    (area_key,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                communes = row["communes"]
                if isinstance(communes, str):
                    communes = json.loads(communes)
                return communes if isinstance(communes, list) else []
        finally:
            self._release_conn(conn)

    def set_communes_cache(self, area_key: str, communes: list[dict]) -> None:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO transit_communes_rayon (area_key, communes, resolved_at)
                       VALUES (%s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (area_key) DO UPDATE
                           SET communes = %s, resolved_at = CURRENT_TIMESTAMP""",
                    (area_key, json.dumps(communes, ensure_ascii=False), json.dumps(communes, ensure_ascii=False)),
                )
                conn.commit()
        finally:
            self._release_conn(conn)
