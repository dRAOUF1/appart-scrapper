"""Fixtures des tests fonctionnels : l'app Flask **réelle**, base doublée.

Ces tests passent par `main.create_app()` — pas par une app Flask reconstruite à
la main. C'est délibéré : le câblage (CSRFProtect et son exemption de l'API,
`before_request`/`teardown_request` qui empruntent et rendent la connexion, les
filtres Jinja, le context processor admin, l'ordre des blueprints) fait partie du
comportement de production. Une app maison ne le teste pas, et divergerait.

Seuls `Storage` et `Notifier` sont remplacés par des doubles, et le scheduler
n'est pas démarré.

Ce module porte aussi les **builders de données de vue** (`make_admin_stats`,
`make_dashboard_data`, ...). Les templates Jinja lisent des clés très précises
(`stats.orphan_listings`, `db_stats.tables`, `session['username'][0]`) : les
construire ici une fois évite de dupliquer ces dicts dans chaque test, et un
champ ajouté au template ne se corrige qu'à un endroit.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from tests.helpers.factories import make_listing_row, make_search_row, make_user_row
from tests.helpers.fakes import fake_notifier, fake_storage

ADMIN_USERNAME = "root-admin"


@pytest.fixture
def storage():
    """Le `Storage` doublé que verra l'app. À configurer dans chaque test."""
    return fake_storage()


@pytest.fixture
def notifier():
    return fake_notifier()


@pytest.fixture
def app_factory(monkeypatch, storage, notifier):
    """Fabrique d'app : `main.create_app()` avec ses dépendances doublées.

    Exposée séparément de `app` pour les tests du *démarrage* lui-même (absence
    de SECRET_KEY, variables d'environnement manquantes), qui doivent contrôler
    l'environnement avant l'appel et parfois s'attendre à une exception.
    """
    import main

    monkeypatch.setattr(main, "load_dotenv", lambda *a, **kw: False)
    monkeypatch.setattr(main, "Storage", lambda database_url: storage)
    monkeypatch.setattr(main, "Notifier", lambda **kwargs: notifier)
    # Pas de scheduler ni de verrou Postgres en test : ce chemin a ses propres
    # tests unitaires (tests/unit/test_scheduler.py).
    monkeypatch.setattr(main, "_start_background_tasks", lambda app: None)
    # `_scrape_executor` et `_scrape_futures` sont des globales de module,
    # partagées par toutes les apps du process : sans isolation, un scrape
    # soumis par un test resterait « en cours » pour les suivants, et une vraie
    # tâche de fond tournerait sur le storage doublé pendant leur exécution.
    monkeypatch.setattr(main, "_scrape_executor", MagicMock())
    monkeypatch.setattr(main, "_scrape_futures", {})

    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_USERNAME)

    def build():
        flask_app = main.create_app()
        flask_app.config.update(TESTING=True)
        return flask_app

    return build


@pytest.fixture
def app(app_factory):
    """App Flask de production, avec storage/notifier doublés.

    `load_dotenv` est neutralisé : `create_app()` l'appelle, et `.env` contient
    le `DATABASE_URL` de production — il ne doit jamais entrer dans
    l'environnement d'un test (cf. le garde-fou de tests/conftest.py).
    """
    return app_factory()


@pytest.fixture
def app_without_csrf(app):
    """Même app, protection CSRF désactivée.

    Pour les tests qui exercent la *logique* d'un POST web plutôt que le jeton
    lui-même. L'exemption CSRF est vérifiée pour elle-même dans les tests de
    sécurité — ne pas utiliser cette fixture pour cela.
    """
    app.config.update(WTF_CSRF_ENABLED=False)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def user(storage):
    """Utilisateur standard, reconnu par token (API et session web)."""
    row = make_user_row(id=1, username="alice", api_token="token-alice")
    storage.users.get_user_by_token.side_effect = lambda token: row if token == row["api_token"] else None
    storage.users.get_user_by_username.side_effect = lambda name: row if name == row["username"] else None
    return row


@pytest.fixture
def admin_user(storage):
    """Utilisateur dont le username correspond à ADMIN_USERNAME."""
    row = make_user_row(id=99, username=ADMIN_USERNAME, api_token="token-admin")
    storage.users.get_user_by_token.side_effect = lambda token: row if token == row["api_token"] else None
    return row


@pytest.fixture
def api_client(app, user):
    """Client HTTP authentifié par header, comme un consommateur de l'API."""
    client = app.test_client()
    client.environ_base["HTTP_X_API_TOKEN"] = user["api_token"]
    return client


def _log_in(client, row):
    """Pose exactement les trois clés que `/login` écrit en production.

    `username` en fait partie : `templates/base.html` l'utilise sans garde
    (`session.get('username','')[0]`), donc une session incomplète ferait
    échouer le rendu de toute page authentifiée.
    """
    with client.session_transaction() as sess:
        sess["user_id"] = row["id"]
        sess["username"] = row["username"]
        sess["api_token"] = row["api_token"]
    return client


@pytest.fixture
def web_client(app_without_csrf, user):
    """Client web connecté (session posée), CSRF désactivé."""
    return _log_in(app_without_csrf.test_client(), user)


@pytest.fixture
def admin_client(app_without_csrf, admin_user):
    """Client web connecté en admin."""
    return _log_in(app_without_csrf.test_client(), admin_user)


# ---------------------------------------------------------------------------
# Builders de données de vue
#
# Chaque builder rend une structure COMPLÈTE, telle que le repository la
# renvoie en production, parce que les templates lisent des clés précises sans
# garde. Surcharges par mot-clé, comme les factories.
# ---------------------------------------------------------------------------

def make_admin_stats(**overrides) -> dict:
    """Retour de `AdminRepository.get_enhanced_admin_stats()`.

    `templates/admin.html` compare `stats.orphan_listings > 0` et
    `stats.users_without_searches > 0` : ces clés ne peuvent pas manquer, une
    comparaison avec Undefined levant à la première alerte.
    """
    stats = {
        "users": 3,
        "searches": 5,
        "total_listings": 42,
        "search_listings": 51,
        "new_today": 4,
        "orphan_listings": 2,
        "avg_listings_per_search": 8.4,
        "users_without_searches": 1,
        "top_users": [{"username": "alice", "listing_count": 30}],
        "top_searches": [{"id": 1, "label": "Paris 13e", "username": "alice", "listing_count": 30}],
        "activity_7d": [{"day": datetime(2026, 7, 1), "count": 4}],
        "sources_breakdown": [{"source": "seloger", "cnt": 4}],
    }
    stats.update(overrides)
    return stats


def make_dashboard_data(**overrides) -> dict:
    """Retour de `UserRepository.get_dashboard_data()` : stats + recherches + récentes."""
    data = {
        "stats": {"searches": 2, "total_listings": 12, "new_today": 3},
        "searches": [make_search_row(listing_count=7)],
        "recent": [make_listing_row(search_label="Paris 13e", found_at=datetime(2026, 7, 1, 9, 0))],
    }
    data.update(overrides)
    return data


def make_user_stats(**overrides) -> dict:
    """Retour de `UserRepository.get_user_stats()` (GET /api/stats)."""
    stats = {"searches": 2, "total_listings": 12, "new_today": 3}
    stats.update(overrides)
    return stats


def make_admin_user_row(**overrides) -> dict:
    """Ligne de `get_all_users()` : un user_row enrichi des compteurs admin."""
    row = make_user_row(**{k: v for k, v in overrides.items() if k in ("id", "username", "api_token", "created_at")})
    row.setdefault("search_count", 2)
    row.setdefault("listing_count", 11)
    row.update({k: v for k, v in overrides.items() if k in ("search_count", "listing_count")})
    return row


def make_user_detail(**overrides) -> dict:
    """Retour de `UserRepository.get_user_detail()`, avec ses sous-listes."""
    detail = make_admin_user_row()
    detail["searches"] = [
        {"id": 1, "label": "Paris 13e", "source": "seloger",
         "created_at": datetime(2026, 1, 1), "listing_count": 7},
    ]
    detail["recent_listings"] = [
        {"title": "Appartement 3 pièces", "price": "1 200 €", "location": "Paris 13e",
         "first_seen": datetime(2026, 7, 1), "search_label": "Paris 13e"},
    ]
    detail.update(overrides)
    return detail


def make_search_detail(**overrides) -> dict:
    """Retour de `SearchRepository.get_search_detail()` (jointure sur username)."""
    detail = make_search_row()
    detail["username"] = "alice"
    detail["total_listings"] = 7
    detail["recent_listings"] = [make_listing_row(found_at=datetime(2026, 7, 1, 9, 0))]
    detail.update(overrides)
    return detail


def make_admin_listing_row(**overrides) -> dict:
    """Ligne de `get_all_listings()` : annonce + nombre de recherches liées."""
    row = make_listing_row(first_seen=datetime(2026, 7, 1))
    row["linked_searches"] = 2
    row.update(overrides)
    return row


def make_listing_detail(**overrides) -> dict:
    """Retour de `ListingRepository.get_listing_detail()` : `linked_searches` est
    une LISTE ici, alors que c'est un COMPTEUR dans `get_all_listings()`."""
    detail = make_listing_row(first_seen=datetime(2026, 7, 1))
    detail["linked_searches"] = [{"id": 1, "label": "Paris 13e", "source": "seloger", "username": "alice"}]
    detail.update(overrides)
    return detail


def make_db_stats(**overrides) -> dict:
    """Retour de `AdminRepository.get_db_stats()`.

    `row_count` doit être un entier : le template le passe à `"{:,}".format`.
    """
    stats = {
        "db_size": "42 MB",
        "tables": [
            {"table_name": "listings", "row_count": 1234, "total_size": "20 MB", "data_size": "18 MB"},
            {"table_name": "users", "row_count": 3, "total_size": "48 kB", "data_size": "16 kB"},
        ],
        "indexes": [{"schemaname": "public", "tablename": "listings",
                     "indexname": "listings_pkey", "index_size": "1 MB"}],
    }
    stats.update(overrides)
    return stats


def make_table_details(**overrides) -> dict:
    """Retour de `AdminRepository.get_table_details()`."""
    details = {
        "columns": [{"column_name": "id", "data_type": "integer",
                     "is_nullable": "NO", "column_default": "nextval(...)",
                     "character_maximum_length": None}],
        "indexes": [{"indexname": "users_pkey", "indexdef": "CREATE UNIQUE INDEX ..."}],
        "constraints": [{"constraint_name": "users_pkey", "constraint_type": "p",
                         "definition": "PRIMARY KEY (id)"}],
        "total_size": "48 kB",
    }
    details.update(overrides)
    return details


def make_active_connection(**overrides) -> dict:
    """Ligne de `get_active_connections()` (`pg_stat_activity`)."""
    row = {
        "pid": 4242,
        "usename": "appart",
        "application_name": "appart-scrapper",
        "client_addr": "10.0.0.1",
        "backend_start": datetime(2026, 7, 1, 9, 0),
        "state": "idle",
        "query": "SELECT 1",
        "query_start": datetime(2026, 7, 1, 9, 0),
    }
    row.update(overrides)
    return row


def make_admin_log_row(**overrides) -> dict:
    """Ligne de `admin_logs`, telle que `get_admin_logs()` la renvoie."""
    row = {
        "id": 1,
        "action": "user_deleted",
        "details": "User 'bob' (ID:2) deleted",
        "performed_by": ADMIN_USERNAME,
        "created_at": datetime(2026, 7, 1, 9, 0),
    }
    row.update(overrides)
    return row


def make_scrape_stats(**overrides) -> dict:
    """Retour de `ScrapeLogRepository.get_scrape_stats()`."""
    stats = {
        "total": 12,
        "success_count": 10,
        "error_count": 1,
        "empty_count": 1,
        "avg_listings": 8.5,
        "avg_duration": 31.2,
        "last_scrape": None,
    }
    stats.update(overrides)
    return stats


def make_filter_options(**overrides) -> dict:
    """Retour de `ListingRepository.get_filter_options()` (facettes du filtre)."""
    options = {
        "cities": ["Paris"],
        "districts": ["Paris 13e"],
        "zip_codes": ["75013"],
        "property_types": ["apartment"],
        "agencies": ["Agence Test"],
        "epc": ["C"],
        "ges": ["B"],
    }
    options.update(overrides)
    return options


# ---------------------------------------------------------------------------
# Fixtures dérivées, pour les tests de rendu de page
# ---------------------------------------------------------------------------

@pytest.fixture
def owned_search(storage, user):
    """Une recherche appartenant à `user`, servie par `get_search()`.

    Passe par `side_effect` pour que TOUT autre id renvoie None : un test
    d'autorisation ne doit pas pouvoir réussir par accident.
    """
    row = make_search_row(id=1, user_id=user["id"])
    storage.searches.get_search.side_effect = lambda search_id: row if search_id == row["id"] else None
    storage.searches.get_user_searches.return_value = [row]
    return row


@pytest.fixture
def foreign_search(storage, user):
    """Une recherche appartenant à QUELQU'UN D'AUTRE, servie pour l'id 1.

    C'est le double du scénario IDOR : la ligne existe, la route la trouve,
    et c'est la comparaison `user_id` qui doit refuser.
    """
    row = make_search_row(id=1, user_id=user["id"] + 1000)
    storage.searches.get_search.side_effect = lambda search_id: row if search_id == row["id"] else None
    return row


@pytest.fixture
def admin_stats(storage):
    """Branche des stats admin complètes : rend `/admin` affichable."""
    stats = make_admin_stats()
    storage.admin.get_enhanced_admin_stats.return_value = stats
    return stats
