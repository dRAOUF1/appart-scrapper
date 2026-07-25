"""Fixtures des tests fonctionnels : l'app Flask **réelle**, base doublée.

Ces tests passent par `main.create_app()` — pas par une app Flask reconstruite à
la main. C'est délibéré : le câblage (CSRFProtect et son exemption de l'API,
`before_request`/`teardown_request` qui empruntent et rendent la connexion, les
filtres Jinja, le context processor admin, l'ordre des blueprints) fait partie du
comportement de production. Une app maison ne le teste pas, et divergerait.

Seuls `Storage` et `Notifier` sont remplacés par des doubles, et le scheduler
n'est pas démarré.
"""

from __future__ import annotations

import pytest

from tests.helpers.factories import make_user_row
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
def app(monkeypatch, storage, notifier, tmp_path):
    """App Flask de production, avec storage/notifier doublés.

    `load_dotenv` est neutralisé : `create_app()` l'appelle, et `.env` contient
    le `DATABASE_URL` de production — il ne doit jamais entrer dans
    l'environnement d'un test (cf. le garde-fou de tests/conftest.py).
    """
    import main

    monkeypatch.setattr(main, "load_dotenv", lambda *a, **kw: False)
    monkeypatch.setattr(main, "Storage", lambda database_url: storage)
    monkeypatch.setattr(main, "Notifier", lambda **kwargs: notifier)
    # Pas de scheduler ni de verrou Postgres en test : ce chemin a ses propres
    # tests unitaires (tests/unit/test_scheduler.py).
    monkeypatch.setattr(main, "_start_background_tasks", lambda app: None)

    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_USERNAME)

    flask_app = main.create_app()
    flask_app.config.update(TESTING=True)
    return flask_app


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
