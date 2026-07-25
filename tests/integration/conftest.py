"""Fixtures des tests d'intégration : un vrai Postgres, jetable et local.

Ce que ces tests prouvent, et que les tests unitaires ne peuvent pas prouver :
que le SQL réel s'exécute contre un vrai moteur — types, contraintes,
CASCADE, CAST, `READ ONLY`, verrous consultatifs, `execute_values`. Tout ce
qui est pur (construction de clauses, normalisation, allowlists) est couvert
dans tests/unit/ et n'est pas dupliqué ici.

Isolation : un seul mécanisme, `clean_db` (autouse), qui TRUNCATE toutes les
tables avant chaque test. Les helpers ci-dessous n'uniquifient donc rien : un
username est `alice`, et il n'existe qu'un `alice` par test.
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg2
import pytest

from repositories.base import BaseRepository
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


@pytest.fixture(autouse=True)
def clean_geo_cache(clean_db, storage):
    """Vide aussi le cache géo SeLoger avant chaque test.

    `seloger_place_ids` n'est pas dans `_TABLES` (elle n'a ni FK ni lien avec
    les données utilisateur). L'ancienne suite compensait par des nettoyages
    manuels en fin de test et une dépendance d'ordre assumée en commentaire :
    une fixture dédiée règle le problème une fois pour toutes, sans toucher à
    la logique de `clean_db`.
    """
    conn = storage._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE seloger_place_ids")
        conn.commit()
    finally:
        storage._release_conn(conn)


# ---------------------------------------------------------------------------
# Accès SQL direct — pour observer ce que les repos ont réellement écrit
# ---------------------------------------------------------------------------

class _Sql:
    """SQL brut sur le même Postgres, hors des repos.

    Sert à deux choses : vérifier l'état réel des tables après un appel de
    repo, et fabriquer des lignes que les repos ne savent pas produire
    (ancien vocabulaire de critères, `notified` laissé à sa valeur par
    défaut, `first_seen` vieilli).
    """

    def __init__(self, storage):
        self._storage = storage

    def _run(self, sql, params, fetch):
        conn = self._storage._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                result = cur.fetchall() if fetch else cur.rowcount
            conn.commit()
            return result
        finally:
            self._storage._release_conn(conn)

    def all(self, sql, params=None) -> list[tuple]:
        return self._run(sql, params, fetch=True)

    def one(self, sql, params=None):
        """Première colonne de la première ligne, ou None si aucune ligne."""
        rows = self._run(sql, params, fetch=True)
        return rows[0][0] if rows else None

    def row(self, sql, params=None) -> tuple | None:
        rows = self._run(sql, params, fetch=True)
        return rows[0] if rows else None

    def exec(self, sql, params=None) -> int:
        return self._run(sql, params, fetch=False)


@pytest.fixture
def sql(storage, clean_db):
    return _Sql(storage)


# ---------------------------------------------------------------------------
# Helpers d'insertion
# ---------------------------------------------------------------------------

def insert_user(storage, username: str = "alice") -> dict:
    """Un utilisateur, via le repo (donc avec un vrai token aléatoire)."""
    return storage.users.create_user(username)


def insert_search(storage, user_id: int, label: str = "Recherche", **overrides) -> dict:
    """Une recherche, via le repo. `criteria` est du canonique par défaut."""
    params = {
        "ntfy_topic": f"topic-{label.lower().replace(' ', '-')}",
        "source": "seloger",
        "criteria": {"locations": [{"kind": "city", "city": "Paris", "postalCode": "75013"}]},
        "scrape_interval": 5,
    }
    params.update(overrides)
    return storage.searches.create_search(user_id, label, **params)


def insert_raw_search(sql, user_id: int, criteria_json: str, **columns) -> int:
    """Une recherche insérée en SQL brut, pour figer une ligne « telle qu'en
    production » — critères dans l'ancien vocabulaire, `sources` à NULL, etc.

    Les repos normalisent à l'écriture comme à la lecture : impossible de
    fabriquer ce genre de ligne en passant par eux.
    """
    values = {
        "label": "Ancienne recherche",
        "ntfy_topic": "topic-legacy",
        "source": "seloger",
    }
    values.update(columns)
    cols = ", ".join(["user_id", "criteria", *values])
    placeholders = ", ".join(["%s"] * (2 + len(values)))
    return sql.one(
        f"INSERT INTO searches ({cols}) VALUES ({placeholders}) RETURNING id",
        (user_id, criteria_json, *values.values()),
    )


@pytest.fixture
def user(storage, clean_db):
    return insert_user(storage, "alice")


@pytest.fixture
def other_user(storage, clean_db):
    return insert_user(storage, "bob")


@pytest.fixture
def search(storage, user):
    return insert_search(storage, user["id"], "Paris 13e")


@pytest.fixture
def other_search(storage, other_user):
    return insert_search(storage, other_user["id"], "Bordeaux")


# ---------------------------------------------------------------------------
# Bases vierges jetables — pour les migrations DDL
# ---------------------------------------------------------------------------

_BLANK_DB_SEQ = itertools.count(1)


@pytest.fixture
def blank_db(pg_url):
    """Fabrique des bases VIERGES sur le même serveur local, et les supprime.

    Les migrations doivent être testées sur une base réellement neuve (c'est
    le cas du premier déploiement) et sur une base à l'ancien schéma. Les
    faire jouer sur la base partagée de la session forcerait à démolir puis
    reconstruire son schéma, ce qui crée exactement la dépendance d'ordre que
    l'ancienne suite avait finie par assumer en commentaire.

    Renvoie une fonction : `url = blank_db()`. Le host vient de `pg_url`, donc
    il est déjà passé par le garde-fou « local uniquement ».
    """
    parsed = urlparse(pg_url)
    base_name = (parsed.path or "/").lstrip("/")
    assert base_name, f"TEST_DATABASE_URL sans nom de base : {pg_url!r}"
    created: list[tuple[str, str]] = []

    def _admin_conn():
        conn = psycopg2.connect(pg_url, connect_timeout=10)
        conn.autocommit = True  # CREATE/DROP DATABASE refuse d'être transactionnel
        return conn

    def _drop(name: str) -> None:
        conn = _admin_conn()
        try:
            with conn.cursor() as cur:
                # FORCE (PG 13+) : coupe les connexions laissées ouvertes par
                # un test en échec plutôt que d'échouer à son tour.
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            conn.close()

    def _make() -> str:
        name = f"{base_name}_blank_{next(_BLANK_DB_SEQ)}"
        assert name != base_name, "une base jetable ne doit jamais être la base de la session"
        _drop(name)
        conn = _admin_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{name}"')
        finally:
            conn.close()
        url = urlunparse(parsed._replace(path=f"/{name}"))
        created.append((name, url))
        return url

    yield _make

    for name, url in created:
        pool = BaseRepository._pools.pop(url, None)
        if pool is not None:
            try:
                pool.closeall()
            except Exception:
                pass
        _drop(name)
