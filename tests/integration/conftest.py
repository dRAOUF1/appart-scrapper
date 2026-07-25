"""Fixtures for integration tests that hit a real Postgres (DATABASE_URL).

Skipped automatically when DATABASE_URL isn't set, so `pytest tests/` still
runs cleanly on a laptop without Docker/Postgres — only
docker-compose.test.yml (which sets DATABASE_URL) actually exercises these.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

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


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "db", ""}


@pytest.fixture(scope="session")
def pg_url():
    """URL d'un Postgres jetable, lue depuis TEST_DATABASE_URL et JAMAIS DATABASE_URL.

    `DATABASE_URL` pointe la production et peut arriver dans os.environ par un
    simple `load_dotenv()` en side effect d'import. Ces tests TRUNCATE toutes les
    tables : on exige donc une variable dédiée, et on refuse tout host distant.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL non défini — tests d'intégration sautés")

    host = (urlparse(url).hostname or "").lower()
    if host not in _LOCAL_HOSTS:
        pytest.fail(
            f"TEST_DATABASE_URL pointe un host non local ({host!r}). Ces tests "
            "vident toutes les tables : refus catégorique de toucher autre chose "
            "qu'un Postgres jetable local ou conteneurisé.",
            pytrace=False,
        )
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
