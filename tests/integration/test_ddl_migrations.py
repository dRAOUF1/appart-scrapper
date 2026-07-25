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
    "seloger_place_ids",
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


def test_searches_table_has_sources_column(storage):
    conn = storage._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'searches' AND column_name = 'sources'
            """)
            row = cur.fetchone()
    finally:
        storage._release_conn(conn)

    assert row is not None


def test_run_migrations_backfills_sources_from_source_on_existing_rows(storage):
    """A search created before multi-source support only has `source` set —
    the migration must backfill `sources` from it so the scrape pipeline
    (which reads `sources`) still picks it up."""
    conn = storage._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, api_token) VALUES ('sources_backfill_user', 'tok_sources_backfill') RETURNING id"
            )
            user_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO searches (user_id, label, ntfy_topic, source, sources) VALUES (%s, 'test', 'topic', 'seloger', NULL) RETURNING id",
                (user_id,),
            )
            search_id = cur.fetchone()[0]
        conn.commit()
    finally:
        storage._release_conn(conn)

    Storage.run_migrations(storage.database_url)

    conn = storage._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT sources FROM searches WHERE id = %s", (search_id,))
            row = cur.fetchone()
    finally:
        storage._release_conn(conn)

    assert row is not None
    assert row[0] == ["seloger"]


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


def test_seloger_place_ids_uses_the_area_key_column(storage):
    """La clé de cache identifie un périmètre à n'importe quel niveau (code
    INSEE de commune, "dept:33", "region:11"...), plus seulement une commune —
    d'où `area_key` et non `insee_code`."""
    conn = storage._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'seloger_place_ids'
            """)
            columns = {row[0] for row in cur.fetchall()}
    finally:
        storage._release_conn(conn)

    assert "area_key" in columns
    assert "insee_code" not in columns


def test_run_migrations_renames_the_legacy_insee_code_column(storage):
    """Les bases créées avant les périmètres larges ont une colonne
    `insee_code` : le renommage doit se faire sans perdre les résolutions déjà
    en cache (un code INSEE nu reste une clé valide)."""
    conn = storage._get_ddl_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS seloger_place_ids")
            cur.execute("""
                CREATE TABLE seloger_place_ids (
                    insee_code  TEXT PRIMARY KEY,
                    place_id    TEXT,
                    resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Clé dédiée à ce test : les autres tests du cache vérifient
            # qu'un périmètre jamais résolu est absent, une ligne laissée
            # derrière les ferait échouer.
            cur.execute(
                "INSERT INTO seloger_place_ids (insee_code, place_id) VALUES (%s, %s)",
                ("00001", "AD08FRLEGACY"),
            )
            conn.commit()
    finally:
        storage._close_conn(conn)

    storage._run_ddl_migrations()

    # La ligne existante survit et reste lisible par le repository.
    row = storage.seloger_geo.get_cached("00001")
    assert row is not None
    assert row["area_key"] == "00001"
    assert row["place_id"] == "AD08FRLEGACY"

    # Et le renommage ne casse pas une seconde exécution.
    storage._run_ddl_migrations()

    conn = storage._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM seloger_place_ids WHERE area_key = %s", ("00001",))
            conn.commit()
    finally:
        storage._release_conn(conn)
