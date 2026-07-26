"""Migrations DDL (storage.py) contre un vrai Postgres.

Principe de ce module : **une colonne n'est prouvée que par un aller-retour**.
Interroger `information_schema` prouve qu'une colonne porte le bon nom, jamais
que l'application sait écrire et relire à travers — un `ALTER` avec un type
incompatible passerait le test sans broncher. Chaque migration est donc
validée en écrivant puis relisant par les repos, sur une base créée pour
l'occasion (fixture `blank_db`) : aucune dépendance d'ordre, aucun nettoyage
manuel.

`information_schema` ne sert plus que là où l'absence d'une colonne (ou
l'absence de DDL) est elle-même le sujet du test.
"""

from __future__ import annotations

import json

import psycopg2
import pytest

from repositories.base import BaseRepository
from repositories.user_repo import UserRepository
from storage import Storage
from tests.helpers.factories import make_criteria, make_listing, make_photos_json

# ---------------------------------------------------------------------------
# Utilitaires locaux : SQL brut sur une base arbitraire (hors repos)
# ---------------------------------------------------------------------------


def _query(url: str, sql: str, params=None):
    conn = psycopg2.connect(url, connect_timeout=10)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _exec(url: str, sql: str, params=None) -> None:
    conn = psycopg2.connect(url, connect_timeout=10)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    finally:
        conn.close()


def _columns_of(url: str, table: str) -> set[str]:
    rows = _query(
        url,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    )
    return {r[0] for r in rows}


def _tables_of(url: str) -> set[str]:
    rows = _query(
        url,
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
    )
    return {r[0] for r in rows}


# Toutes les colonnes de `listings`, remplies avec des valeurs distinctes et
# non vides : un type incompatible ou une colonne oubliée fait échouer
# l'écriture ou la relecture, pas seulement une comparaison de noms.
_FULL_LISTING = {
    "listing_id": "mig_full_1",
    "url": "https://www.seloger.com/annonces/mig_full_1.htm",
    "title": "Loft atypique 120 m²",
    "price": "2 450 €/mois",
    "price_value": 2450.5,
    "surface": "120",
    "rooms": "5",
    "location": "Bordeaux Chartrons",
    "image_url": "https://cdn.example/main.jpg",
    "description": "Beaux volumes, parquet d'origine.",
    "agency": "Agence des Quais",
    "source": "laforet",
    "legacy_id": "LF-99887",
    "price_details": "charges comprises",
    "city": "Bordeaux",
    "district": "Chartrons",
    "zip_code": "33300",
    "property_type": "apartment",
    "is_private": True,
    "epc": "C",
    "ges": "D",
    "is_new": True,
    "is_exclusive": True,
    "has_3d_visit": True,
    "creation_date": "2026-07-01",
    "update_date": "2026-07-20",
    "headline": "Coup de cœur",
}


def _round_trip_whole_schema(url: str) -> None:
    """Écrit puis relit à travers CHAQUE table et CHAQUE colonne du schéma.

    C'est la vraie preuve qu'une migration a fonctionné : `Storage(url)`
    commence par `_init_db()` (qui refuse de démarrer sans les tables), puis
    chaque repo écrit et relit ses colonnes avec leurs types réels.
    """
    storage = Storage(url)

    user = storage.users.create_user("migrated")
    assert storage.users.get_user_by_token(user["api_token"])["username"] == "migrated"

    criteria = make_criteria(rooms=[2, 3], surfaceMin=45)
    search = storage.searches.create_search(
        user["id"], "Label migré", "topic-migré", "laforet",
        criteria, 12, is_active=False, sources=["laforet", "seloger"],
    )
    fetched = storage.searches.get_search(search["id"])
    assert fetched["criteria"] == criteria           # colonne criteria JSONB
    assert fetched["sources"] == ["laforet", "seloger"]  # colonne sources JSONB
    assert fetched["scrape_interval"] == 12
    assert fetched["is_active"] is False
    assert fetched["blacklisted_agencies"] == []     # colonne TEXT[]
    assert fetched["blacklist_mode"] == "exclude"

    assert storage.searches.update_blacklisted_agencies(search["id"], ["Foncia", "Orpi"]) is True
    assert storage.searches.update_blacklist_mode(search["id"], "no_notify") is True
    reread = storage.searches.get_search(search["id"])
    assert reread["blacklisted_agencies"] == ["Foncia", "Orpi"]
    assert reread["blacklist_mode"] == "no_notify"

    photos = make_photos_json(3)
    listing = make_listing(**_FULL_LISTING, phone='["0102030405"]', photos=photos)
    new_listings, already = storage.listings.save_and_link([listing], search["id"])
    assert [item.listing_id for item in new_listings] == ["mig_full_1"]
    assert already == []

    detail = storage.listings.get_listing_detail("mig_full_1")
    for column, expected in _FULL_LISTING.items():
        assert detail[column] == expected, f"colonne {column} : aller-retour cassé"
    assert detail["phone"] == ["0102030405"]         # colonne phone JSONB
    assert detail["photos"] == json.loads(photos)    # colonne photos JSONB
    assert detail["first_seen"] is not None          # colonne first_seen TIMESTAMP

    # search_listings : found_at + notified
    unnotified = storage.listings.get_unnotified_listings_for_search(search["id"])
    assert [item.listing_id for item in unnotified] == ["mig_full_1"]
    storage.listings.mark_listings_notified(search["id"], ["mig_full_1"])
    assert storage.listings.get_unnotified_listings_for_search(search["id"]) == []
    assert storage.listings.get_listings_for_search(search["id"])[0]["found_at"] is not None

    # admin_logs
    storage.admin.log_admin_action("migration_probe", "détails", "tester")
    logs = storage.admin.get_admin_logs()
    assert [log["action"] for log in logs] == ["migration_probe"]
    assert logs[0]["created_at"] is not None

    # app_settings
    assert storage.settings.set_setting("theme", "dark") is True
    assert storage.settings.get_setting("theme") == "dark"

    # seloger_place_ids
    storage.seloger_geo.set_cached("dept:33", "AD06FR34")
    cached = storage.seloger_geo.get_cached("dept:33")
    assert cached["area_key"] == "dept:33"
    assert cached["place_id"] == "AD06FR34"
    assert cached["resolved_at"] is not None

    # bienici_zone_ids
    storage.bienici_geo.set_cached("dept:33", ["-7405"])
    cached = storage.bienici_geo.get_cached("dept:33")
    assert cached["area_key"] == "dept:33"
    assert cached["zone_ids"] == ["-7405"]
    assert cached["resolved_at"] is not None


# ---------------------------------------------------------------------------
# Base vierge, et idempotence
# ---------------------------------------------------------------------------

_EXPECTED_TABLES = {
    "users", "searches", "listings", "search_listings",
    "admin_logs", "app_settings", "seloger_place_ids", "bienici_zone_ids",
}


def test_run_migrations_on_a_virgin_database_builds_a_fully_usable_schema(blank_db):
    """Le cas du premier déploiement : rien en base, tout doit être créé.

    Régression historique couverte au passage : `CREATE INDEX
    idx_searches_user_active(user_id, is_active)` tournait avant l'`ALTER
    TABLE` qui ajoute `is_active`, ce qui échouait sur une base neuve. Ici
    l'échec serait immédiat, avant même l'aller-retour.
    """
    url = blank_db()
    assert _tables_of(url) == set(), "la base doit être réellement vierge"

    Storage.run_migrations(url)

    assert _EXPECTED_TABLES.issubset(_tables_of(url))
    _round_trip_whole_schema(url)


def test_run_migrations_is_idempotent_and_preserves_data(blank_db):
    """Rejouer les migrations est le geste normal après un déploiement :
    chaque instruction est censée être idempotente (`IF NOT EXISTS`), et les
    données déjà en base doivent survivre."""
    url = blank_db()
    Storage.run_migrations(url)
    storage = Storage(url)
    user = storage.users.create_user("survivant")
    search = storage.searches.create_search(
        user["id"], "À conserver", "topic", "seloger", make_criteria(), 9,
    )

    Storage.run_migrations(url)
    Storage.run_migrations(url)

    survivor = storage.searches.get_search(search["id"])
    assert survivor["label"] == "À conserver"
    assert survivor["criteria"] == make_criteria()
    assert storage.users.get_user_by_username("survivant")["id"] == user["id"]
    # ... et le schéma reste pleinement fonctionnel après trois passages.
    assert storage.searches.update_scrape_interval(search["id"], 15) is True
    assert storage.searches.get_search(search["id"])["scrape_interval"] == 15


def test_index_on_searches_user_active_is_usable(blank_db):
    """Le seul contrôle de schéma volontairement conservé : cet index est la
    régression d'origine (créé avant sa colonne). On vérifie qu'il existe ET
    qu'une requête filtrant sur (user_id, is_active) répond correctement."""
    url = blank_db()
    Storage.run_migrations(url)
    storage = Storage(url)
    user = storage.users.create_user("indexed")
    active = storage.searches.create_search(user["id"], "Active", "t1", "seloger", {}, 5, is_active=True)
    storage.searches.create_search(user["id"], "Inactive", "t2", "seloger", {}, 5, is_active=False)

    indexes = {row[0] for row in _query(
        url, "SELECT indexname FROM pg_indexes WHERE tablename = 'searches'",
    )}
    assert "idx_searches_user_active" in indexes

    rows = _query(url, "SELECT id FROM searches WHERE user_id = %s AND is_active = TRUE", (user["id"],))
    assert [r[0] for r in rows] == [active["id"]]


# ---------------------------------------------------------------------------
# _init_db : jamais de DDL au runtime
# ---------------------------------------------------------------------------

def test_init_db_refuses_to_start_when_tables_are_missing(blank_db):
    url = blank_db()

    with pytest.raises(RuntimeError, match="Database tables not found"):
        Storage(url)


def test_init_db_never_creates_anything(blank_db):
    """`_init_db` constate, il ne répare pas : après un démarrage refusé, la
    base doit être aussi vide qu'avant (c'est ici que `information_schema` est
    légitime — l'ABSENCE de DDL est le sujet)."""
    url = blank_db()

    with pytest.raises(RuntimeError, match="Database tables not found"):
        Storage(url)

    assert _tables_of(url) == set()

    # Et l'échec est reproductible : rien n'a été créé « au premier essai ».
    with pytest.raises(RuntimeError, match="Database tables not found"):
        Storage(url)


def test_init_db_passes_as_soon_as_the_users_table_exists(blank_db):
    """`_init_db` ne teste QUE la présence de `users`.

    Conséquence directe, exploitée par le test de migration partielle plus
    bas : une base à moitié migrée démarre sans un mot, et casse plus tard au
    premier INSERT sur une colonne manquante.
    """
    url = blank_db()
    _exec(url, "CREATE TABLE users (id SERIAL PRIMARY KEY, username TEXT, api_token TEXT)")

    storage = Storage(url)  # ne lève pas, alors que searches/listings n'existent pas

    assert _tables_of(url) == {"users"}
    with pytest.raises(psycopg2.errors.UndefinedTable):
        storage.searches.get_search(1)


# ---------------------------------------------------------------------------
# run_migrations contourne __init__ : garde-fou sur les repos disponibles
# ---------------------------------------------------------------------------

def test_run_migrations_wires_only_the_users_repository(blank_db, monkeypatch):
    """`run_migrations` fabrique l'instance via `cls.__new__(cls)` pour sauter
    `_init_db` (les tables n'existent pas encore, par définition) et ne pose
    QUE `database_url` et `users`.

    Le jour où une migration voudrait passer par un autre repo — par exemple
    `self.settings.set_setting(...)` pour enregistrer un numéro de version —
    elle lèverait `AttributeError` en production. Ce test fige le contrat.
    """
    url = blank_db()
    seen: dict[str, set[str]] = {}
    original = Storage._run_ddl_migrations

    def spy(self):
        seen["attributes"] = set(vars(self))
        return original(self)

    monkeypatch.setattr(Storage, "_run_ddl_migrations", spy)

    Storage.run_migrations(url)

    assert seen["attributes"] == {"database_url", "users"}

    # La garde, vue de l'intérieur d'une migration : tout autre repo manque.
    instance = Storage.__new__(Storage)
    instance.database_url = url
    instance.users = UserRepository(url)
    for missing in ("searches", "listings", "settings", "admin", "seloger_geo", "scrape_logs"):
        with pytest.raises(AttributeError):
            getattr(instance, missing)


# ---------------------------------------------------------------------------
# Migrations spécifiques
# ---------------------------------------------------------------------------

def test_migration_removes_the_dead_use_bff_api_setting(blank_db):
    """L'API BFF de SeLoger est morte : le réglage qui l'activait doit
    disparaître des bases déjà déployées, sans emporter les autres réglages."""
    url = blank_db()
    Storage.run_migrations(url)
    storage = Storage(url)
    storage.settings.set_setting("use_bff_api", "true")
    storage.settings.set_setting("notify_enabled", "true")

    Storage.run_migrations(url)

    assert storage.settings.get_setting("use_bff_api", default="absent") == "absent"
    assert storage.settings.get_setting("notify_enabled") == "true"


def test_migration_renames_legacy_insee_code_column_keeping_cached_resolutions(blank_db):
    """Les bases créées avant les périmètres larges ont une colonne
    `insee_code`. Le bloc `DO $$` la renomme en `area_key` — et les
    résolutions déjà en cache doivent rester lisibles par le repo (un code
    INSEE nu reste une clé valide)."""
    url = blank_db()
    _exec(url, """
        CREATE TABLE seloger_place_ids (
            insee_code  TEXT PRIMARY KEY,
            place_id    TEXT,
            resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    _exec(url, "INSERT INTO seloger_place_ids (insee_code, place_id) VALUES ('33063', 'AD08FRLEGACY')")

    Storage.run_migrations(url)

    assert _columns_of(url, "seloger_place_ids") >= {"area_key", "place_id", "resolved_at"}
    assert "insee_code" not in _columns_of(url, "seloger_place_ids")

    storage = Storage(url)
    row = storage.seloger_geo.get_cached("33063")
    assert row["area_key"] == "33063"
    assert row["place_id"] == "AD08FRLEGACY"

    # Le renommage est conditionnel : un second passage ne doit pas échouer
    # (la colonne `insee_code` n'existe plus) ni perdre la ligne.
    Storage.run_migrations(url)
    assert storage.seloger_geo.get_cached("33063")["place_id"] == "AD08FRLEGACY"


def test_migration_backfills_sources_from_source_on_pre_existing_rows(blank_db):
    """Une recherche créée avant le multi-source n'a que `source`. Le pipeline
    de scraping lit `sources` : sans backfill, la recherche cesse d'être
    scrapée en silence."""
    url = blank_db()
    Storage.run_migrations(url)
    storage = Storage(url)
    user = storage.users.create_user("legacy_sources")
    # `create_search` écrit toujours `sources` : il faut du SQL brut pour
    # reproduire une ligne d'avant la migration.
    search_id = _query(url, """
        INSERT INTO searches (user_id, label, ntfy_topic, source, sources)
        VALUES (%s, 'Ancienne', 'topic', 'laforet', NULL) RETURNING id
    """, (user["id"],))[0][0]
    assert storage.searches.get_search(search_id)["sources"] == ["laforet"]  # repli défensif du repo

    Storage.run_migrations(url)

    # Cette fois la valeur est réellement EN BASE, pas reconstruite à la lecture.
    assert _query(url, "SELECT sources FROM searches WHERE id = %s", (search_id,))[0][0] == ["laforet"]


def test_migration_adds_notified_to_a_pre_existing_search_listings_table(blank_db):
    """Le bug rencontré en production : la colonne `notified` a été ajoutée au
    DDL, mais `run_migrations` n'est pas jouée au démarrage — une base déjà
    déployée ne l'a reçue qu'après un `scripts/migrate.py` à la main.

    Le `DEFAULT TRUE` n'est pas décoratif : il fait passer les liens
    pré-existants pour « déjà notifiés », sinon le premier scrape après la
    migration renotifierait tout l'historique.
    """
    url = blank_db()
    Storage.run_migrations(url)
    storage = Storage(url)
    user = storage.users.create_user("notified_backfill")
    search = storage.searches.create_search(user["id"], "S", "topic", "seloger", {}, 5)
    storage.listings.save_and_link([make_listing(listing_id="old_link")], search["id"])
    storage.listings.mark_listings_notified(search["id"], ["old_link"])

    _exec(url, "ALTER TABLE search_listings DROP COLUMN notified")
    _exec(url, """
        INSERT INTO listings (listing_id, url) VALUES ('pre_existing', 'https://e/pre')
    """)
    _exec(
        url,
        "INSERT INTO search_listings (search_id, listing_id) VALUES (%s, 'pre_existing')",
        (search["id"],),
    )

    Storage.run_migrations(url)

    # Aller-retour réel : le lien pré-existant est vu comme déjà notifié...
    assert storage.listings.get_unnotified_listings_for_search(search["id"]) == []
    # ... et un nouveau lien, lui, est bien à notifier (notified=FALSE explicite).
    storage.listings.save_and_link([make_listing(listing_id="brand_new")], search["id"])
    unnotified = storage.listings.get_unnotified_listings_for_search(search["id"])
    assert [item.listing_id for item in unnotified] == ["brand_new"]


# ---------------------------------------------------------------------------
# Les deux commit() intermédiaires : un échec entre les deux laisse un état
# partiel, et rien ne le détecte au démarrage suivant.
# ---------------------------------------------------------------------------

class _FailingCursor:
    """Curseur qui refuse une instruction précise, pour simuler une coupure."""

    def __init__(self, cursor, fail_on: str):
        self._cursor = cursor
        self._fail_on = fail_on

    def execute(self, sql, params=None):
        if self._fail_on in sql:
            raise psycopg2.OperationalError("coupure simulée pendant la migration")
        return self._cursor.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._cursor.__exit__(*exc_info)


class _FailingConn:
    def __init__(self, conn, fail_on: str):
        self._conn = conn
        self._fail_on = fail_on

    def cursor(self, *args, **kwargs):
        return _FailingCursor(self._conn.cursor(*args, **kwargs), self._fail_on)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_a_failure_between_the_two_commits_leaves_a_half_migrated_database(blank_db, monkeypatch):
    """# BUG : `_run_ddl_migrations` commite deux fois (après les tables, puis
    après les ALTER/backfill) sans jalon de version. Une coupure entre les
    deux laisse une base durablement incohérente : les tables sont là, donc
    `_init_db` autorise le démarrage, mais `searches.sources`,
    `blacklisted_agencies` et `blacklist_mode` manquent — l'application tombe
    au premier INSERT, pas au boot.

    Ce test documente le comportement ACTUEL ; il n'y a rien à réparer côté
    test tant que la production n'a pas de table de versions de schéma.
    """
    url = blank_db()
    original_get_ddl_conn = BaseRepository._get_ddl_conn
    monkeypatch.setattr(
        BaseRepository,
        "_get_ddl_conn",
        lambda self: _FailingConn(original_get_ddl_conn(self), "ALTER TABLE listings"),
    )

    with pytest.raises(psycopg2.OperationalError, match="coupure simulée"):
        Storage.run_migrations(url)

    monkeypatch.undo()

    # Le premier commit a tenu : les tables existent.
    assert _EXPECTED_TABLES.issubset(_tables_of(url))
    # Le second n'a jamais eu lieu : les colonnes ajoutées par ALTER manquent.
    # (`notified`, elle, figure aussi dans le CREATE TABLE : elle survit.)
    assert _columns_of(url, "searches").isdisjoint({"sources", "blacklisted_agencies", "blacklist_mode"})

    # Et pourtant l'application démarre — c'est tout le problème.
    storage = Storage(url)
    user = storage.users.create_user("half_migrated")
    with pytest.raises(psycopg2.errors.UndefinedColumn):
        storage.searches.create_search(user["id"], "Boum", "topic", "seloger", {}, 5)

    # Rejouer la migration complète répare la base : c'est le seul recours.
    Storage.run_migrations(url)
    search = storage.searches.create_search(user["id"], "Réparé", "topic", "seloger", make_criteria(), 5)
    assert storage.searches.get_search(search["id"])["sources"] == ["seloger"]
