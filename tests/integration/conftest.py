"""Fixtures for integration tests that hit a real Postgres (DATABASE_URL).

Skipped automatically when DATABASE_URL isn't set, so `pytest tests/` still
runs cleanly on a laptop without Docker/Postgres — only
docker-compose.test.yml (which sets DATABASE_URL) actually exercises these.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repositories.user_repo import UserRepository
from storage import Storage

_THIS_DIR = Path(__file__).resolve().parent

# Truncated together (in one statement) so CASCADE handles FK ordering
# regardless of listing order.
_TABLES = ["search_listings", "listings", "searches", "users", "admin_logs", "app_settings"]


def pytest_collection_modifyitems(config, items):
    for item in items:
        if Path(str(item.fspath)).resolve().is_relative_to(_THIS_DIR):
            item.add_marker(pytest.mark.integration)


def _bootstrap_schema(pg_url: str) -> None:
    """Run the real DDL migrations once, bypassing Storage.__init__'s
    _init_db() check (which requires tables to already exist — by design,
    production never runs DDL implicitly at boot)."""
    tmp = Storage.__new__(Storage)
    tmp.database_url = pg_url
    tmp.users = UserRepository(pg_url)
    tmp._run_ddl_migrations()


@pytest.fixture(scope="session")
def pg_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL non défini — tests d'intégration sautés")
    return url


@pytest.fixture(scope="session")
def storage(pg_url):
    _bootstrap_schema(pg_url)
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
