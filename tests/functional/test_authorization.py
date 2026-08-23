"""Balayage systématique du contrôle de propriété sur les recherches.

Le motif `get_search(id)` → `search["user_id"] != g.user["id"]` → refus est
recopié à la main dans chaque endpoint. Rien dans le code ne l'impose : une
seule omission, sur une seule route, est une IDOR — n'importe qui pourrait lire,
modifier ou supprimer la recherche d'un autre en changeant un entier dans l'URL.

Ce fichier existe pour rendre cette omission impossible à commettre en silence.
Il ne teste pas le comportement métier de chaque route (c'est le travail de
test_web_pages.py et test_search_urls.py), mais une seule chose, sur toutes :

    la recherche appartient à quelqu'un d'autre  ⇒  refus, et aucune écriture.

Règle de forme : le web redirige avec un message (ou 404 JSON pour les routes
de données comme `/urls`), sans jamais rendre la page. Les anciens endpoints API,
qui répondaient 404 et jamais 403, ont disparu avec le token (#30).
"""

from __future__ import annotations

import io

import pytest

from tests.helpers.factories import make_search_row

# Marqueur d'un envoi de fichier : le corps doit être reconstruit à CHAQUE
# appel. Un `BytesIO` placé dans les paramètres serait partagé par tous les
# tests du parametrize et se retrouverait vide dès le deuxième.
UPLOAD = "__upload__"

# Un ZIP vide minimal (End Of Central Directory seul), assez pour que Flask
# accepte le fichier — le contenu n'a pas d'importance, le refus doit tomber
# avant qu'il soit lu.
EMPTY_ZIP = b"PK\x05\x06" + b"\x00" * 18


def _request(client, method, url, kwargs):
    """Joue la requête, en fabriquant un corps neuf pour les envois de fichier."""
    if kwargs == UPLOAD:
        kwargs = {
            "data": {"log_archive": (io.BytesIO(EMPTY_ZIP), "logs.zip")},
            "content_type": "multipart/form-data",
        }
    return client.open(url, method=method, **kwargs)


# Une recherche qui existe, mais qui appartient à Bob (id 2). L'utilisateur
# authentifié dans les fixtures est Alice (id 1).
OTHER_USER_ID = 2
SEARCH_ID = 42
LOG_ID = 7


@pytest.fixture
def foreign_search(storage):
    """`get_search` renvoie une recherche d'un AUTRE utilisateur."""
    row = make_search_row(id=SEARCH_ID, user_id=OTHER_USER_ID, label="La recherche de Bob")
    storage.searches.get_search.return_value = row
    return row


@pytest.fixture
def missing_search(storage):
    """`get_search` ne trouve rien — même réponse attendue qu'une recherche
    étrangère, précisément pour ne pas distinguer les deux cas."""
    storage.searches.get_search.return_value = None
    return


def _mutating_calls(storage):
    """Toutes les méthodes d'écriture appelées sur le storage, tous repos confondus.

    Sert d'assertion négative : un refus qui écrit quand même serait pire
    qu'un refus manquant, parce qu'il passerait pour correct.
    """
    write_prefixes = ("create", "update", "delete", "toggle", "mark", "save", "set", "reset",
                      "purge", "truncate", "import", "link")
    called = []
    for repo_name in ("users", "searches", "listings", "scrape_logs", "admin", "settings", "seloger_geo"):
        repo = getattr(storage, repo_name)
        for attr in dir(repo):
            if attr.startswith("_") or not attr.startswith(write_prefixes):
                continue
            method = getattr(repo, attr)
            if getattr(method, "called", False):
                called.append(f"{repo_name}.{attr}")
    return called


# ---------------------------------------------------------------------------
# Web — redirection, jamais la page
# ---------------------------------------------------------------------------

WEB_ENDPOINTS = [
    pytest.param("GET", f"/searches/{SEARCH_ID}/urls", {}, id="GET-urls"),
    pytest.param("POST", f"/searches/{SEARCH_ID}/delete", {}, id="POST-delete"),
    pytest.param("POST", f"/searches/{SEARCH_ID}/scrape", {}, id="POST-scrape"),
    pytest.param("POST", f"/searches/{SEARCH_ID}/interval", {"data": {"interval": "10"}}, id="POST-interval"),
    pytest.param("POST", f"/searches/{SEARCH_ID}/toggle-active", {}, id="POST-toggle-active"),
    pytest.param("POST", f"/searches/{SEARCH_ID}/blacklist-agencies", {"data": {}}, id="POST-blacklist-agencies"),
    pytest.param("POST", f"/searches/{SEARCH_ID}/blacklist-mode",
                 {"data": {"blacklist_mode": "exclude"}}, id="POST-blacklist-mode"),
    pytest.param("GET", f"/searches/{SEARCH_ID}/edit", {}, id="GET-edit"),
    pytest.param("POST", f"/searches/{SEARCH_ID}/edit", {"data": {"label": "vol"}}, id="POST-edit"),
    pytest.param("GET", f"/searches/{SEARCH_ID}/logs", {}, id="GET-logs"),
    pytest.param("GET", f"/searches/{SEARCH_ID}/logs/live", {}, id="GET-logs-live"),
    pytest.param("GET", f"/searches/{SEARCH_ID}/logs/{LOG_ID}/raw", {}, id="GET-log-raw"),
    pytest.param("GET", f"/searches/{SEARCH_ID}/logs/{LOG_ID}/download", {}, id="GET-log-download"),
    pytest.param("GET", f"/searches/{SEARCH_ID}/logs/export", {}, id="GET-logs-export"),
    pytest.param("POST", f"/searches/{SEARCH_ID}/logs/import", UPLOAD, id="POST-logs-import"),
    pytest.param("GET", f"/listings/{SEARCH_ID}", {}, id="GET-listings"),
]


class TestWebOwnership:
    @pytest.mark.parametrize(("method", "url", "kwargs"), WEB_ENDPOINTS)
    def test_a_search_owned_by_someone_else_never_renders(
        self, web_client, storage, foreign_search, method, url, kwargs
    ):
        """Le refus est une redirection : l'utilisateur revient à sa liste avec
        un message, et le contenu de la recherche d'autrui n'apparaît nulle
        part — pas même son libellé dans un titre de page."""
        resp = _request(web_client, method, url, kwargs)

        assert resp.status_code in (302, 303, 404), f"{method} {url} a répondu {resp.status_code}"
        assert b"La recherche de Bob" not in resp.data
        assert _mutating_calls(storage) == []

    @pytest.mark.parametrize(("method", "url", "kwargs"), WEB_ENDPOINTS)
    def test_a_missing_search_is_refused_the_same_way(
        self, web_client, storage, missing_search, method, url, kwargs
    ):
        resp = _request(web_client, method, url, kwargs)

        assert resp.status_code in (302, 303, 404)
        assert _mutating_calls(storage) == []

    @pytest.mark.parametrize(("method", "url", "kwargs"), WEB_ENDPOINTS)
    def test_an_anonymous_visitor_is_sent_to_the_login_page(self, client, storage, method, url, kwargs):
        resp = _request(client, method, url, kwargs)

        assert resp.status_code in (302, 303, 400)
        if resp.status_code in (302, 303):
            assert "/login" in resp.headers["Location"]
            storage.searches.get_search.assert_not_called()
        assert _mutating_calls(storage) == []


class TestRawLogOwnership:
    """Un `log_id` est un identifiant **global** : `find_entry_any` balaie tous
    les répertoires de recherches, toutes recherches et tous utilisateurs
    confondus. La seule barrière est le `user_id` passé au repository."""

    def test_the_raw_log_lookup_is_scoped_to_the_current_user(self, web_client, storage, user):
        storage.searches.get_search.return_value = make_search_row(id=SEARCH_ID, user_id=user["id"])
        storage.scrape_logs.get_scrape_log_raw.return_value = None

        web_client.get(f"/searches/{SEARCH_ID}/logs/{LOG_ID}/raw")

        # Le user_id est bien transmis : sans lui, le repository renverrait le
        # log de n'importe qui.
        storage.scrape_logs.get_scrape_log_raw.assert_called_once_with(LOG_ID, user["id"])

    def test_a_log_belonging_to_another_search_of_the_same_user_is_still_served(
        self, web_client, storage, user
    ):
        """# BUG : la route vérifie que la *recherche* de l'URL appartient à
        l'utilisateur, et que le *log* lui appartient — mais jamais que le log
        appartient à cette recherche-là. Un log de la recherche 99 s'affiche
        donc sous l'URL de la recherche 42, avec le titre de la 42.

        Fuite mineure (les deux recherches sont au même utilisateur), mais
        l'affichage est trompeur. Test figeant le comportement actuel.
        """
        from datetime import datetime

        storage.searches.get_search.return_value = make_search_row(id=SEARCH_ID, user_id=user["id"])
        storage.scrape_logs.get_scrape_log_raw.return_value = {
            "id": LOG_ID,
            "search_id": 99,  # une AUTRE recherche
            "raw_logs": "contenu du log de la recherche 99",
            "status": "success",
            "started_at": datetime(2026, 7, 1, 10, 0, 0),
            "completed_at": datetime(2026, 7, 1, 10, 0, 30),
            "duration_sec": 30,
            "listings_found": 3,
            "new_listings": 1,
            "error_message": "",
        }

        resp = web_client.get(f"/searches/{SEARCH_ID}/logs/{LOG_ID}/raw")

        assert resp.status_code == 200
