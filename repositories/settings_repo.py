"""Settings repository — app settings key-value store."""

from __future__ import annotations

from repositories.base import BaseRepository


class SettingsRepository(BaseRepository):
    """App settings CRUD operations."""

    def get_setting(self, key: str, default: str = "") -> str:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
                row = cur.fetchone()
                return row[0] if row else default
        finally:
            self._release_conn(conn)

    def set_setting(self, key: str, value: str) -> bool:
        conn = self._get_conn_for_request()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO app_settings (key, value) VALUES (%s, %s)
                       ON CONFLICT (key) DO UPDATE SET value = %s""",
                    (key, value, value),
                )
                conn.commit()
                return True
        finally:
            self._release_conn(conn)
