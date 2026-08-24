"""MapPin repository — CRUD des repères personnels, strictement scoping user.

Issue #26 : les repères sont GLOBAUX (une ligne = un utilisateur + un point),
mais JAMAIS visibles ni modifiables par un autre utilisateur. Toutes les
méthodes prennent donc `user_id` et l'appliquent dans le WHERE même quand la
cible est désignée par son `id` : une route qui oublie le scoping ne peut pas
exposer la ligne d'autrui (défense en profondeur contre l'IDOR).
"""

from __future__ import annotations

from repositories.base import BaseRepository

# Colonnes mutables via update() — allowlist stricte : jamais de nom de
# colonne venant de la requête dans le SQL.
_UPDATABLE = ("label", "note", "icon")


class MapPinRepository(BaseRepository):
    """CRUD des repères personnels (`map_pins`)."""

    def create(self, user_id: int, label: str, latitude: float,
               longitude: float, note: str = "", icon: str = "📍") -> dict:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    """INSERT INTO map_pins (user_id, label, note, icon, latitude, longitude)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       RETURNING id, user_id, label, note, icon, latitude, longitude, created_at""",
                    (user_id, label, note, icon, latitude, longitude),
                )
                pin = dict(cur.fetchone())
                conn.commit()
                return pin
        except Exception:
            conn.rollback()
            raise
        finally:
            self._release_conn(conn)

    def list_for_user(self, user_id: int) -> list[dict]:
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    """SELECT id, user_id, label, note, icon, latitude, longitude, created_at
                       FROM map_pins WHERE user_id = %s ORDER BY created_at DESC, id DESC""",
                    (user_id,),
                )
                return [dict(row) for row in cur.fetchall()]
        finally:
            self._release_conn(conn)

    def get_owned(self, user_id: int, pin_id: int) -> dict | None:
        """Le repère de CET utilisateur, ou None — y compris si la ligne
        existe mais appartient à quelqu'un d'autre (la route répond 404)."""
        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    """SELECT id, user_id, label, note, icon, latitude, longitude, created_at
                       FROM map_pins WHERE id = %s AND user_id = %s""",
                    (pin_id, user_id),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            self._release_conn(conn)

    def update(self, user_id: int, pin_id: int, fields: dict) -> dict | None:
        """Met à jour les champs autorisés (label/note/icon) du repère DE cet
        utilisateur. Retourne la ligne à jour, ou None si introuvable/hors
        périmètre."""
        payload = {k: fields[k] for k in _UPDATABLE if k in fields}
        if not payload:
            return self.get_owned(user_id, pin_id)

        assignments = ", ".join(f"{col} = %s" for col in payload)
        params = list(payload.values()) + [pin_id, user_id]

        conn = self._get_conn_for_request()
        try:
            with self._dict_cursor(conn) as cur:
                cur.execute(
                    f"""UPDATE map_pins SET {assignments}
                        WHERE id = %s AND user_id = %s
                        RETURNING id, user_id, label, note, icon, latitude, longitude, created_at""",
                    params,
                )
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return None
                pin = dict(row)
                conn.commit()
                return pin
        except Exception:
            conn.rollback()
            raise
        finally:
            self._release_conn(conn)

    def delete(self, user_id: int, pin_id: int) -> bool:
        """Supprime LE repère de cet utilisateur ; False si absent ou d'un
        autre utilisateur (le WHERE porte les deux conditions)."""
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM map_pins WHERE id = %s AND user_id = %s",
                    (pin_id, user_id),
                )
                deleted = cur.rowcount > 0
                conn.commit()
                return deleted
        finally:
            self._release_conn(conn)
