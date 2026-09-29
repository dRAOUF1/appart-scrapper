"""Tab « Scrapes » de l'admin — historique & diagnostic (issue #19).

Ce que ce module garantit, critère par critère de l'issue :

1. **tab Scrapes** — nav étendue (#16), colonnes françaises, tri chronologique
   décroissant, pagination (page 2, page hors plage) ;
2. **filtres combinables** — statut / source / recherche / dates transmis
   tels quels au repository (une seule passe côté stockage fichier) ;
3. **cartes de synthèse** — taux succès 24 h & 7 j, durée moyenne, top erreurs,
   cohérentes avec ce que le repo renvoie ;
4. **bloc stats par recherche** — dans la fiche détail existante, aux côtés
   des exports/imports zip qui doivent rester accessibles (non-régression) ;
5. **viewer de log brut** — même rendu que la page utilisateur (macro
   partagée), consultable par l'admin pour TOUTES les recherches ;
6. **relance d'un échec** — bouton réservé aux échecs d'une recherche encore
   existante ET active, soumission réelle via submit_scrape (stub du pool),
   audit « scrape_retried », toast avec le rang en file ;
7. **accès** — la muraille admin est vérifiée sur ces URLs par le garde
   ADMIN_URLS de tests/functional/test_admin.py (exhaustivité imposée).

Les patterns HTMX (fragment + HX-Trigger, modale data-confirm, bandeau pause)
sont ceux du socle #16/#17 : ils sont re-testés ici SUR les nouvelles routes.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import Future
from datetime import datetime

import pytest

from tests.functional.conftest import ADMIN_USERNAME, make_admin_stats, make_search_detail
from tests.helpers.factories import make_scrape_log_entry, make_search_row, make_user_row


class FileExecutorFactice:
    """`submit()` enregistre et renvoie un Future JAMAIS résolu (cf.
    test_admin_pilotage) : la relance laisse un job « en cours » dont la vue
    file d'attente peut lire le rang."""

    max_workers = 1

    def __init__(self):
        self.soumissions: list[tuple] = []

    def submit(self, fn, *args):
        self.soumissions.append(args)
        return Future()


@pytest.fixture(autouse=True)
def scrapes_views(storage):
    """Contextes plausibles pour tous les onglets visités par ce module."""
    storage.admin.get_enhanced_admin_stats.return_value = make_admin_stats()
    storage.admin.count_admin_logs.return_value = 0
    storage.listings.count_all_listings.return_value = 0
    storage.listings.get_orphan_listings_count.return_value = 0
    storage.searches.get_all_searches.return_value = []
    storage.searches.get_search.return_value = None
    storage.searches.get_search_detail.return_value = None
    storage.scrape_logs.get_all_scrape_logs.return_value = []
    storage.scrape_logs.count_all_scrape_logs.return_value = 0
    storage.scrape_logs.get_global_scrape_stats.return_value = {}
    storage.scrape_logs.get_scrape_log_raw.return_value = None
    storage.scrape_logs.get_scrape_stats.return_value = {}
    return storage


@pytest.fixture
def file_propre():
    """Isole les globales de pilotage partagées par toutes les apps du process."""
    import core.scrape_control as core_scrape_control
    import main

    core_scrape_control._soumissions.clear()
    main._scrape_futures.clear()
    yield
    core_scrape_control._soumissions.clear()
    main._scrape_futures.clear()


@pytest.fixture
def admin_client_csrf(app, admin_user):
    """Client connecté en admin sur l'app AVEC protection CSRF active."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_user["id"]
        sess["username"] = admin_user["username"]
    return client


def entree_log(**overrides) -> dict:
    """Entrée brute telle que la renvoie le repository global.

    Le label, la source et le caractère relançable sont ajoutés PAR LA ROUTE
    (enrichissement depuis `get_all_searches`) : un test qui veut une ligne
    labellisée/relançable sème donc la recherche correspondante dans
    `storage.searches.get_all_searches`, jamais ici.
    """
    return make_scrape_log_entry(**overrides)


# ---------------------------------------------------------------------------
# 1. Rendu de la tab
# ---------------------------------------------------------------------------


class TestRenduOnglet:
    def test_la_nav_propose_l_onglet_et_le_fragment_est_sans_html(self, admin_client):
        fragment = admin_client.get("/admin/scrapes", headers={"HX-Request": "true"}).data.decode()

        assert "<html" not in fragment
        assert 'id="admin-content"' in fragment
        assert 'id="admin-tabs-nav" hx-swap-oob="true"' in fragment

    def test_la_page_complete_porte_la_colonne_de_l_onglet(self, admin_client):
        page = admin_client.get("/admin/scrapes").data.decode()

        assert "<html" in page
        assert ">Scrapes</a>" in page

    def test_les_colonnes_francaises_sont_presentes(self, admin_client, storage):
        storage.scrape_logs.get_all_scrape_logs.return_value = [entree_log(id=1)]
        storage.scrape_logs.count_all_scrape_logs.return_value = 1

        page = admin_client.get("/admin/scrapes").data.decode()

        for colonne in ("<th>Date</th>", "<th>Recherche</th>", "<th>Source</th>",
                        "<th>Statut</th>", "<th>Durée</th>", "<th>Trouvées</th>", "<th>Nouvelles</th>"):
            assert colonne in page, colonne

    def test_le_tri_chronologique_decroissant_est_reflete(self, admin_client, storage):
        """Le repo trie déjà desc : le template ne doit PAS inverser. On pose
        deux lignes distinctes et on vérifie leur ordre dans le HTML. Les dates
        naïves UTC sont affichées en heure de Paris (+2 h en juillet)."""
        plus_recente = entree_log(id=2, search_id=1, started_at=datetime(2026, 7, 26, 10, 0))
        moins_recente = entree_log(id=1, search_id=2, started_at=datetime(2026, 7, 25, 9, 0))
        storage.scrape_logs.get_all_scrape_logs.return_value = [plus_recente, moins_recente]
        storage.scrape_logs.count_all_scrape_logs.return_value = 2

        page = admin_client.get("/admin/scrapes").data.decode()

        assert page.index("26/07/2026 12:00") < page.index("25/07/2026 11:00")

    def test_chaque_recherche_est_un_lien_vers_son_detail_admin(self, admin_client, storage):
        """Le label affiché vient de la table recherches (enrichissement route),
        pas du stockage fichier des logs."""
        storage.searches.get_all_searches.return_value = [
            make_search_row(id=7, label="Paris T2", source="seloger")
        ]
        storage.scrape_logs.get_all_scrape_logs.return_value = [entree_log(id=5, search_id=7)]
        storage.scrape_logs.count_all_scrape_logs.return_value = 1

        page = admin_client.get("/admin/scrapes").data.decode()

        assert "Paris T2" in page
        assert '/admin/searches/7"' in page

    def test_pagination_page_2_conserve_les_filtres(self, admin_client, storage):
        storage.scrape_logs.get_all_scrape_logs.return_value = [entree_log(id=9)]
        storage.scrape_logs.count_all_scrape_logs.return_value = 25

        page = admin_client.get(
            "/admin/scrapes?page=2&statut=error&source=seloger&recherche=3"
            "&date_from=2026-07-01&date_to=2026-07-26"
        ).data.decode()

        assert "Page 2 / 2" in page
        # La pagination transporte les filtres courants (liens Précédent/Suivant).
        assert "statut=error" in page and "date_from=2026-07-01" in page
        lien_suivant = re.search(r'href="(/admin/scrapes\?page=[^"]+)"', page)
        assert lien_suivant is None or "page=1" in lien_suivant.group(1), (
            "seul le lien Précédent peut exister en dernière page"
        )

    def test_une_page_hors_plage_ne_provoque_pas_d_erreur(self, admin_client, storage):
        storage.scrape_logs.get_all_scrape_logs.return_value = []
        storage.scrape_logs.count_all_scrape_logs.return_value = 4

        reponse = admin_client.get("/admin/scrapes?page=999")

        assert reponse.status_code == 200
        assert "Aucun scrape trouvé" in reponse.data.decode()
        args, kwargs = storage.scrape_logs.get_all_scrape_logs.call_args
        assert kwargs["offset"] == (999 - 1) * 20

    def test_etat_pause_affiche_sur_l_onglet(self, admin_client, storage):
        storage.settings.get_setting.side_effect = lambda cle, defaut="": (
            "true" if cle == "scheduler_paused" else defaut
        )

        page = admin_client.get("/admin/scrapes").data.decode()

        assert "Planification en pause" in page


# ---------------------------------------------------------------------------
# 2. Filtres combinables
# ---------------------------------------------------------------------------


class TestFiltres:
    def test_statut_source_et_dates_sont_transmis_tels_quels(self, admin_client, storage):
        storage.searches.get_all_searches.return_value = [
            make_search_row(id=3, sources=["seloger"]),
        ]

        admin_client.get(
            "/admin/scrapes?statut=error&source=seloger&date_from=2026-07-01&date_to=2026-07-26"
        )

        kwargs = storage.scrape_logs.count_all_scrape_logs.call_args.kwargs
        assert kwargs == {
            "status_filter": "error",
            "search_ids": [3],
            "date_from": "2026-07-01",
            "date_to": "2026-07-26",
        }

    def test_le_filtre_recherche_cible_une_seule_recherche(self, admin_client, storage):
        admin_client.get("/admin/scrapes?recherche=42")

        kwargs = storage.scrape_logs.get_all_scrape_logs.call_args.kwargs
        assert kwargs["search_ids"] == [42]

    def test_une_recherche_non_numerique_ne_plante_pas(self, admin_client, storage):
        reponse = admin_client.get("/admin/scrapes?recherche=nawak")

        assert reponse.status_code == 200
        kwargs = storage.scrape_logs.get_all_scrape_logs.call_args.kwargs
        assert kwargs["search_ids"] == []

    def test_sans_filtre_aucune_restriction_n_est_passee(self, admin_client, storage):
        admin_client.get("/admin/scrapes")

        kwargs = storage.scrape_logs.get_all_scrape_logs.call_args.kwargs
        assert kwargs["status_filter"] == ""
        assert kwargs["search_ids"] is None

    def test_le_select_des_sources_reflete_les_recherches(self, admin_client, storage):
        storage.searches.get_all_searches.return_value = [
            make_search_row(id=1, sources=["seloger", "laforet"]),
            make_search_row(id=2, sources=["bienici"]),
        ]

        page = admin_client.get("/admin/scrapes").data.decode()

        for option in ('value="seloger"', 'value="laforet"', 'value="bienici"'):
            assert option in page
        for label in ("Paris 13e T2-T3",):
            assert label in page

    def test_les_cartes_de_synthese_affichent_les_stats_globales(self, admin_client, storage):
        storage.scrape_logs.get_global_scrape_stats.return_value = {
            "taux_succes_24h": 92.5,
            "nb_scrapes_24h": 40,
            "taux_succes_7j": 88.0,
            "nb_scrapes_7j": 200,
            "duree_moyenne_7j": 31.2,
            "top_erreurs": [{"message": "HTTP 403 anti-bot", "occurrences": 3,
                             "derniere": datetime(2026, 7, 26, 8, 0)}],
        }

        page = admin_client.get("/admin/scrapes").data.decode()

        assert "92.5%" in page and "88.0%" in page
        assert "31.2s" in page
        assert "HTTP 403 anti-bot" in page
        assert "× 3" in page

    def test_des_fenetres_sans_donnees_affichent_un_tiret(self, admin_client, storage):
        """« Pas de donnée » n'est jamais rendu comme « 0 % » (contrat repo)."""
        storage.scrape_logs.get_global_scrape_stats.return_value = {
            "taux_succes_24h": None, "nb_scrapes_24h": 0,
            "taux_succes_7j": None, "nb_scrapes_7j": 0,
            "duree_moyenne_7j": None, "top_erreurs": [],
        }

        page = admin_client.get("/admin/scrapes").data.decode()

        assert "—%" in page
        assert "0%" not in page.split("Taux de succès 24")[1].split("</div>")[0]


# ---------------------------------------------------------------------------
# 3. Viewer du log brut
# ---------------------------------------------------------------------------


class TestViewerLogBrut:
    def test_le_contenu_brut_est_rendu_avec_sa_colorisation(self, admin_client, storage):
        storage.scrape_logs.get_scrape_log_raw.return_value = {
            **make_scrape_log_entry(id=12),
            "raw_logs": "INFO scrape démarré\nWARNING throttle\nERROR timeout\n",
        }
        storage.searches.get_search.return_value = make_search_row(id=1)

        page = admin_client.get("/admin/scrapes/logs/12").data.decode()

        # La macro partagée colorise les niveaux de log (rendu identique à la
        # page utilisateur) : le texte est donc entouré des spans attendus.
        assert '<span class="log-info">INFO</span> scrape démarré' in page
        assert '<span class="log-error">ERROR</span>' in page
        assert '<span class="log-warning">WARNING</span>' in page
        assert "/admin/searches/1" in page, "le détail de la recherche reste joignable"

    def test_le_viewer_consulte_toutes_les_recherches(self, admin_client, storage):
        """L'admin passe par get_scrape_log_raw SANS user_id : il voit les logs
        d'une recherche dont il n'est pas propriétaire (le chemin utilisateur
        garde sa restriction à lui)."""
        storage.scrape_logs.get_scrape_log_raw.return_value = {
            **make_scrape_log_entry(id=12), "raw_logs": "x",
        }

        admin_client.get("/admin/scrapes/logs/12")

        storage.scrape_logs.get_scrape_log_raw.assert_called_once_with(12)

    def test_un_log_inconnu_redirige_vers_la_liste(self, admin_client, storage):
        storage.scrape_logs.get_scrape_log_raw.return_value = None

        reponse = admin_client.get("/admin/scrapes/logs/999")

        assert reponse.status_code in (302, 303)
        assert "/admin/scrapes" in reponse.headers["Location"]

    def test_un_log_sans_contenu_brut_le_dit(self, admin_client, storage):
        storage.scrape_logs.get_scrape_log_raw.return_value = {
            **make_scrape_log_entry(id=12), "raw_logs": "",
        }

        page = admin_client.get("/admin/scrapes/logs/12").data.decode()

        assert "Aucun log brut disponible" in page


# ---------------------------------------------------------------------------
# 4. Relance d'un scrape en échec
# ---------------------------------------------------------------------------


class TestRelance:
    @pytest.fixture
    def client_relance(self, app_without_csrf, storage, file_propre):
        app_without_csrf._scrape_executor = FileExecutorFactice()
        storage.admin.get_enhanced_admin_stats.return_value = make_admin_stats()
        row = make_user_row(id=99, username=ADMIN_USERNAME)
        storage.users.get_user_by_id.side_effect = lambda uid: row if uid == row["id"] else None
        client = app_without_csrf.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = row["id"]
            sess["username"] = row["username"]
        return client, app_without_csrf

    def test_la_relance_soumet_reellement_le_scrape(self, client_relance, storage):
        """Critère d'acceptation clé : le POST déclenche une VRAIE soumission
        (submit_scrape sur le pool factice) pour le compte du propriétaire."""
        client, app = client_relance
        storage.searches.get_search.return_value = make_search_row(id=7, user_id=4)

        reponse = client.post("/admin/scrapes/7/retry", headers={"HX-Request": "true"})

        assert reponse.status_code == 200
        assert app._scrape_executor.soumissions == [(7, 4)]

    def test_le_toast_annonce_le_rang_en_file(self, client_relance, storage):
        client, _app = client_relance
        storage.searches.get_search.return_value = make_search_row(id=7)

        reponse = client.post("/admin/scrapes/7/retry", headers={"HX-Request": "true"})

        declencheurs = json.loads(reponse.headers["HX-Trigger"])
        toast = declencheurs["admin:toast"]
        assert toast["message"] == "Scrape relancé — rang en file : 1"
        assert toast["category"] == "success"

    def test_la_relance_est_tracee_dans_le_journal_audit(self, client_relance, storage):
        client, _app = client_relance
        storage.searches.get_search.return_value = make_search_row(id=7)

        client.post("/admin/scrapes/7/retry")

        appel = storage.admin.log_admin_action.call_args
        assert appel.args[0] == "scrape_retried"
        assert "7" in appel.args[1]

    def test_un_scrape_deja_en_file_n_est_pas_double(self, client_relance, storage):
        """La déduplication submit_scrape fait le travail : warning, pas de
        seconde soumission, pas d'audit trompeur."""
        client, app = client_relance
        storage.searches.get_search.return_value = make_search_row(id=7)
        premier_post_ok = client.post("/admin/scrapes/7/retry")
        assert premier_post_ok.status_code in (200, 302), "POST classique : fragment ou redirection"
        deja_soumises = len(app._scrape_executor.soumissions)
        storage.admin.log_admin_action.reset_mock()

        reponse = client.post("/admin/scrapes/7/retry", headers={"HX-Request": "true"})

        assert len(app._scrape_executor.soumissions) == deja_soumises
        declencheurs = json.loads(reponse.headers["HX-Trigger"])
        assert declencheurs["admin:toast"]["category"] == "warning"
        storage.admin.log_admin_action.assert_not_called()

    def test_une_recherche_supprimee_ne_peut_pas_etre_relancee(self, client_relance, storage):
        client, app = client_relance
        storage.searches.get_search.return_value = None

        client.post("/admin/scrapes/7/retry")

        assert app._scrape_executor.soumissions == []

    def test_une_recherche_en_pause_n_est_pas_relancee(self, client_relance, storage):
        client, app = client_relance
        storage.searches.get_search.return_value = make_search_row(id=7, is_active=False)

        reponse = client.post("/admin/scrapes/7/retry", headers={"HX-Request": "true"})

        assert app._scrape_executor.soumissions == []
        declencheurs = json.loads(reponse.headers["HX-Trigger"])
        assert "en pause" in declencheurs["admin:toast"]["message"]

    def test_les_filtres_et_la_page_survivent_a_la_relance(self, client_relance, storage):
        """Critère « pagination conservée après action » : les champs cachés du
        formulaire sont relus par le contexte (request.values fusionne form)."""
        client, _app = client_relance
        storage.searches.get_search.return_value = make_search_row(id=7)
        storage.scrape_logs.get_all_scrape_logs.return_value = [entree_log(id=9)]
        storage.scrape_logs.count_all_scrape_logs.return_value = 100

        reponse = client.post(
            "/admin/scrapes/7/retry",
            data={"statut": "error", "source": "seloger", "page": "3"},
            headers={"HX-Request": "true"},
        )

        corps = reponse.data.decode()
        kwargs = storage.scrape_logs.get_all_scrape_logs.call_args.kwargs
        assert kwargs["status_filter"] == "error"
        assert kwargs["offset"] == (3 - 1) * 20
        assert "Page 3 / 5" in corps

    def test_le_bouton_est_masque_sur_un_succes(self, admin_client, storage):
        storage.scrape_logs.get_all_scrape_logs.return_value = [
            entree_log(id=1, status="success"),
            entree_log(id=2, status="empty"),
        ]
        storage.scrape_logs.count_all_scrape_logs.return_value = 2

        page = admin_client.get("/admin/scrapes").data.decode()

        assert "/retry" not in page

    def test_le_bouton_est_masque_si_la_recherche_a_disparu_ou_paused(self, admin_client, storage):
        storage.searches.get_all_searches.return_value = []
        storage.scrape_logs.get_all_scrape_logs.return_value = [
            entree_log(id=1, status="error"),                       # enrichissement route : disparue
        ]
        storage.scrape_logs.count_all_scrape_logs.return_value = 1

        page = admin_client.get("/admin/scrapes").data.decode()

        # Le contexte marque peut_relancer=False quand get_all_searches est vide.
        assert "Relancer" not in page

    def test_le_bouton_apparait_sur_un_echec_relancable(self, admin_client, storage):
        recherche = make_search_row(id=7, is_active=True)
        storage.searches.get_all_searches.return_value = [recherche]
        storage.scrape_logs.get_all_scrape_logs.return_value = [
            entree_log(id=1, status="error", error_message="boom", search_id=7)
        ]
        storage.scrape_logs.count_all_scrape_logs.return_value = 1

        page = admin_client.get("/admin/scrapes").data.decode()

        assert "/admin/scrapes/7/retry" in page
        assert "Relancer" in page

    def test_la_requete_mutante_sans_jeton_est_refusee(self, admin_client_csrf):
        reponse = admin_client_csrf.post("/admin/scrapes/7/retry")

        assert reponse.status_code == 400


# ---------------------------------------------------------------------------
# 5. Bloc stats par recherche + non-régression export/import
# ---------------------------------------------------------------------------


class TestBlocStatsRecherche:
    @pytest.fixture
    def fiche(self, admin_client, storage):
        storage.searches.get_search_detail.return_value = make_search_detail(id=1)
        return lambda: admin_client.get("/admin/searches/1").data.decode()

    def test_la_fiche_detail_expose_les_stats_de_scrapes(self, admin_client, storage, fiche):
        storage.scrape_logs.get_scrape_stats.return_value = {
            "total": 13, "success_count": 10, "error_count": 1, "partial_count": 1, "empty_count": 1,
            "avg_listings": 8.5, "avg_new": 1.5, "avg_duration": 31.2,
            "last_scrape": {"status": "success",
                            "started_at": datetime(2026, 7, 26, 9, 0),
                            "error_message": None},
        }

        page = fiche()

        storage.scrape_logs.get_scrape_stats.assert_called_once_with(1)
        assert "Exécutés" in page
        assert "10 succès · 1 vides · 1 partiels · 1 échecs" in page
        assert "31.2s" in page

    def test_un_log_partiel_detaille_le_resultat_de_chaque_source(self, admin_client, storage):
        storage.scrape_logs.get_scrape_log_raw.return_value = {
            **make_scrape_log_entry(id=12, status="partial", error_message="seloger: blocage"),
            "raw_logs": "",
            "details": {
                "per_source": {
                    "seloger": {"error": "blocage anti-bot"},
                    "laforet": {"found": 3},
                },
            },
        }

        page = admin_client.get("/admin/scrapes/logs/12").data.decode()

        assert "Résultat par source" in page
        assert "blocage anti-bot" in page
        assert "3 annonce(s) trouvée(s)" in page

    def test_une_recherche_jamais_scrapee_n_affiche_pas_de_bloc_vide(self, storage, fiche):
        storage.scrape_logs.get_scrape_stats.return_value = {"total": 0}

        page = fiche()

        assert "Exécutés" not in page

    def test_les_exports_imports_zip_restaurent_accessibles(self, storage, fiche):
        """Non-régression explicite (#19) : la nouvelle carte ne chasse ni les
        boutons zip, ni le formulaire d'import de la fiche."""
        page = fiche()

        assert "/admin/searches/1/logs/export" in page
        assert "/admin/searches/1/logs/import" in page
        assert 'name="log_archive"' in page

    def test_les_routes_zip_repondent_encore(self, admin_client, storage):
        """Et pas seulement leur markup : l'export sert bien un fichier."""
        import io
        import zipfile

        storage.searches.get_search.return_value = make_search_row(id=1)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("metadata.jsonl", "")
        storage.scrape_logs.export_scrape_logs.return_value = "/tmp/fake-export.zip"
        from unittest.mock import patch

        with patch("routes.admin.send_file", return_value="zip-servi") as send_file_mock:
            reponse = admin_client.get("/admin/searches/1/logs/export")

        assert reponse.data == b"zip-servi"
        send_file_mock.assert_called_once()


# ---------------------------------------------------------------------------
# 6. Non-régression du socle #16/#17
# ---------------------------------------------------------------------------


class TestNonRegressionSocle:
    def test_le_dashboard_garde_sa_zone_file_et_ses_bulk(self, admin_client, storage):
        storage.searches.get_all_searches.return_value = [make_search_row(id=1)]

        page = admin_client.get("/admin").data.decode()

        assert 'id="admin-queue-zone"' in page
        assert "Toutes les recherches actives" in page
        assert 'hx-trigger="every 30s"' in page, "le polling stats #16 reste branché"

    def test_le_bandea_u_pause_suit_sur_la_nouvelle_tab(self, admin_client, storage):
        """Complément local au garde URLS_ONGLETS de test_admin_pilotage : le
        bandeau voyage aussi en réponse d'action sur la tab scrapes."""
        storage.settings.get_setting.side_effect = lambda cle, defaut="": (
            "true" if cle == "scheduler_paused" else defaut
        )

        fragment = admin_client.get("/admin/scrapes", headers={"HX-Request": "true"}).data.decode()

        assert "pause-banner" in fragment
        assert 'id="admin-pause-zone" hx-swap-oob="true"' in fragment
