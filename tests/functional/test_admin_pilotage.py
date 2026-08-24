"""Pilotage du scraping dans l'admin (issue #17).

Ce que ce module garantit, critère par critère de l'issue :

1. **pause globale** — la clé `scheduler_paused` bascule via POST admin
   (toast + audit + bandeau visible partout), et se propage aux fragments
   HTMX par swap hors bande ;
2. **toggle par recherche** — bouton liste/détail branché sur
   `toggle_search_active`, état réel reflété après reload, audit ;
3. **file d'attente live** — le fragment pollé montre les scrapes soumis
   MANUELLEMENT (critère d'acceptation), leur rang en file, l'occupation de
   l'executor, le détenteur du verrou 727271 et l'état de pause — jamais 500 ;
4. **scrape en masse** — toutes les recherches actives / d'une source,
   compteurs soumises/déjà-en-file, confirmation modale, audit récapitulatif ;
5. **CSRF** — aucune requête mutante sans jeton (champ ou X-CSRFToken).

Les patterns HTMX (fragment + HX-Trigger, modale data-confirm) sont ceux du
socle #16 : ils sont re-testés ici SUR les nouvelles routes.
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from tests.functional.conftest import ADMIN_USERNAME, make_admin_stats
from tests.helpers.factories import make_search_row, make_user_row

# URLs des sept onglets, pour vérifier que le bandeau suit partout (#19 : + Scrapes).
URLS_ONGLETS = [
    "/admin",
    "/admin/users",
    "/admin/searches",
    "/admin/listings",
    "/admin/scrapes",
    "/admin/database",
    "/admin/logs",
]


class FileExecutorFactice:
    """`submit()` enregistre et renvoie un Future JAMAIS résolu.

    Simule fidèlement le ThreadPoolExecutor(max_workers=1) de production vu
    de l'extérieur : le job part, son future reste « en cours » — exactement
    ce qu'il faut pour lire la file pendant le test. `max_workers` porte le
    nom public lu par `_contexte_file` via getattr(..., "_max_workers", 1).
    """

    max_workers = 1

    def __init__(self):
        self.soumissions: list[tuple] = []

    def submit(self, fn, *args):
        self.soumissions.append(args)
        return Future()


@pytest.fixture(autouse=True)
def pilotage_views(storage):
    """Contexte dashboard plausible pour tous les tests de ce module."""
    storage.admin.get_enhanced_admin_stats.return_value = make_admin_stats()
    storage.admin.execute_query.return_value = ([], 0, None)
    storage.admin.count_admin_logs.return_value = 0
    storage.searches.get_all_searches.return_value = []
    storage.searches.get_search.return_value = None
    storage.searches.get_search_detail.return_value = None
    storage.listings.count_all_listings.return_value = 0
    storage.listings.get_orphan_listings_count.return_value = 0
    return storage


@pytest.fixture
def file_propre():
    """Isole les globales de pilotage partagées par toutes les apps du process
    (cf. tests/functional/conftest.py : `_scrape_executor`/`_scrape_futures`
    sont déjà remplacés par test) : timestamps et futures résiduels sortent."""
    import core.scrape_control as core_scrape_control
    import main

    core_scrape_control._soumissions.clear()
    main._scrape_futures.clear()
    yield
    core_scrape_control._soumissions.clear()
    main._scrape_futures.clear()


def pose_pause(storage, valeur: str) -> None:
    """Configure la lecture app_settings comme le ferait le vrai repo."""
    storage.settings.get_setting.side_effect = lambda cle, defaut="": (
        valeur if cle == "scheduler_paused" else defaut
    )


def pose_toggle_dynamique(storage, valeur_initiale: str = "false"):
    """Branché get/set cohérents : la relecture reflète l'écriture, comme la
    vraie table app_settings — indispensable pour vérifier que le fragment
    renvoyé par le toggle montre DÉJÀ le nouvel état."""
    etat = {"valeur": valeur_initiale}

    def _get(cle, defaut=""):
        return etat["valeur"] if cle == "scheduler_paused" else defaut

    def _set(cle, valeur):
        if cle == "scheduler_paused":
            etat["valeur"] = valeur

    storage.settings.get_setting.side_effect = _get
    storage.settings.set_setting.side_effect = _set
    return etat


def soumet_recherche(app, search_id: int, user_id: int = 1) -> None:
    """Soumission MANUELLE réelle (core.submit_scrape) sur un executor factice :
    c'est exactement le chemin d'une route admin_scrape_search, la vue file
    doit donc montrer ce qu'il laisse derrière lui."""
    import core.scrape_control

    core.scrape_control.submit_scrape(app, search_id=search_id, user_id=user_id)


def client_admin(app, storage):
    """Client connecté en admin : `require_admin` résout la session via
    get_user_by_id (comme le fixture conftest admin_client), il faut donc
    brancher ce lookup sur la ligne attendue."""
    row = make_user_row(id=99, username=ADMIN_USERNAME)
    storage.users.get_user_by_id.side_effect = lambda uid: row if uid == row["id"] else None
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = row["id"]
        sess["username"] = row["username"]
    return client


@pytest.fixture
def admin_client_csrf(app, admin_user):
    """Client connecté en admin sur l'app AVEC protection CSRF active — même
    fixture que tests/functional/test_admin_htmx.py, redéfinie ici pour éprouver
    la garde sur les nouvelles routes (#17) sans coupler les deux modules."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_user["id"]
        sess["username"] = admin_user["username"]
    return client


# ---------------------------------------------------------------------------
# 1. Bandeau de pause globale
# ---------------------------------------------------------------------------


class TestBandeauPauseGlobal:
    def test_l_ancre_du_bandeau_existe_meme_hors_pause(self, admin_client):
        """L'ancre reste dans le DOM (vide) : c'est elle que le swap
        hx-swap-oob vient remplacer quand la pause s'active ailleurs."""
        page = admin_client.get("/admin").data.decode()

        assert 'id="admin-pause-zone"' in page
        assert "pause-banner" not in page

    @pytest.mark.parametrize("url", URLS_ONGLETS)
    def test_le_bandeau_apparait_sur_tous_les_onglets(self, admin_client, storage, url):
        """Critère #17 : l'état de pause doit être visible PARTOUT dans
        l'admin, pas seulement sur le dashboard qui a servi au toggle."""
        pose_pause(storage, "true")

        page = admin_client.get(url).data.decode()

        assert "pause-banner" in page
        assert "Scheduler en pause globale" in page
        assert "Reprendre le scheduler" in page

    def test_le_fragment_htmx_transport_le_bandeau_hors_bande(self, admin_client, storage):
        """Comme la nav d'onglets (#16), le bandeau voyage en hx-swap-oob :
        une navigation HTMX suffit à rafraîchir son état."""
        pose_pause(storage, "true")

        corps = admin_client.get("/admin/searches", headers={"HX-Request": "true"}).data.decode()

        assert "<html" not in corps
        assert 'id="admin-pause-zone" hx-swap-oob="true"' in corps


# ---------------------------------------------------------------------------
# 2. Toggle de la pause globale (POST /admin/scheduler/pause)
# ---------------------------------------------------------------------------


class TestTogglePauseAdmin:
    def test_le_post_htmx_active_la_pause_avec_toast_et_audit(self, admin_client, storage):
        pose_toggle_dynamique(storage, "false")

        reponse = admin_client.post("/admin/scheduler/pause", headers={"HX-Request": "true"})

        assert reponse.status_code == 200
        assert "Location" not in reponse.headers

        storage.settings.set_setting.assert_called_once_with("scheduler_paused", "true")
        appel = storage.admin.log_admin_action.call_args
        assert appel.args[0] == "scheduler_pause_toggled"
        assert "mis en pause" in appel.args[1]

        declencheurs = json.loads(reponse.headers["HX-Trigger"])
        toast = declencheurs["admin:toast"]
        assert "mis en pause" in toast["message"]
        assert toast["category"] == "warning"

        # Le fragment retourné porte déjà le nouvel état (bandeau oob inclus).
        corps = reponse.data.decode()
        assert "pause-banner" in corps
        assert 'id="admin-pause-zone" hx-swap-oob="true"' in corps

    def test_le_second_post_reprend_le_scheduler(self, admin_client, storage):
        pose_toggle_dynamique(storage, "false")
        admin_client.post("/admin/scheduler/pause")

        reponse = admin_client.post("/admin/scheduler/pause", headers={"HX-Request": "true"})

        valeurs = [c.args for c in storage.settings.set_setting.call_args_list]
        assert valeurs == [("scheduler_paused", "true"), ("scheduler_paused", "false")]
        declencheurs = json.loads(reponse.headers["HX-Trigger"])
        assert declencheurs["admin:toast"]["category"] == "success"
        assert "repris" in declencheurs["admin:toast"]["message"]

    def test_sans_js_le_post_classique_redirige_avec_flash(self, admin_client, storage):
        pose_toggle_dynamique(storage, "false")

        reponse = admin_client.post("/admin/scheduler/pause", follow_redirects=True)

        assert reponse.status_code == 200
        corps = reponse.data.decode()
        assert "toast-warning" in corps
        assert "mis en pause" in corps
        assert "<html" in corps

    def test_la_requete_mutante_sans_jeton_est_refusee(self, admin_client_csrf):
        reponse = admin_client_csrf.post(
            "/admin/scheduler/pause", headers={"HX-Request": "true"}
        )

        assert reponse.status_code == 400


# ---------------------------------------------------------------------------
# 3. Fragment file d'attente live (?fragment=queue)
# ---------------------------------------------------------------------------


class TestFragmentFileDAttente:
    @pytest.fixture
    def client_avec_file(self, app_without_csrf, storage, file_propre):
        """App dont l'executor est factice : les soumissions laissent des
        futures vivants, lisibles par le fragment. Retourne (client, app)."""
        app_without_csrf._scrape_executor = FileExecutorFactice()
        return client_admin(app_without_csrf, storage), app_without_csrf

    def get_file(self, client) -> str:
        reponse = client.get(
            "/admin?tab=dashboard&fragment=queue", headers={"HX-Request": "true"}
        )
        assert reponse.status_code == 200
        return reponse.data.decode()

    def test_le_pattern_de_polling_du_socle_est_respecte(self, client_avec_file):
        client, _app = client_avec_file
        corps = self.get_file(client)

        assert "<html" not in corps
        assert 'id="admin-queue-zone"' in corps
        assert "every 5s" in corps, "l'attribut de polling vit sur l'élément échangé"
        assert "fragment=queue" in corps

    def test_un_scrape_lance_manuellement_est_visible_en_rang_1(self, client_avec_file, storage):
        """Critère d'acceptation : la vue file montre bien un scrape lancé
        MANUELLEMENT — ici via submit_scrape, le chemin réel de la route."""
        client, app = client_avec_file
        recherche = make_search_row(id=11, label="Paris 13e T2", source="seloger", user_id=1)
        storage.searches.get_search.side_effect = lambda sid: recherche if sid == 11 else None

        soumet_recherche(app, search_id=11, user_id=1)

        corps = self.get_file(client)
        assert "Paris 13e T2" in corps
        assert "En cours" in corps
        assert "il y a 0 s" in corps

    def test_les_rangs_se_suivent_pour_plusieurs_scrapes(self, client_avec_file, storage):
        client, app = client_avec_file
        lignes = {
            11: make_search_row(id=11, label="Première", source="seloger"),
            12: make_search_row(id=12, label="Deuxième", source="laforet"),
            13: make_search_row(id=13, label="Troisième", source="bienici"),
        }
        storage.searches.get_search.side_effect = lambda sid: lignes.get(sid)

        for sid in (11, 12, 13):
            soumet_recherche(app, search_id=sid, user_id=1)

        corps = self.get_file(client)
        assert "Première" in corps and "Deuxième" in corps and "Troisième" in corps
        assert "En attente (rang 3)" in corps
        assert "seloger" in corps and "laforet" in corps

    def test_le_detenteur_du_verrou_consultatif_est_affiche(self, client_avec_file, storage):
        client, _app = client_avec_file
        storage.admin.execute_query.return_value = (
            [{"usename": "appart", "application_name": "appart-scrapper"}],
            1,
            None,
        )

        corps = self.get_file(client)

        assert "Verrou : appart-scrapper" in corps
        sql_execute = storage.admin.execute_query.call_args.args[0]
        assert "pg_locks" in sql_execute
        assert "727271" in sql_execute, "la clé du verrou consultatif est lue, jamais devinée"

    def test_le_prochain_passage_planifie_est_affiche(self, client_avec_file, storage):
        client, app = client_avec_file
        scheduler = MagicMock()
        scheduler.get_job.return_value.next_run_time = datetime(2026, 8, 24, 12, 0, 30)
        app._scheduler = scheduler

        corps = self.get_file(client)

        assert "Prochain passage planifié" in corps
        assert "12:00:30" in corps

    def test_une_base_muette_ne_provoque_jamais_500(self, client_avec_file, storage):
        """Robustesse exigée : le fragment est pollé toutes les 5 s, chaque
        lecture périphérique doit dégrader en badge neutre, jamais en erreur."""
        client, _app = client_avec_file
        storage.admin.execute_query.side_effect = RuntimeError("pool épuisé")

        corps = self.get_file(client)

        assert "Verrou non détenu ici" in corps
        assert "Aucun scrape en cours" in corps

    def test_letat_de_pause_est_visible_dans_la_file(self, client_avec_file, storage):
        client, _app = client_avec_file
        pose_pause(storage, "true")

        corps = self.get_file(client)

        assert "Planification en pause" in corps

    def test_une_file_vide_annonce_l_absence_de_scrapes(self, client_avec_file):
        client, _app = client_avec_file

        corps = self.get_file(client)

        assert "Aucun scrape en cours ni en attente" in corps


# ---------------------------------------------------------------------------
# 4. Scrape en masse (POST /admin/scrapes/bulk)
# ---------------------------------------------------------------------------


class TestScrapeEnMasse:
    @pytest.fixture
    def client_bulk(self, app_without_csrf, storage, file_propre):
        app_without_csrf._scrape_executor = FileExecutorFactice()
        return client_admin(app_without_csrf, storage), app_without_csrf

    def test_toutes_les_recherches_actives_partent_en_file(self, client_bulk, storage):
        client, app = client_bulk
        actives = [
            make_search_row(id=1, user_id=4, sources=["seloger"]),
            make_search_row(id=2, user_id=5, sources=["seloger", "laforet"]),
        ]
        storage.searches.get_all_searches.return_value = actives + [
            make_search_row(id=9, is_active=False),
        ]

        reponse = client.post(
            "/admin/scrapes/bulk", data={"cible": "all"}, headers={"HX-Request": "true"}
        )

        assert reponse.status_code == 200
        toast = json.loads(reponse.headers["HX-Trigger"])["admin:toast"]
        assert toast["message"] == "2 scrape(s) lancé(s) pour toutes recherches actives"
        assert toast["category"] == "success"

        executor = app._scrape_executor
        # La soumission passe par submit_scrape : (search_id, user_id) par job.
        assert sorted(executor.soumissions) == [(1, 4), (2, 5)], \
            "l'inactive n'est jamais soumise, chaque owner est transmis"

    def test_le_filtre_par_source_n_embarque_que_sa_recherches(self, client_bulk, storage):
        client, app = client_bulk
        storage.searches.get_all_searches.return_value = [
            make_search_row(id=1, sources=["seloger"]),
            make_search_row(id=2, sources=["laforet"]),
        ]

        reponse = client.post(
            "/admin/scrapes/bulk", data={"cible": "laforet"}, headers={"HX-Request": "true"}
        )

        toast = json.loads(reponse.headers["HX-Trigger"])["admin:toast"]
        assert toast["message"] == "1 scrape(s) lancé(s) pour source 'laforet'"
        assert app._scrape_executor.soumissions == [(2, 1)]

    def test_une_recherche_deja_en_file_est_comptee_sans_etre_doublee(self, client_bulk, storage):
        """La dédup existante fait le travail : le bulk la signale, il ne la
        relance pas — et la file reste séquentielle (max_workers=1)."""
        client, app = client_bulk
        storage.searches.get_all_searches.return_value = [
            make_search_row(id=1, sources=["seloger"]),
            make_search_row(id=2, sources=["seloger"]),
        ]
        soumet_recherche(app, search_id=1, user_id=1)  # déjà en cours
        deja_soumises = len(app._scrape_executor.soumissions)

        reponse = client.post(
            "/admin/scrapes/bulk", data={"cible": "all"}, headers={"HX-Request": "true"}
        )
        assert reponse.status_code == 200

        toast = json.loads(reponse.headers["HX-Trigger"])["admin:toast"]
        assert toast["message"] == (
            "1 scrape(s) lancé(s) pour toutes recherches actives — 1 déjà en file d'attente"
        )
        # Seule la recherche 2 a été ajoutée par le bulk : la 1 n'est PAS doublée.
        assert app._scrape_executor.soumissions[deja_soumises:] == [(2, 1)]

    def test_aucune_recherche_active_donne_un_message_info(self, client_bulk, storage):
        client, _app = client_bulk
        storage.searches.get_all_searches.return_value = []

        reponse = client.post(
            "/admin/scrapes/bulk", data={"cible": "all"}, headers={"HX-Request": "true"}
        )
        assert reponse.status_code == 200, reponse.data[:500]

        toast = json.loads(reponse.headers["HX-Trigger"])["admin:toast"]
        assert toast["message"] == "Aucune recherche active pour toutes recherches actives"
        assert toast["category"] == "info"

    def test_chaque_bulk_est_trace_dans_le_journal_d_audit(self, client_bulk, storage):
        client, _app = client_bulk
        storage.searches.get_all_searches.return_value = [
            make_search_row(id=1, sources=["seloger"]),
            make_search_row(id=2, sources=["seloger"], is_active=False),
        ]

        client.post("/admin/scrapes/bulk", data={"cible": "all"})

        appel = storage.admin.log_admin_action.call_args
        assert appel.args[0] == "bulk_scrape"
        assert "toutes recherches actives" in appel.args[1]
        assert "1 submitted" in appel.args[1]

    def test_le_dashboard_propose_les_boutons_bulk_avec_confirmation(self, client_bulk, storage):
        client, _app = client_bulk
        storage.searches.get_all_searches.return_value = [
            make_search_row(id=1, sources=["seloger"]),
            make_search_row(id=2, sources=["seloger"]),
            make_search_row(id=3, sources=["laforet"]),
        ]

        page = client.get("/admin").data.decode()

        assert "Toutes les recherches actives" in page
        assert 'name="cible" value="all"' in page
        assert 'name="cible" value="laforet"' in page
        assert "data-confirm=\"Lancer le scraping des 3 recherche(s) active(s) ?\"" in page

    def test_la_requete_mutante_sans_jeton_est_refusee(self, admin_client_csrf):
        reponse = admin_client_csrf.post("/admin/scrapes/bulk", data={"cible": "all"})

        assert reponse.status_code == 400


# ---------------------------------------------------------------------------
# 5. Toggle actif/pause par recherche
# ---------------------------------------------------------------------------


class TestToggleRechercheActive:
    def test_depuis_la_liste_la_bascule_est_refletee_apres_reload(self, admin_client, storage):
        """Critère d'acceptation : le toggle reflète l'état RÉEL is_active —
        le fragment reconstruit la liste depuis get_all_searches, pas depuis
        un état optimiste. Les lignes portent `username` (jointure du repo)."""
        active = make_search_row(id=5, label="Bordeaux centre", is_active=True, username="alice")
        storage.searches.get_search.side_effect = lambda sid: active if sid == 5 else None
        storage.searches.toggle_search_active.return_value = False
        storage.searches.get_all_searches.return_value = [
            make_search_row(id=5, label="Bordeaux centre", is_active=False, username="alice"),
        ]

        reponse = admin_client.post(
            "/admin/searches/5/toggle-active", headers={"HX-Request": "true"}
        )

        storage.searches.toggle_search_active.assert_called_once_with(5)
        toast = json.loads(reponse.headers["HX-Trigger"])["admin:toast"]
        assert toast["message"] == "Recherche mise en pause"

        corps = reponse.data.decode()
        assert "badge-inactive" in corps
        assert "En pause" in corps
        appel = storage.admin.log_admin_action.call_args
        assert appel.args[0] == "search_active_toggled"
        assert "paused" in appel.args[1]
        assert "Bordeaux centre" in appel.args[1]

    def test_depuis_le_detail_on_retombe_sur_le_detail(self, admin_client, storage):
        storage.searches.get_search.side_effect = lambda sid: (
            make_search_row(id=5, is_active=False) if sid == 5 else None
        )
        storage.searches.toggle_search_active.return_value = True
        storage.searches.get_search_detail.return_value = {
            **make_search_row(id=5, is_active=True),
            "username": "alice",
            "total_listings": 0,
            "recent_listings": [],
        }

        reponse = admin_client.post(
            "/admin/searches/5/toggle-active",
            data={"origine": "detail"},
            headers={"HX-Request": "true"},
        )

        corps = reponse.data.decode()
        assert "Détails de la recherche" in corps
        assert "badge-active" in corps, "le détail reflète le NOUVEL état actif"
        assert "Mettre en pause" in corps, "le bouton propose maintenant la contre-opération"

    def test_une_recherche_inconnue_redirige_sans_bascule(self, admin_client, storage):
        storage.searches.get_search.return_value = None

        reponse = admin_client.post("/admin/searches/999/toggle-active")

        assert reponse.status_code in (302, 303)
        assert "/admin/searches" in reponse.headers["Location"]
        storage.searches.toggle_search_active.assert_not_called()

    def test_une_disparition_entre_lecture_et_bascule_est_geree(self, admin_client, storage):
        storage.searches.get_search.return_value = make_search_row(id=5)
        storage.searches.toggle_search_active.return_value = None

        reponse = admin_client.post("/admin/searches/5/toggle-active")

        assert reponse.status_code in (302, 303)
        storage.admin.log_admin_action.assert_not_called()

    def test_le_listing_propose_le_toggle_pour_chaque_recherche(self, admin_client, storage):
        storage.searches.get_all_searches.return_value = [
            make_search_row(id=5, is_active=True, username="alice"),
            make_search_row(id=6, is_active=False, username="bob"),
        ]

        page = admin_client.get("/admin/searches").data.decode()

        assert "/admin/searches/5/toggle-active" in page
        assert "/admin/searches/6/toggle-active" in page
        assert "badge-active" in page and "badge-inactive" in page

    def test_la_requete_mutante_sans_jeton_est_refusee(self, admin_client_csrf):
        reponse = admin_client_csrf.post("/admin/searches/5/toggle-active")

        assert reponse.status_code == 400


# ---------------------------------------------------------------------------
# 6. Non-régression du dashboard enrichi
# ---------------------------------------------------------------------------


class TestDashboardEnrichi:
    def test_le_dashboard_conserve_stats_graphiques_et_file(self, admin_client, storage):
        """Le dashboard est devenu la page de pilotage : ses blocs historiques
        (#16) doivent cohabiter avec les nouveaux (#17)."""
        page = admin_client.get("/admin").data.decode()

        assert 'id="admin-stats-zone"' in page
        assert "data-chart-cible=\"#chart-activite-7j\"" in page
        assert "Pilotage du scraping" in page
        assert "File d'attente des scrapes" in page

    def test_le_fragment_stats_restait_minimal(self, admin_client):
        """Le polling stats (#16) ne doit pas se retrouver alourdi par le
        contexte file : il rend toujours le partial seul."""
        corps = admin_client.get(
            "/admin?tab=dashboard&fragment=stats", headers={"HX-Request": "true"}
        ).data.decode()

        assert "<html" not in corps
        assert "admin-queue-zone" not in corps
