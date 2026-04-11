"""Base repository — shared DB connection management."""

from __future__ import annotations

import json
import threading
from typing import Optional

import psycopg2
import psycopg2.extras
from loguru import logger


class BaseRepository:
    """Shared connection management for all repositories.

    Thread-safe: one connection per thread, with Flask request
    connection sharing via g._db_conn.
    """

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._local = threading.local()

    def _get_conn(self):
        """Create a new connection (recommended for pgBouncer transaction mode)."""
        conn = psycopg2.connect(self.database_url, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '30000'")
        cur.close()
        return conn

    def _get_conn_for_request(self):
        """Return shared Flask request connection or create new one."""
        try:
            from flask import g
            if hasattr(g, '_db_conn') and g._db_conn is not None:
                return g._db_conn
        except Exception:
            pass
        return self._get_conn()

    def _get_ddl_conn(self):
        """Create a connection WITHOUT statement_timeout for DDL operations."""
        conn = psycopg2.connect(self.database_url, connect_timeout=30)
        cur = conn.cursor()
        cur.execute("SET lock_timeout = '60000'")
        cur.close()
        return conn

    def _release_conn(self, conn):
        """Only close if NOT the shared Flask request connection."""
        try:
            from flask import g
            if hasattr(g, '_db_conn') and g._db_conn is conn:
                return
        except Exception:
            pass
        self._close_conn(conn)

    def _close_conn(self, conn):
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
