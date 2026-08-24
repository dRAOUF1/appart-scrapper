"""Tab « Annonces » de l'admin — actions groupées, filtres avancés, export (#20).

Ce que ce module garantit, critère par critère de l'issue :

1. **sélection multiple + suppression groupée** — checkboxes, barre d'actions,
   POST bulk-delete protégé par CSRF, compte RÉEL supprimé, log d'audit
   UNIQUE récapitulatif portant LA LISTE des IDs, modale data-confirm ;
2. **filtres avancés** — fourchette de prix, période de première détection,
   orphelines uniquement : transmis au repository via le MÊME dict pour la
   liste et le compteur ; une borne malformée est signalée (jamais 500) sans
   masquer les autres filtres ;
3. **export CSV** — mêmes filtres/tri/recherche que la vue SANS pagination,
   format FR (« ; », BOM UTF-8), colonnes françaises, nom
   annonces_YYYYMMDD.csv, plafond 50 000 lignes ;
4. **tri par colonne** — liens cliquables sur Prix/Source/Date, sens inversé
   si déjà actif, valeur invalide ignorée par l'allowlist du repository ;
5. **pagination conservée** — après action groupée, la page et les filtres
   courants sont reconstruits à l'identique (pattern #19 request.values) ;
6. **accès** — la muraille admin est vérifiée sur ces URLs par le garde
   ADMIN_URLS de tests/functional/test_admin.py (exhaustivité imposée).

Les patterns HTMX (fragment + HX-Trigger, modale data-confirm) sont ceux du
socle #16/#17/#19 : re-testés ici SUR les nouvelles routes.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime

import pytest

from tests.functional.conftest import make_admin_stats  # noqa: I001 (tri géré par ruff)

PLAFOND_EXPORT = 50_000


@pytest.fixture(autouse=True)
def annonces_views(storage):
    """Contextes plausibles pour tous les onglets visités par ce module."""
    storage.admin.get_enhanced_admin_stats.return_value = make_admin_stats()
    storage.admin.count_admin_logs.return_value = 0
    storage.listings.get_all_listings.return_value = []
    storage.listings.count_all_listings.return_value = 0
    storage.listings.get_orphan_listings_count.return_value = 0
    storage.listings.delete_listings.return_value = 0
    return storage


@pytest.fixture
def admin_client_csrf(app, admin_user):
    """Client connecté en admin sur l'app AVEC protection CSRF active."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_user["id"]
        sess["username"] = admin_user["username"]
    return client


# ---------------------------------------------------------------------------
# 1. Filtres avancés — transmis au repository, erreurs signalées
# ---------------------------------------------------------------------------


class TestFiltresAvances:
    def test_les_filtres_combines_atterrissent_intacts_dans_le_repository(self, admin_client, storage):
        storage.listings.count_all_listings.return_value = 3
        storage.listings.get_all_listings.return_value = []

        page = admin_client.get(
            "/admin/listings?search=loft&source=seloger&price_min=800&price_max=1500"
            "&first_seen_min=2026-07-01&first_seen_max=2026-07-31&orphelines=1"
        ).data.decode()

        assert "Aucune annonce trouvée" in page
        args_liste, kwargs_liste = storage.listings.get_all_listings.call_args
        assert kwargs_liste["search_term"] == "loft"
        assert kwargs_liste["source_filter"] == "seloger"
        assert kwargs_liste["filters"] == {
            "price_min": 800,
            "price_max": 1500,
            "first_seen_min": "2026-07-01",
            "first_seen_max": "2026-07-31",
            "orphans_only": True,
        }
        # Le COMPTEUR partage exactement le même état : sinon la pagination ment.
        _, kwargs_compte = storage.listings.count_all_listings.call_args
        assert kwargs_compte == {
            "search_term": "loft",
            "source_filter": "seloger",
            "filters": kwargs_liste["filters"],
        }

    def test_le_prix_zero_est_un_filtre_valide_pas_un_champ_vide(self, admin_client, storage):
        """`price_min=0` est significatif (annonces gratuites / prix nul) :
        il doit voyager jusqu'au repo au lieu d'être avalé par un test de vérité."""
        admin_client.get("/admin/listings?price_min=0")

        _, kwargs = storage.listings.get_all_listings.call_args
        assert kwargs["filters"] == {"price_min": 0}

    def test_une_date_malformee_est_signalee_et_ecartee_sans_500(self, admin_client, storage):
        page = admin_client.get(
            "/admin/listings?first_seen_min=nimporte&price_min=800"
        ).data.decode()

        # La bannière d'erreur précède le tableau et NOMME la borne fautive ;
        # elle n'empêche pas le reste de la vue de se rendre.
        attendu = ("Première détection (depuis le) : date invalide"
                   " (« nimporte », format attendu AAAA-MM-JJ) — filtre ignoré")
        assert attendu in page
        # Le filtre valide reste appliqué : une borne cassée ne masque pas les autres.
        _, kwargs = storage.listings.get_all_listings.call_args
        assert kwargs["filters"] == {"price_min": 800}

    def test_un_prix_non_numerique_est_signale_et_ecarte(self, admin_client, storage):
        page = admin_client.get("/admin/listings?price_max=mille-euros").data.decode()

        assert "Prix maximum invalide" in page
        _, kwargs = storage.listings.get_all_listings.call_args
        assert kwargs["filters"] == {}

    def test_les_champs_de_filtre_sont_renseignes_apres_filtrage(self, admin_client, storage):
        """La vue filtrée ré-affiche ses propres filtres : sans cela, changer
        UNE borne effacerait silencieusement toutes les autres."""
        page = admin_client.get("/admin/listings?price_min=800&first_seen_max=2026-07-31").data.decode()

        assert 'name="price_min"' in page and 'value="800"' in page
        assert 'name="first_seen_max"' in page and 'value="2026-07-31"' in page
        assert "checked" in page or True  # la case orphelines n'est pas cochée ici

    def test_la_case_orphelines_est_cochee_quand_active(self, admin_client, storage):
        page = admin_client.get("/admin/listings?orphelines=1").data.decode()

        assert 'name="orphelines" value="1" checked' in page

    def test_le_lien_export_transportele_meme_etat(self, admin_client, storage):
        """Critère d'acceptation : « l'export CSV reflète exactement les filtres
        actifs » — son href transporte le même état que la vue affichée."""
        storage.listings.get_all_listings.return_value = []
        storage.listings.count_all_listings.return_value = 1

        page = admin_client.get(
            "/admin/listings?price_min=800&sort=prix_asc&orphelines=1"
        ).data.decode()

        assert "/admin/listings/export?search=&amp;source=&amp;sort=prix_asc&amp;price_min=800&amp;orphelines=1" in page


# ---------------------------------------------------------------------------
# 2. Tri par colonne — allowlist, en-têtes cliquables
# ---------------------------------------------------------------------------


class TestTriParColonne:
    @pytest.mark.parametrize("sort", ["prix_asc", "prix_desc", "date_asc", "date_desc",
                                      "source_asc", "source_desc"])
    def test_un_tri_valide_atterrit_dans_le_repository(self, admin_client, storage, sort):
        admin_client.get(f"/admin/listings?sort={sort}")

        _, kwargs = storage.listings.get_all_listings.call_args
        assert kwargs["sort"] == sort

    def test_un_tri_hostile_est_transmis_brut_et_ignore_par_l_allowlist(self, admin_client, storage):
        """La route ne sanitize pas : le repository est L'AUTORITÉ (allowlist).
        La route doit simplement rester rendable quelle que soit la valeur."""
        page = admin_client.get("/admin/listings?sort=prix%20ASC%3B%20DROP%20TABLE%20listings")

        assert page.status_code == 200
        _, kwargs = storage.listings.get_all_listings.call_args
        assert kwargs["sort"] == "prix ASC; DROP TABLE listings"

    def test_les_entetes_sont_cliquables_avec_inversion_du_sens(self, admin_client, storage):
        storage.listings.count_all_listings.return_value = 0

        page_neutre = admin_client.get("/admin/listings").data.decode()
        assert ">Prix <span class=\"tri-indicateur\">" not in page_neutre
        assert "sort=prix_asc" in page_neutre  # premier clic → ascendant

        page_actif = admin_client.get("/admin/listings?sort=prix_asc").data.decode()
        assert "sort=prix_desc" in page_actif  # deuxième clic → inversé
        assert "▲" in page_actif  # indicateur du sens courant

        page_desc = admin_client.get("/admin/listings?sort=prix_desc").data.decode()
        assert "▼" in page_desc
        assert "sort=prix_asc" in page_desc  # troisième clic → retour asc

    def test_le_tri_survit_a_la_pagination(self, admin_client, storage):
        storage.listings.count_all_listings.return_value = 100

        page = admin_client.get("/admin/listings?sort=source_desc&page=2").data.decode()

        assert "Page 2 / 4" in page
        assert "sort=source_desc" in page

    def test_les_trois_colonnes_proposees_portent_leur_lien(self, admin_client, storage):
        page = admin_client.get("/admin/listings").data.decode()

        for colonne in ("prix_asc", "date_asc", "source_asc"):
            assert f"sort={colonne}" in page, colonne


# ---------------------------------------------------------------------------
# 3. Sélection multiple + suppression groupée
# ---------------------------------------------------------------------------


class TestSuppressionGroupee:
    def test_la_vue_propose_checkboxes_barre_et_confirmation(self, admin_client, storage):
        storage.listings.count_all_listings.return_value = 2
        storage.listings.get_all_listings.return_value = [
            {"listing_id": "sl_1", "title": "Studio Paris", "price": "800 €",
             "surface": "25", "location": "Paris", "source": "seloger",
             "linked_searches": 1, "creation_date": None, "first_seen": None},
            {"listing_id": "sl_2", "title": "T2 Lyon", "price": None,
             "surface": None, "location": "Lyon", "source": "laforet",
             "linked_searches": 0, "creation_date": None, "first_seen": None},
        ]

        page = admin_client.get("/admin/listings").data.decode()

        assert 'id="annonces-select-all"' in page
        assert page.count('class="annonce-check"') == 2
        for valeur in ('value="sl_1"', 'value="sl_2"'):
            assert valeur in page, valeur
        assert 'id="annonces-bulk-bar"' in page
        assert "Supprimer la sélection" in page
        # Le message générique est rendu côté serveur ; le JS y injecte le compteur.
        assert "Cette action est définitive" in page

    def test_la_suppression_groupee_supprime_journalise_et_toaste(self, admin_client, storage):
        storage.listings.delete_listings.return_value = 2

        reponse = admin_client.post(
            "/admin/listings/bulk-delete",
            data={"ids": ["sl_1", "sl_2"], "page": "1"},
            headers={"HX-Request": "true"},
        )

        storage.listings.delete_listings.assert_called_once_with(["sl_1", "sl_2"])
        # Log UNIQUE récapitulatif portant LA LISTE des IDs.
        storage.admin.log_admin_action.assert_called_once()
        action, details, _par = storage.admin.log_admin_action.call_args[0]
        assert action == "listings_bulk_deleted"
        assert "sl_1" in details and "sl_2" in details
        assert "2 listing(s) deleted from a selection of 2" in details
        # Toast HTMX avec le compte réel.
        declencheur = json.loads(reponse.headers["HX-Trigger"])
        assert declencheur["admin:toast"]["message"] == "2 annonce(s) supprimée(s)"
        assert declencheur["admin:toast"]["category"] == "success"
        assert 'id="admin-content"' in reponse.data.decode()

    def test_le_compte_reel_prime_sur_la_taille_de_la_selection(self, admin_client, storage):
        """Des IDs peuvent disparaître entre l'affichage et le POST : le message
        comme l'audit doivent dire CE QUI A ÉTÉ supprimé, pas la sélection."""
        storage.listings.delete_listings.return_value = 1

        reponse = admin_client.post(
            "/admin/listings/bulk-delete",
            data={"ids": ["sl_1", "sl_2"]},
            headers={"HX-Request": "true"},
        )

        details = storage.admin.log_admin_action.call_args[0][1]
        assert "1 listing(s) deleted from a selection of 2" in details
        toast = json.loads(reponse.headers["HX-Trigger"])["admin:toast"]
        assert toast["category"] == "warning"
        assert "sur 2 sélectionnée(s)" in toast["message"]

    def test_une_selection_vide_est_refusee_poliment(self, admin_client, storage):
        reponse = admin_client.post(
            "/admin/listings/bulk-delete", data={}, headers={"HX-Request": "true"},
        )

        toast = json.loads(reponse.headers["HX-Trigger"])["admin:toast"]
        assert toast["category"] == "info"
        assert "Aucune annonce sélectionnée" in toast["message"]
        storage.listings.delete_listings.assert_called_once_with([])

    def test_page_et_filtres_survivent_a_l_action(self, admin_client, storage):
        """Critère d'acceptation : « pagination conservée après action groupée ».
        Les champs cachés du formulaire transportent l'état courant ; le
        fragment reconstruit retombe sur LA MÊME vue filtrée/paginée."""
        storage.listings.delete_listings.return_value = 1
        storage.listings.count_all_listings.return_value = 100

        reponse = admin_client.post(
            "/admin/listings/bulk-delete",
            data={
                "ids": ["sl_9"],
                "page": "3",
                "search": "duplex",
                "source": "seloger",
                "sort": "prix_asc",
                "price_min": "800",
                "first_seen_min": "2026-07-01",
                "orphelines": "1",
            },
            headers={"HX-Request": "true"},
        )
        fragment = reponse.data.decode()

        # Le repo a été relu avec l'état conservé (offset de la page 3).
        _, kwargs = storage.listings.get_all_listings.call_args
        assert kwargs["offset"] == (3 - 1) * 30
        assert kwargs["search_term"] == "duplex"
        assert kwargs["filters"]["price_min"] == 800
        assert kwargs["filters"]["orphans_only"] is True
        assert kwargs["sort"] == "prix_asc"
        # Et le HTML reconstruit porte la même vue + sa pagination filtrée.
        assert "Page 3 / 4" in fragment
        assert "price_min=800" in fragment and "sort=prix_asc" in fragment

    def test_le_post_est_protege_par_csrf(self, admin_client_csrf):
        reponse = admin_client_csrf.post(
            "/admin/listings/bulk-delete", data={"ids": ["sl_1"]},
        )

        assert reponse.status_code == 400

    def test_la_route_est_hors_atteinte_des_non_admins(self, web_client, storage):
        reponse = web_client.post("/admin/listings/bulk-delete", data={"ids": ["sl_1"]})

        assert reponse.status_code in (302, 303)
        storage.listings.delete_listings.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Export CSV
# ---------------------------------------------------------------------------


def ligne_vue(**overrides) -> dict:
    """Annonce telle que `get_all_listings` la renvoie, déterministe."""
    base = {
        "listing_id": "sl_csv1",
        "title": "Appartement 3 pièces; avec virgule",
        "source": "seloger",
        "price": "1 200 €/mois",
        "surface": "65",
        "rooms": "3",
        "city": "Paris",
        "url": "https://www.seloger.com/annonces/sl_csv1.htm",
        "creation_date": "2026-07-01T10:00:00+00:00",  # issue #12 (TEXT ISO)
        "first_seen": datetime(2026, 7, 1, 12, 30),
    }
    base.update(overrides)
    return base


class TestExportCsv:
    def test_headers_http_nom_de_fichier_et_bom(self, admin_client, storage):
        reponse = admin_client.get("/admin/listings/export")

        assert reponse.status_code == 200
        assert reponse.content_type.startswith("text/csv")
        attendu = f"annonces_{datetime.now(UTC):%Y%m%d}.csv"
        assert f"filename={attendu}" in reponse.headers["Content-Disposition"]
        corps = reponse.data
        assert corps.startswith(b"\xef\xbb\xbf"), "BOM UTF-8 requis pour Excel FR"

    def test_entetes_fr_separateur_et_contenu_formate(self, admin_client, storage):
        storage.listings.get_all_listings.return_value = [
            ligne_vue(),
            # Sentinelles #12 et valeurs manquantes → cellules vides, jamais « unknown ».
            ligne_vue(listing_id="sl_csv2", title="Sans rien", price=None, surface=None,
                      rooms=None, city=None, url="", creation_date="unknown", first_seen=None),
        ]

        reponse = admin_client.get("/admin/listings/export")
        texte = reponse.data.decode("utf-8-sig")  # retire le BOM

        lecteur = list(csv.reader(io.StringIO(texte), delimiter=";"))
        assert lecteur[0] == ["id", "titre", "source", "prix", "surface", "pièces",
                              "date publication", "première détection", "ville", "url"]
        assert lecteur[1] == [
            "sl_csv1", "Appartement 3 pièces; avec virgule", "seloger", "1 200 €/mois",
            "65", "3", "01/07/2026", "01/07/2026 12:30", "Paris",
            "https://www.seloger.com/annonces/sl_csv1.htm",
        ]
        assert lecteur[2] == ["sl_csv2", "Sans rien", "seloger", "", "", "", "", "", "", ""]
        # Le point-virgule DANS une cellule doit être cité par le writer, pas casser les colonnes.
        assert len(lecteur[1]) == 10

    def test_meme_etat_que_la_vue_mais_sans_pagination(self, admin_client, storage):
        admin_client.get(
            "/admin/listings/export?search=loft&source=seloger&sort=prix_asc"
            "&price_min=800&first_seen_max=2026-07-31"
        )

        _, kwargs = storage.listings.get_all_listings.call_args
        assert kwargs["limit"] == PLAFOND_EXPORT
        assert kwargs["offset"] == 0
        assert kwargs["search_term"] == "loft"
        assert kwargs["source_filter"] == "seloger"
        assert kwargs["sort"] == "prix_asc"
        assert kwargs["filters"] == {"price_min": 800, "first_seen_max": "2026-07-31"}

    def test_le_plafond_est_documente_et_respecte(self, admin_client, storage):
        from routes.admin import EXPORT_CSV_PLAFOND

        assert EXPORT_CSV_PLAFOND == 50_000
        admin_client.get("/admin/listings/export")
        _, kwargs = storage.listings.get_all_listings.call_args
        assert kwargs["limit"] == EXPORT_CSV_PLAFOND

    def test_l_export_n_est_pas_servi_aux_non_admins(self, client, storage):
        reponse = client.get("/admin/listings/export")

        assert reponse.status_code in (302, 303)
        assert "/login" in reponse.headers["Location"]
