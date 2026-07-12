"""Integration: the real DDL migrations must run cleanly on a fresh DB.

Regression coverage for the DDL-ordering bug fixed this session: on a
brand-new database, CREATE INDEX idx_searches_user_active(user_id, is_active)
used to run before the ALTER TABLE that added is_active, which failed
outright. This module's `storage` fixture already runs the real migrations
(see conftest.py::_bootstrap_schema) — if that setup succeeded, the ordering
fix is proven; these tests additionally check idempotency and schema shape.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


EXPECTED_TABLES = {
    "users", "searches", "listings", "search_listings", "admin_logs", "app_settings",
}


def test_all_expected_tables_exist(storage):
    conn = storage._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
            """)
            tables = {row[0] for row in cur.fetchall()}
    finally:
        storage._release_conn(conn)

    assert EXPECTED_TABLES.issubset(tables)


def test_searches_table_has_is_active_column(storage):
    """Directly validates the fixed ordering: is_active must exist (the
    index on it was previously created before this column on fresh DBs)."""
    conn = storage._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'searches' AND column_name = 'is_active'
            """)
            row = cur.fetchone()
    finally:
        storage._release_conn(conn)

    assert row is not None


def test_search_listings_has_notified_column_defaulting_true(storage):
    conn = storage._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_default FROM information_schema.columns
                WHERE table_name = 'search_listings' AND column_name = 'notified'
            """)
            row = cur.fetchone()
    finally:
        storage._release_conn(conn)

    assert row is not None
    assert "true" in row[0].lower()


def test_migrations_are_idempotent(storage):
    """Re-running the DDL migrations on an already-migrated DB must not raise."""
    storage._run_ddl_migrations()


def test_index_on_searches_user_active_exists(storage):
    conn = storage._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'searches' AND indexname = 'idx_searches_user_active'
            """)
            row = cur.fetchone()
    finally:
        storage._release_conn(conn)

    assert row is not None
