"""Base repository — shared DB connection management."""

from __future__ import annotations

import json
import threading

import psycopg2
import psycopg2.extensions
import psycopg2.extras
import psycopg2.pool


class BaseRepository:
    """Shared connection management for all repositories.

    Connections are borrowed from a process-wide pool (one pool per
    database_url, shared across all repository instances) instead of opening
    a new TCP connection per request. Flask requests share one borrowed
    connection via g._db_conn for the request's lifetime.
    """

    _pools: dict[str, psycopg2.pool.ThreadedConnectionPool] = {}
    _pools_lock = threading.Lock()

    def __init__(self, database_url: str):
        self.database_url = database_url

    def _get_pool(self) -> psycopg2.pool.ThreadedConnectionPool:
        pool = BaseRepository._pools.get(self.database_url)
        if pool is None:
            with BaseRepository._pools_lock:
                pool = BaseRepository._pools.get(self.database_url)
                if pool is None:
                    pool = psycopg2.pool.ThreadedConnectionPool(
                        1, 20, dsn=self.database_url, connect_timeout=10,
                    )
                    BaseRepository._pools[self.database_url] = pool
        return pool

    def _get_conn(self):
        """Borrow a connection from the shared pool."""
        conn = self._get_pool().getconn()
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '30000'")
        cur.close()
        return conn

    def _get_conn_for_request(self):
        """Return shared Flask request connection or borrow a new one."""
        try:
            from flask import g
            if hasattr(g, '_db_conn') and g._db_conn is not None:
                return g._db_conn
        except Exception:
            pass
        return self._get_conn()

    def _get_ddl_conn(self):
        """Standalone connection outside the pool, for one-off DDL at startup."""
        conn = psycopg2.connect(self.database_url, connect_timeout=30)
        cur = conn.cursor()
        cur.execute("SET lock_timeout = '60000'")
        cur.close()
        return conn

    def _release_conn(self, conn):
        """Return a borrowed connection to the pool, unless it's still the
        shared Flask request connection (released at request teardown instead).

        Also clears a failed transaction left behind by a write method that
        raised before its own commit/rollback (e.g. an uncaught IntegrityError
        variant). Without this, a connection could be returned to the pool
        mid-aborted-transaction and poison whichever request borrows it next.
        """
        try:
            from flask import g
            if hasattr(g, '_db_conn') and g._db_conn is conn:
                return
        except Exception:
            pass
        self.release_to_pool(conn)

    def release_to_pool(self, conn):
        """Roll back if needed and return conn to the shared pool."""
        if conn is None:
            return
        try:
            if conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_INERROR:
                conn.rollback()
        except Exception:
            pass
        try:
            self._get_pool().putconn(conn)
        except Exception:
            self._close_conn(conn)

    def _close_conn(self, conn):
        """Hard-close a standalone (non-pooled) connection, e.g. from _get_ddl_conn."""
        if conn is not None:
            try:
                if not conn.closed:
                    conn.close()
            except Exception:
                pass

    def _parse_json_column(self, row: dict, column: str) -> dict:
        """Parse a JSONB column if it's a string."""
        val = row.get(column)
        if isinstance(val, str):
            try:
                row[column] = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                row[column] = {}
        return row

    def _dict_cursor(self, conn):
        """Return a RealDictCursor for the connection."""
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def statistiques_pool(database_url: str) -> dict | None:
    """État du pool partagé de CE process — lecture pure (issue #21).

    Lit les compteurs internes du ThreadedConnectionPool psycopg2 sous son
    propre verrou, sans modifier aucun comportement d'emprunt/retour :
      - `libres` : connexions ouvertes disponibles dans le pool ;
      - `utilisees` : connexions actuellement empruntées.

    Retourne None si le pool n'existe pas encore pour cette URL (aucun
    emprunt depuis le démarrage) ou si la structure interne du driver a
    changé — l'appelant affiche alors un état neutre, jamais une erreur.
    """
    pool = BaseRepository._pools.get(database_url)
    if pool is None:
        return None
    try:
        with pool._lock:
            return {
                "min": pool.minconn,
                "max": pool.maxconn,
                "libres": len(pool._pool),
                "utilisees": len(pool._used),
            }
    except Exception:
        return None
