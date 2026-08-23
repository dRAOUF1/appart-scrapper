"""Tests de `routes/admin.py` — la surface la plus privilégiée du projet.

Ce blueprint permet de supprimer n'importe quel utilisateur, vider une table
et exécuter du SQL arbitraire. Deux choses comptent ici plus que le reste :

1. **le mur `@require_admin`** — testé sur la liste complète des URLs, pas sur
   un échantillon : une route ajoutée sans le décorateur est une élévation de
   privilège immédiate ;
2. **les deux allowlists de `truncate`** — celle de la route et celle du
   repository, qui divergent (voir TestTruncateTable).

Le reste vérifie que chaque action privilégiée est bien tracée dans le journal
d'audit : une suppression non journalisée est indétectable après coup.
"""

from __future__ import annotations

import io

import pytest

from tests.functional.conftest import make_admin_stats
from tests.helpers.factories import make_listing_row, make_search_row, make_user_row

EMPTY_ZIP = b"PK\x05\x06" + b"\x00" * 18

# Toutes les URLs du blueprint admin, méthode comprise. Construite à la main
# depuis routes/admin.py : une route oubliée ici n'est pas protégée par le test
# de privilège, d'où la vérification d'exhaustivité en fin de fichier.
ADMIN_URLS = [
    pytest.param("GET", "/admin", id="dashboard"),
    pytest.param("GET", "/admin/users", id="users"),
    pytest.param("GET", "/admin/users/1", id="user-detail"),
    pytest.param("POST", "/admin/users/1/delete", id="user-delete"),
    pytest.param("POST", "/admin/users/create", id="user-create"),
    pytest.param("GET", "/admin/searches", id="searches"),
    pytest.param("GET", "/admin/searches/1", id="search-detail"),
    pytest.param("POST", "/admin/searches/1/delete", id="search-delete"),
    pytest.param("POST", "/admin/searches/1/scrape", id="search-scrape"),
    pytest.param("GET", "/admin/searches/1/logs/export", id="logs-export"),
    pytest.param("POST", "/admin/searches/1/logs/import", id="logs-import"),
    pytest.param("GET", "/admin/listings", id="listings"),
    pytest.param("GET", "/admin/listings/sl_1", id="listing-detail"),
    pytest.param("POST", "/admin/listings/sl_1/delete", id="listing-delete"),
    pytest.param("POST", "/admin/listings/cleanup-orphan", id="listings-cleanup-orphan"),
    pytest.param("GET", "/admin/database", id="database"),
    pytest.param("GET", "/admin/database/table/users", id="table-detail"),
    pytest.param("POST", "/admin/database/query", id="database-query"),
    pytest.param("POST", "/admin/database/truncate", id="database-truncate"),
    pytest.param("GET", "/admin/logs", id="audit-logs"),
    pytest.param("POST", "/admin/logs/purge", id="audit-logs-purge"),
    pytest.param("POST", "/admin/cleanup", id="cleanup"),
]


@pytest.fixture(autouse=True)
def admin_views(storage):
    """Données de vue plausibles pour tous les onglets d'admin.html.

    Le template lit des clés précises sans garde (`stats.orphan_listings`,
    `db_stats.tables`) : les fournir ici évite que chaque test se transforme en
    chasse aux `UndefinedError` sans rapport avec ce qu'il vérifie.
    """
    storage.admin.get_enhanced_admin_stats.return_value = make_admin_stats()
    storage.admin.get_db_stats.return_value = {"tables": [], "database_size": "12 MB"}
    storage.admin.get_active_connections.return_value = []
    storage.admin.get_table_details.return_value = {"columns": [], "row_count": 0}
    storage.admin.get_admin_logs.return_value = []
    storage.admin.count_admin_logs.return_value = 0
    storage.admin.execute_query.return_value = ([], 0, None)
    storage.admin.truncate_table.return_value = True
    storage.admin.purge_old_logs.return_value = 0
    storage.users.get_all_users.return_value = []
    storage.users.get_user_detail.return_value = None
    storage.searches.get_all_searches.return_value = []
    storage.searches.get_search_detail.return_value = None
    storage.searches.get_search.return_value = None
    storage.listings.get_all_listings.return_value = []
    storage.listings.count_all_listings.return_value = 0
    storage.listings.get_listing_detail.return_value = None
    storage.listings.get_orphan_listings_count.return_value = 0
    storage.listings.delete_orphan_listings.return_value = 0
    storage.listings.delete_old_listings.return_value = 0
    return storage


# ---------------------------------------------------------------------------
# Le mur d'accès
# ---------------------------------------------------------------------------

class TestAdminAccessControl:
    @pytest.mark.parametrize(("method", "url"), ADMIN_URLS)
    def test_an_anonymous_visitor_never_reaches_an_admin_page(self, client, storage, method, url):
        """Deux refus valables selon la méthode, et c'est voulu : un GET est
        redirigé vers la connexion, un POST est d'abord arrêté par la
        protection CSRF (400) — le jeton est vérifié avant les décorateurs.
        Dans les deux cas, rien d'administratif n'est servi ni exécuté."""
        resp = client.open(url, method=method)

        if method == "GET":
            assert resp.status_code in (302, 303)
            assert "/login" in resp.headers["Location"]
        else:
            assert resp.status_code in (302, 303, 400)
            if resp.status_code in (302, 303):
                assert "/login" in resp.headers["Location"]
        storage.users.delete_user.assert_not_called()
        storage.admin.truncate_table.assert_not_called()
        storage.admin.execute_query.assert_not_called()

    @pytest.mark.parametrize(("method", "url"), ADMIN_URLS)
    def test_a_logged_in_non_admin_is_refused(self, web_client, storage, method, url):
        """`web_client` est Alice, connectée mais dont le nom ne correspond pas
        à ADMIN_USERNAME. Elle doit être renvoyée au tableau de bord, sans
        qu'aucune action privilégiée ne soit exécutée."""
        resp = web_client.open(url, method=method)

        assert resp.status_code in (302, 303)
        assert "/admin" not in resp.headers["Location"]
        storage.users.delete_user.assert_not_called()
        storage.admin.truncate_table.assert_not_called()
        storage.admin.execute_query.assert_not_called()

    @pytest.mark.parametrize(("method", "url"), ADMIN_URLS)
    def test_the_admin_gets_through(self, admin_client, method, url):
        """Contre-épreuve : le refus ci-dessus vient bien du privilège, pas
        d'une route cassée qui renverrait 302 pour tout le monde."""
        resp = admin_client.open(url, method=method)

        assert resp.status_code in (200, 302, 303, 404)
        if resp.status_code in (302, 303):
            assert "/login" not in resp.headers["Location"]

    def test_admin_access_is_fail_closed_without_the_env_var(self, monkeypatch, app_without_csrf, admin_user):
        """`ADMIN_USERNAME` absent ⇒ personne n'est admin, pas « tout le monde
        l'est ». La comparaison est faite sur une variable d'environnement :
        un déploiement qui l'oublie doit fermer la porte, pas l'ouvrir."""
        monkeypatch.delenv("ADMIN_USERNAME", raising=False)
        client = app_without_csrf.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = admin_user["id"]
            sess["username"] = admin_user["username"]

        resp = client.get("/admin")

        assert resp.status_code in (302, 303)
        assert "/admin" not in resp.headers["Location"]

    def test_the_url_list_covers_every_admin_route(self, app):
        """Garde-fou du garde-fou : si une route admin est ajoutée sans être
        listée ici, elle échapperait silencieusement aux tests de privilège
        ci-dessus. Ce test échoue alors, avec le nom de la route manquante."""
        declared = set()
        for rule in app.url_map.iter_rules():
            if not str(rule).startswith("/admin"):
                continue
            for method in rule.methods & {"GET", "POST", "PUT", "DELETE"}:
                declared.add((method, str(rule)))

        # Les URLs du test portent des identifiants concrets ; on compare sur
        # les motifs de règles Flask.
        tested_rules = set()
        for param in ADMIN_URLS:
            method, url = param.values
            match = app.url_map.bind("localhost").match(url, method=method, return_rule=True)
            tested_rules.add((method, str(match[0])))

        assert declared - tested_rules == set()


# ---------------------------------------------------------------------------
# Utilisateurs
# ---------------------------------------------------------------------------

class TestAdminUsers:
    def test_the_user_list_is_filtered_in_python_not_in_sql(self, admin_client, storage):
        """# BUG (performance) : `get_all_users()` charge la table entière, puis
        le filtre est appliqué en Python. Sans conséquence à 10 utilisateurs,
        mais la requête ne passe jamais de terme de recherche à la base."""
        storage.users.get_all_users.return_value = [
            make_user_row(id=1, username="alice"),
            make_user_row(id=2, username="bob"),
        ]

        resp = admin_client.get("/admin/users?search=ali")

        assert resp.status_code == 200
        storage.users.get_all_users.assert_called_once_with()

    @pytest.mark.parametrize(("term", "expected"), [("ALI", 1), ("ali", 1), ("o", 1), ("zzz", 0)])
    def test_the_search_term_is_case_insensitive(self, admin_client, storage, term, expected):
        storage.users.get_all_users.return_value = [
            make_user_row(id=1, username="alice"),
            make_user_row(id=2, username="bob"),
        ]

        resp = admin_client.get(f"/admin/users?search={term}")

        assert resp.status_code == 200

    def test_a_missing_user_detail_redirects_instead_of_rendering_nothing(self, admin_client, storage):
        storage.users.get_user_detail.return_value = None

        resp = admin_client.get("/admin/users/404")

        assert resp.status_code in (302, 303)

    def test_deleting_a_user_is_recorded_in_the_audit_log(self, admin_client, storage, admin_user):
        """Une suppression non journalisée est indétectable après coup : le
        journal est la seule trace qu'il reste."""
        storage.users.get_user_detail.return_value = make_user_row(id=5, username="victime")

        admin_client.post("/admin/users/5/delete")

        storage.users.delete_user.assert_called_once_with(5)
        action, details, performed_by = storage.admin.log_admin_action.call_args[0]
        assert action == "user_deleted"
        assert "victime" in details
        assert performed_by == admin_user["username"]

    def test_deleting_an_unknown_user_does_nothing_at_all(self, admin_client, storage):
        storage.users.get_user_detail.return_value = None

        admin_client.post("/admin/users/404/delete")

        storage.users.delete_user.assert_not_called()
        storage.admin.log_admin_action.assert_not_called()

    def test_an_admin_can_delete_their_own_account(self, admin_client, storage, admin_user):
        """# BUG : rien n'empêche l'admin de supprimer son propre compte. Comme
        l'accès admin repose sur la correspondance du username avec
        ADMIN_USERNAME, il se verrouille dehors — et personne ne peut recréer
        le compte, puisque la création est elle-même réservée à l'admin.
        Test figeant le comportement actuel."""
        storage.users.get_user_detail.return_value = make_user_row(
            id=admin_user["id"], username=admin_user["username"]
        )

        admin_client.post(f"/admin/users/{admin_user['id']}/delete")

        storage.users.delete_user.assert_called_once_with(admin_user["id"])

    def test_creating_a_user_normalizes_the_username(self, admin_client, storage):
        storage.users.create_user.return_value = make_user_row(id=7, username="charlie")

        admin_client.post("/admin/users/create", data={"username": "  CHARLIE  "})

        storage.users.create_user.assert_called_once_with("charlie")

    def test_creating_a_user_announces_no_token(self, admin_client, storage):
        """Le flash de création n'affiche plus aucun jeton (#30) : un secret
        dans un `flash` finit dans le cookie de session signé et dans le HTML
        de la page suivante (historique, caches)."""
        storage.users.create_user.return_value = make_user_row(id=8, username="dave")

        resp = admin_client.post("/admin/users/create", data={"username": "dave"}, follow_redirects=True)

        assert resp.status_code == 200
        assert b"Token" not in resp.data

    def test_creating_a_user_without_a_name_is_refused(self, admin_client, storage):
        admin_client.post("/admin/users/create", data={"username": "   "})

        storage.users.create_user.assert_not_called()

    def test_a_duplicate_username_is_reported_not_raised(self, admin_client, storage):
        """Le repository lève un `ValueError` porteur d'un message destiné à
        l'utilisateur : il doit devenir un flash, pas une 500."""
        storage.users.create_user.side_effect = ValueError("Le nom d'utilisateur 'bob' est déjà pris")

        resp = admin_client.post("/admin/users/create", data={"username": "bob"}, follow_redirects=True)

        assert resp.status_code == 200
        assert "déjà pris".encode() in resp.data


# ---------------------------------------------------------------------------
# Recherches et annonces
# ---------------------------------------------------------------------------

class TestAdminSearches:
    def test_deleting_a_search_is_audited_with_its_label(self, admin_client, storage, admin_user):
        storage.searches.get_search.return_value = make_search_row(id=3, label="Paris 13e")

        admin_client.post("/admin/searches/3/delete")

        storage.searches.delete_search.assert_called_once_with(3)
        action, details, _ = storage.admin.log_admin_action.call_args[0]
        assert action == "search_deleted"
        assert "Paris 13e" in details

    def test_scraping_runs_on_behalf_of_the_search_owner(self, admin_client, storage):
        """L'admin déclenche le scrape, mais celui-ci s'exécute avec le
        `user_id` du propriétaire : le service vérifie ensuite cette
        correspondance, et un mauvais id ferait échouer le scrape en silence."""
        storage.searches.get_search.return_value = make_search_row(id=3, user_id=8)

        with pytest.MonkeyPatch.context() as mp:
            calls = []
            mp.setattr("core.scrape_control.submit_scrape",
                       lambda app, sid, uid: calls.append((sid, uid)) or (True, "démarré"))
            admin_client.post("/admin/searches/3/scrape")

        assert calls == [(3, 8)]

    def test_scraping_an_unknown_search_redirects(self, admin_client, storage):
        storage.searches.get_search.return_value = None

        resp = admin_client.post("/admin/searches/404/scrape")

        assert resp.status_code in (302, 303)


class TestAdminListings:
    def test_the_listing_page_paginates(self, admin_client, storage):
        storage.listings.get_all_listings.return_value = [make_listing_row()]
        storage.listings.count_all_listings.return_value = 130

        resp = admin_client.get("/admin/listings?page=2")

        assert resp.status_code == 200
        kwargs = storage.listings.get_all_listings.call_args.kwargs
        assert kwargs["offset"] == kwargs["limit"]

    def test_cleaning_orphans_reports_how_many_were_removed(self, admin_client, storage):
        storage.listings.delete_orphan_listings.return_value = 17

        resp = admin_client.post("/admin/listings/cleanup-orphan", follow_redirects=True)

        assert resp.status_code == 200
        storage.listings.delete_orphan_listings.assert_called_once()


# ---------------------------------------------------------------------------
# Base de données — la partie la plus dangereuse
# ---------------------------------------------------------------------------

class TestExecuteQuery:
    def test_an_empty_query_is_refused_before_reaching_the_database(self, admin_client, storage):
        admin_client.post("/admin/database/query", data={"sql": "   "})

        storage.admin.execute_query.assert_not_called()

    def test_a_query_is_executed_and_audited(self, admin_client, storage, admin_user):
        storage.admin.execute_query.return_value = ([{"n": 1}], 1, None)

        admin_client.post("/admin/database/query", data={"sql": "SELECT 1"})

        storage.admin.execute_query.assert_called_once_with("SELECT 1")
        action, details, performed_by = storage.admin.log_admin_action.call_args[0]
        assert action == "db_query"
        assert details == "SELECT 1"
        assert performed_by == admin_user["username"]

    def test_a_long_query_is_truncated_in_the_audit_log(self, admin_client, storage):
        """Le journal garde 200 caractères : assez pour reconnaître la requête,
        pas assez pour qu'un `INSERT` massif y recopie ses données."""
        long_sql = "SELECT " + "x" * 500

        admin_client.post("/admin/database/query", data={"sql": long_sql})

        _, details, _ = storage.admin.log_admin_action.call_args[0]
        assert len(details) == 200
        assert long_sql.startswith(details)

    def test_a_failing_query_is_reported_as_a_message_not_a_crash(self, admin_client, storage):
        """`execute_query` renvoie l'erreur en troisième valeur au lieu de la
        lever : la page doit la montrer et rester utilisable."""
        storage.admin.execute_query.return_value = ([], 0, 'relation "nawak" does not exist')

        resp = admin_client.post("/admin/database/query", data={"sql": "SELECT * FROM nawak"},
                                 follow_redirects=True)

        assert resp.status_code == 200
        assert b"nawak" in resp.data

    def test_a_failing_query_is_still_audited(self, admin_client, storage):
        """Une tentative ratée reste une tentative : elle doit laisser une
        trace, sinon un balayage d'erreurs passerait inaperçu."""
        storage.admin.execute_query.return_value = ([], 0, "boom")

        admin_client.post("/admin/database/query", data={"sql": "DROP TABLE users"})

        storage.admin.log_admin_action.assert_called_once()

    def test_the_result_set_is_returned_whole(self, admin_client, storage):
        """# BUG (disponibilité) : aucune limite de lignes. `SELECT * FROM
        listings` sur une grosse table charge tout en mémoire, le sérialise
        dans le HTML, et peut faire tomber le process."""
        storage.admin.execute_query.return_value = ([{"i": i} for i in range(5000)], 5000, None)

        resp = admin_client.post("/admin/database/query", data={"sql": "SELECT * FROM listings"})

        assert resp.status_code == 200


class TestTruncateTable:
    HOSTILE = [
        pytest.param("users; DROP DATABASE appart", id="chainage-de-requete"),
        pytest.param("users --", id="commentaire"),
        pytest.param("*", id="joker"),
        pytest.param("pg_shadow", id="table-systeme"),
        pytest.param("Users", id="casse-differente"),
        pytest.param("users ", id="espace-final-non-normalise"),
    ]

    @pytest.mark.parametrize("table_name", HOSTILE)
    def test_a_name_outside_the_allowlist_never_reaches_the_repository(
        self, admin_client, storage, table_name
    ):
        """🔒 `truncate_table` interpole le nom de table dans le SQL (`TRUNCATE
        TABLE {name} CASCADE`) : l'allowlist est la seule protection. Elle doit
        rejeter avant l'appel, pas compter sur le repository.

        Note : « users » avec un espace final est accepté par le `.strip()` de
        la route — c'est bien la valeur nettoyée qui est comparée.
        """
        resp = admin_client.post("/admin/database/truncate", data={"table_name": table_name})

        assert resp.status_code in (302, 303)
        if table_name.strip() not in {"users", "searches", "listings", "search_listings",
                                      "scrape_logs", "admin_logs"}:
            storage.admin.truncate_table.assert_not_called()

    def test_an_allowed_table_is_truncated_and_audited(self, admin_client, storage):
        admin_client.post("/admin/database/truncate", data={"table_name": "listings"})

        storage.admin.truncate_table.assert_called_once_with("listings")
        action, details, _ = storage.admin.log_admin_action.call_args[0]
        assert action == "table_truncated"
        assert "listings" in details

    def test_an_empty_name_is_refused(self, admin_client, storage):
        admin_client.post("/admin/database/truncate", data={"table_name": "  "})

        storage.admin.truncate_table.assert_not_called()

    def test_a_repository_refusal_is_reported(self, admin_client, storage):
        storage.admin.truncate_table.return_value = False

        resp = admin_client.post("/admin/database/truncate", data={"table_name": "listings"},
                                 follow_redirects=True)

        assert resp.status_code == 200
        storage.admin.log_admin_action.assert_not_called()

    def test_the_route_and_repository_allowlists_disagree(self):
        """# BUG : les deux listes ne sont pas les mêmes.

        - `app_settings` est autorisée par le repository mais pas par la route :
          impossible à vider depuis l'interface, alors que le repo le permet à
          tout appelant interne ;
        - `scrape_logs` figure dans les DEUX alors que **cette table n'existe
          plus** — les logs de scrape sont des fichiers depuis le passage à
          `scrape_logs/storage.py`. La vider renverrait une erreur Postgres.

        Une seule liste, partagée, éviterait les deux problèmes. Test figeant
        l'écart actuel pour qu'il soit visible.
        """
        import inspect

        from repositories.admin_repo import AdminRepository
        from routes import admin as admin_routes

        route_src = inspect.getsource(admin_routes.admin_truncate_table)
        repo_src = inspect.getsource(AdminRepository.truncate_table)

        assert "app_settings" in repo_src
        assert "app_settings" not in route_src
        # La table fantôme, présente des deux côtés.
        assert "scrape_logs" in route_src
        assert "scrape_logs" in repo_src


class TestAdminAuditLog:
    def test_the_audit_log_paginates_and_passes_its_filters_down(self, admin_client, storage):
        storage.admin.count_admin_logs.return_value = 120

        resp = admin_client.get("/admin/logs?page=2&action=user_deleted&date_from=2026-01-01")

        assert resp.status_code == 200
        kwargs = storage.admin.get_admin_logs.call_args.kwargs
        assert kwargs["action_filter"] == "user_deleted"
        assert kwargs["date_from"] == "2026-01-01"
        assert kwargs["offset"] == kwargs["limit"]

    def test_an_unparsable_page_number_falls_back_to_the_first_page(self, admin_client, storage):
        resp = admin_client.get("/admin/logs?page=nawak")

        assert resp.status_code == 200
        assert storage.admin.get_admin_logs.call_args.kwargs["offset"] == 0

    def test_purging_reports_the_number_of_deleted_rows(self, admin_client, storage):
        storage.admin.purge_old_logs.return_value = 42

        resp = admin_client.post("/admin/logs/purge", data={"days": "7"}, follow_redirects=True)

        assert resp.status_code == 200
        storage.admin.purge_old_logs.assert_called_once_with(days=7)


class TestAdminLogsImport:
    def test_an_archive_from_another_search_asks_for_confirmation(self, admin_client, storage):
        """`override_required` est une chaîne magique renvoyée par l'exporteur :
        elle doit devenir un message compréhensible, pas une erreur brute."""
        storage.searches.get_search.return_value = make_search_row(id=1)
        storage.searches.get_search_detail.return_value = make_search_row(id=1)
        storage.scrape_logs.import_scrape_logs.side_effect = ValueError("override_required")

        resp = admin_client.post(
            "/admin/searches/1/logs/import",
            data={"log_archive": (io.BytesIO(EMPTY_ZIP), "logs.zip")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        assert resp.status_code == 200
        assert b"override" in resp.data.lower()

    def test_the_temporary_archive_is_removed_even_when_the_import_fails(self, admin_client, storage, tmp_path):
        storage.searches.get_search.return_value = make_search_row(id=1)
        storage.searches.get_search_detail.return_value = make_search_row(id=1)
        storage.scrape_logs.import_scrape_logs.side_effect = ValueError("archive corrompue")

        admin_client.post(
            "/admin/searches/1/logs/import",
            data={"log_archive": (io.BytesIO(EMPTY_ZIP), "logs.zip")},
            content_type="multipart/form-data",
        )

        # Le fichier temporaire porte un nom prévisible dans /tmp ; ce qui
        # compte ici est qu'il ne survive pas à l'échec.
        leftovers = list(__import__("pathlib").Path("/tmp").glob("admin_logs_import_1_*.zip"))
        assert leftovers == []
