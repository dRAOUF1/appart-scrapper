"""Integration: the real DDL migrations must run cleanly on a fresh DB,
AND correctly upgrade an existing/pre-existing database.

Regression coverage for the DDL-ordering bug fixed this session: on a
brand-new database, CREATE INDEX idx_searches_user_active(user_id, is_active)
used to run before the ALTER TABLE that added is_active, which failed
outright. This module's `storage` fixture already runs the real migrations
(see conftest.py, Storage.run_migrations) — if that setup succeeded, the
ordering fix is proven on a fresh DB.

test_run_migrations_adds_missing_notified_column_to_existing_table below
covers the OTHER path — upgrading a database that already existed with an
old schema — which is what actually broke in production: the `notified`
column was added to the DDL, but run_migrations() is never invoked
automatically at boot, so an already-deployed database never got it until
someone ran scripts/migrate.py by hand.
"""

from __future__ import annotations

import pytest

from storage import Storage

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


def test_run_migrations_adds_missing_notified_column_to_existing_table(storage):
    """Recreates the exact bug hit in production: an existing database
    predating the `notified` column. Storage.run_migrations() must add it
    without erroring — proving the upgrade path works, not just fresh-DB
    creation (which is all the other tests above exercise)."""
    conn = storage._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE search_listings DROP COLUMN notified")
        conn.commit()
    finally:
        storage._release_conn(conn)

    Storage.run_migrations(storage.database_url)

    conn = storage._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'search_listings' AND column_name = 'notified'
            """)
            row = cur.fetchone()
    finally:
        storage._release_conn(conn)

    assert row is not None
