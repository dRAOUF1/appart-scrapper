"""Socle UX HTMX du panel admin (issue #16).

Ce que ce module garantit, critère par critère de l'issue :

1. **vendors locaux** — htmx et Chart.js sont servis depuis /static/vendor/
   (pas de CDN, pas de build step), et le layout admin les charge ;
2. **markup du socle** — jeton CSRF exposé, toasts flottants, modale générique,
   et plus AUCUN `onsubmit="return confirm(...)"` résiduel ;
3. **navigation fragmentaire** — une requête `HX-Request` reçoit le fragment de
   l'onglet (+ nav hors bande), jamais une page entière ;
4. **polling stats** — la zone vivante se rafraîchit par GET fragmentaire ;
5. **action POST typique** — suppression d'une annonce sans rechargement,
   toast porté par HX-Trigger, et toujours fonctionnelle en POST classique ;
6. **CSRF** — aucune requête HTMX mutante sans jeton : ni champ, ni en-tête
   ⇒ refus 400. L'en-tête X-CSRFToken (posé globalement par admin.js) suffit.

Les données de vue sont doublées comme dans test_admin.py : ces tests portent
sur le RENDU et les en-têtes, pas sur les repositories.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.functional.conftest import (
    make_active_connection,
    make_admin_stats,
    make_db_stats,
    make_table_details,
)
from tests.helpers.factories import make_user_row

# URLs des six onglets, rendus complets (layout + partial) pour les tests de contenu.
URLS_ONGLETS = [
    pytest.param("/admin", id="dashboard"),
    pytest.param("/admin/users", id="users"),
    pytest.param("/admin/searches", id="searches"),
    pytest.param("/admin/listings", id="listings"),
    pytest.param("/admin/database", id="database"),
    pytest.param("/admin/logs", id="logs"),
]


@pytest.fixture(autouse=True)
def admin_views(storage):
    """Données de vue plausibles pour tous les onglets (cf. test_admin.py),
    avec des db_stats COMPLÈTES (db_size, indexes) que lit le partial database."""
    storage.admin.get_enhanced_admin_stats.return_value = make_admin_stats()
    storage.admin.get_db_stats.return_value = make_db_stats()
    storage.admin.get_active_connections.return_value = [make_active_connection()]
    storage.admin.get_table_details.return_value = make_table_details()
    storage.admin.get_admin_logs.return_value = []
    storage.admin.count_admin_logs.return_value = 0
    storage.users.get_all_users.return_value = []
    storage.users.get_user_detail.return_value = None
    storage.searches.get_all_searches.return_value = []
    storage.searches.get_search_detail.return_value = None
    storage.searches.get_search.return_value = None
    storage.listings.get_all_listings.return_value = []
    storage.listings.count_all_listings.return_value = 0
    storage.listings.get_orphan_listings_count.return_value = 0
    return storage


@pytest.fixture
def admin_client_csrf(app, admin_user):
    """Client connecté en admin sur l'app avec protection CSRF ACTIVE.

    Contrairement à `admin_client` (basé sur `app_without_csrf`) : indispensable
    pour éprouver la garde elle-même — cf. TestGardeCsrf.
    """
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_user["id"]
        sess["username"] = admin_user["username"]
    return client


def extraire_jeton_csrf(client) -> str:
    """Récupère le jeton exposé par le layout admin, comme ferait admin.js."""
    page = client.get("/admin").data.decode()
    correspondance = re.search(r'name="csrf-token"\s+content="([^"]+)"', page)
    assert correspondance, "le layout admin doit exposer <meta name=\"csrf-token\">"
    return correspondance.group(1)


# ---------------------------------------------------------------------------
# 1. Vendors statiques locaux
# ---------------------------------------------------------------------------

class TestVendorsLocaux:
    def test_htmx_et_chart_sont_servis_depuis_le_projet(self, client):
        """Pas de CDN : les deux bibliothèques sont des fichiers statiques du
        dépôt, servis en 200 et non vides. La réponse est fermée explicitement :
        un fichier streamé laissé au GC lèverait un ResourceWarning (= erreur)."""
        for nom in ("htmx.min.js", "chart.umd.min.js"):
            reponse = client.get(f"/static/vendor/{nom}")

            assert reponse.status_code == 200
            assert len(reponse.data) > 1000, f"{nom} semble tronqué ou vide"
            reponse.close()

    def test_le_layout_admin_charge_les_trois_scripts(self, admin_client):
        reponse = admin_client.get("/admin")

        assert reponse.status_code == 200
        corps = reponse.data.decode()
        assert '/static/vendor/htmx.min.js' in corps
        assert '/static/vendor/chart.umd.min.js' in corps
        assert '/static/admin.js' in corps


# ---------------------------------------------------------------------------
# 2. Markup du socle
# ---------------------------------------------------------------------------

class TestMarkupDuSocle:
    @pytest.mark.parametrize("url", URLS_ONGLETS)
    def test_chaque_onglet_expose_le_jeton_csrf(self, admin_client, url):
        """Sans ce meta, admin.js ne peut pas poser X-CSRFToken sur les requêtes
        htmx : sa présence sur CHAQUE onglet est un prérequis de sécurité."""
        page = admin_client.get(url).data.decode()

        assert re.search(r'name="csrf-token"', page)

    @pytest.mark.parametrize("url", URLS_ONGLETS)
    def test_plus_aucun_confirm_natif_residuel(self, admin_client, url):
        """Tous les onsubmit="return confirm(...)" ont été remplacés par
        data-confirm (modale générique pilotée par admin.js)."""
        page = admin_client.get(url).data.decode()

        assert 'onsubmit=' not in page

    def test_le_dashboard_porte_des_confirmations_generiques(self, admin_client):
        page = admin_client.get("/admin").data.decode()

        assert 'data-confirm="' in page

    def test_toasts_flottants_et_modale_sont_dans_le_layout(self, admin_client):
        page = admin_client.get("/admin").data.decode()

        assert 'id="admin-toasts"' in page
        assert 'id="admin-modal-backdrop"' in page
        assert 'data-modal-confirmer' in page

    def test_la_cible_commune_admin_content_est_presente(self, admin_client):
        page = admin_client.get("/admin").data.decode()

        assert 'id="admin-content"' in page

    def test_les_graphiques_embarquent_leurs_donnees_en_json(self, admin_client):
        """Chart.js s'alimente depuis la page (script type=application/json),
        sans endpoint dédié."""
        page = admin_client.get("/admin").data.decode()

        assert 'data-chart-cible="#chart-activite-7j"' in page
        assert 'type="application/json"' in page
        assert 'data-chart-cible="#chart-sources"' in page


# ---------------------------------------------------------------------------
# 3. Navigation fragmentaire
# ---------------------------------------------------------------------------

class TestNavigationFragmentaire:
    @pytest.mark.parametrize(("url", "onglet"), [
        ("/admin", "dashboard"),
        ("/admin/users", "users"),
        ("/admin/searches", "searches"),
        ("/admin/listings", "listings"),
        ("/admin/database", "database"),
        ("/admin/logs", "logs"),
    ])
    def test_une_requete_htmx_recoit_un_fragment_pas_une_page(self, admin_client, url, onglet):
        """hx-get des onglets échange #admin-content : la réponse doit donc être
        le partial seul (+ nav hors bande), jamais un document entier."""
        reponse = admin_client.get(url, headers={"HX-Request": "true"})

        assert reponse.status_code == 200
        corps = reponse.data.decode()
        assert "<html" not in corps
        assert 'id="admin-content"' in corps
        assert 'hx-swap-oob' in corps, "la barre d'onglets doit suivre la navigation"

    def test_un_navigateur_classic_recoit_la_page_complete(self, admin_client):
        reponse = admin_client.get("/admin/users")

        assert reponse.status_code == 200
        corps = reponse.data.decode()
        assert "<html" in corps
        assert 'id="admin-content"' in corps


# ---------------------------------------------------------------------------
# 4. Polling de la zone stats
# ---------------------------------------------------------------------------

class TestPollingStats:
    def test_le_fragment_stats_se_rafraichit_lui_meme(self, admin_client):
        reponse = admin_client.get(
            "/admin?tab=dashboard&fragment=stats", headers={"HX-Request": "true"}
        )

        assert reponse.status_code == 200
        corps = reponse.data.decode()
        assert "<html" not in corps
        assert 'id="admin-stats-zone"' in corps
        assert 'hx-trigger="every 30s"' in corps, \
            "le pattern de polling doit vivre sur l'élément échangé"

    def test_le_polling_ne_recharge_pas_les_graphiques(self, admin_client):
        """Le fragment minimal exclut les canvas Chart.js : pas de re-init
        (ni double instance) pendant le polling."""
        corps = admin_client.get(
            "/admin?tab=dashboard&fragment=stats", headers={"HX-Request": "true"}
        ).data.decode()

        assert "canvas" not in corps


# ---------------------------------------------------------------------------
# 5. Action POST typique : suppression d'une annonce
# ---------------------------------------------------------------------------

class TestActionSuppressionAnnonce:
    def test_via_htmx_reponse_fragment_avec_toast_sans_redirection(self, admin_client, storage):
        storage.listings.count_all_listings.return_value = 0

        reponse = admin_client.post(
            "/admin/listings/sl_1/delete",
            headers={"HX-Request": "true"},
        )

        # Pas de redirection : le fragment remplace #admin-content directement.
        assert reponse.status_code == 200
        assert "Location" not in reponse.headers

        declencheurs = json.loads(reponse.headers["HX-Trigger"])
        toast = declencheurs["admin:toast"]
        assert toast["message"] == "Annonce supprimée"
        assert toast["category"] == "success"

        corps = reponse.data.decode()
        assert "<html" not in corps
        assert 'id="admin-content"' in corps
        assert "sl_1" not in corps, "la ligne supprimée doit avoir disparu du fragment"

        storage.listings.delete_listing.assert_called_once_with("sl_1")

    def test_sans_js_le_post_classique_fonctionne_encore(self, admin_client, storage):
        """Dégradation gracieuse : sans en-tête HX-Request, flash + redirection
        comme avant le socle HTMX."""
        reponse = admin_client.post("/admin/listings/sl_1/delete")

        assert reponse.status_code in (302, 303)
        assert "/admin/listings" in reponse.headers["Location"]
        storage.listings.delete_listing.assert_called_once_with("sl_1")

    def test_le_flash_classique_est_affiche_en_toast_statique(self, admin_client, storage):
        reponse = admin_client.post("/admin/listings/sl_1/delete", follow_redirects=True)

        assert reponse.status_code == 200
        corps = reponse.data.decode()
        assert 'toast-success' in corps
        assert "Annonce supprimée" in corps


# ---------------------------------------------------------------------------
# 6. Garde CSRF sur les requêtes HTMX
# ---------------------------------------------------------------------------

class TestGardeCsrf:
    def test_une_requete_htmx_mutante_sans_jeton_est_refusee(self, admin_client_csrf):
        """Ni champ csrf_token, ni en-tête X-CSRFToken : CSRFProtect doit refuser,
        même déguisée en requête htmx."""
        reponse = admin_client_csrf.post(
            "/admin/listings/sl_1/delete",
            headers={"HX-Request": "true"},
        )

        assert reponse.status_code == 400

    def test_len_tete_x_csrftoken_posee_par_admin_js_suffit(self, admin_client_csrf):
        """C'est exactement ce que fait admin.js (htmx:configRequest) : le
        formulaire part sans son champ caché, protégé par l'en-tête seul."""
        jeton = extraire_jeton_csrf(admin_client_csrf)

        reponse = admin_client_csrf.post(
            "/admin/listings/sl_1/delete",
            headers={"HX-Request": "true", "X-CSRFToken": jeton},
        )

        assert reponse.status_code == 200
        assert "HX-Trigger" in reponse.headers

    def test_le_champ_cache_du_formulaire_suffit_aussi(self, admin_client_csrf):
        """Ceinture et bretelles : les formulaires gardent leur input hidden,
        htmx transmet ses champs comme une soumission classique."""
        jeton = extraire_jeton_csrf(admin_client_csrf)

        reponse = admin_client_csrf.post(
            "/admin/cleanup",
            data={"csrf_token": jeton},
            headers={"HX-Request": "true"},
        )

        assert reponse.status_code == 200


# ---------------------------------------------------------------------------
# 7. Non-régression de surface : la page complète reste servie aux humains
# ---------------------------------------------------------------------------

class TestRenduCompletDesOnglets:
    @pytest.mark.parametrize("url", URLS_ONGLETS)
    def test_chaque_onglet_rend_une_page_valide_pour_un_navigateur_sans_js(self, admin_client, url):
        reponse = admin_client.get(url)

        assert reponse.status_code == 200
        assert "<html" in reponse.data.decode()

    def test_la_barre_longlets_propose_les_six_destinations(self, admin_client):
        page = admin_client.get("/admin").data.decode()

        for destination in ("tab=dashboard", "/admin/users", "/admin/searches",
                            "/admin/listings", "/admin/database", "/admin/logs"):
            assert destination in page

    def test_les_utilisateurs_affiches_conservent_leurs_colonnes(self, admin_client, storage):
        """Non-régression markup (#12) : la colonne datée reste labellisée,
        jamais une « Date » muette."""
        storage.users.get_all_users.return_value = [
            {**make_user_row(id=1, username="alice"), "search_count": 2, "listing_count": 11}
        ]

        page = admin_client.get("/admin/users").data.decode()

        assert "alice" in page
        assert "<th>Inscrit le</th>" in page
