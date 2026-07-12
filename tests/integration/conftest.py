"""Fixtures for integration tests that hit a real Postgres (DATABASE_URL).

Skipped automatically when DATABASE_URL isn't set, so `pytest tests/` still
runs cleanly on a laptop without Docker/Postgres — only
docker-compose.test.yml (which sets DATABASE_URL) actually exercises these.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from storage import Storage

_THIS_DIR = Path(__file__).resolve().parent

# Truncated together (in one statement) so CASCADE handles FK ordering
# regardless of listing order.
_TABLES = ["search_listings", "listings", "searches", "users", "admin_logs", "app_settings"]


def pytest_collection_modifyitems(config, items):
    for item in items:
        if Path(str(item.fspath)).resolve().is_relative_to(_THIS_DIR):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def pg_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL non défini — tests d'intégration sautés")
    return url


@pytest.fixture(scope="session")
def storage(pg_url):
    # Exercises the exact same path as scripts/migrate.py — this is what
    # caught (this time, retroactively) that DDL changes need a real
    # migration step, not just "CREATE TABLE IF NOT EXISTS" on next boot.
    Storage.run_migrations(pg_url)
    return Storage(pg_url)


@pytest.fixture(autouse=True)
def clean_db(storage):
    """Truncate all tables before each integration test for isolation."""
    conn = storage._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
        conn.commit()
    finally:
        storage._release_conn(conn)
    yield
