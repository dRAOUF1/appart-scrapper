"""Gestion des recherches côté admin (issue #18) : édition, duplication,
debug URLs sources, critères lisibles.

Ce que ce module garantit, critère par critère de l'issue :

1. **édition** — l'admin modifie critères/intervalle/topic/notifications d'une
   recherche D'UN AUTRE utilisateur : persistée re-normalisée en canonique
   (inseeCode intact), propriétaire inchangé (`update_search` appelé avec LE
   user_id DU PROPRIÉTAIRE), audit `search_edited_admin` ;
2. **duplication** — copie « Copie de X » inactive, même propriétaire, mêmes
   critères, audit `search_duplicated`, retour vers la fiche de la copie ;
   le contrat fin de la copie (create_search n'écrit rien dans
   search_listings) est verrouillé par tests/unit/test_search_repo_duplicate.py ;
3. **URLs sources** — construction pure hors-ligne par PARSERS RÉELS sur ≥2
   sources (Century21 fusion #7, PAP bloc-g #9, Laforêt ancres #13),
   isolation d'erreur par source, note URL_NOTE affichée ;
4. **critères lisibles** — résumé français dans la fiche (INSEE visible),
   JSON brut replié en <details> ;
5. **CSRF + gardes** — les nouvelles routes figurent dans ADMIN_URLS
   (mur paramétrique de tests/functional/test_admin.py, exhaustivité
   imposée) ; le jeton reste obligatoire sur leurs POST.

Les patterns HTMX (fragment + HX-Trigger toast) sont ceux du socle #16.
Le flux utilisateur d'édition (routes/web.py) n'est PAS modifié : ses tests
(tests/functional/test_web_forms.py) restent la référence de non-régression.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.functional.conftest import make_admin_stats, make_search_detail
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_search_row,
    make_transit_selection,
)

# ---------------------------------------------------------------------------
# Données de départ partagées
# ---------------------------------------------------------------------------

# Recherche possédée par bob (user_id 8) — PAS par l'admin (#18 : éditer la
# recherche d'un autre utilisateur sans se l'approprier).
RECHERCHE_ETRANGERE_ID = 3
PROPRIETAIRE_ID = 8

PARIS_PAYLOAD = {
    "kind": "city",
    "city": "Paris",
    "postalCode": "75013",
    "inseeCode": "75113",
}


def recherche_possession_etrange(**overrides) -> dict:
    """Une recherche d'un AUTRE utilisateur, telle que `get_search` la rend."""
    return make_search_row(id=RECHERCHE_ETRANGERE_ID, user_id=PROPRIETAIRE_ID, **overrides)


def formulaire_edition(**overrides) -> dict:
    """Un POST d'édition valide : payload canonique caché compris."""
    data = {
        "label": "Paris 13e édité",
        "ntfy_topic": "topic-edite",
        "scrape_interval": "10",
        "notify_enabled_present": "1",
        "notify_enabled": "on",
        "location_city": ["Paris 13e (75013)"],
        "location_payload": [json.dumps(PARIS_PAYLOAD)],
        "sources": ["seloger"],
        "transaction": "rent",
        "property_types": ["apartment"],
        "price_max": "1400",
    }
    data.update(overrides)
    return data


@pytest.fixture(autouse=True)
def recherches_views(storage):
    """Contexte plausible pour l'onglet recherches et sa fiche détail."""
    storage.admin.get_enhanced_admin_stats.return_value = make_admin_stats()
    storage.admin.get_db_stats.return_value = {"tables": [], "database_size": "12 MB"}
    storage.admin.get_active_connections.return_value = []
    storage.admin.get_admin_logs.return_value = []
    storage.admin.count_admin_logs.return_value = 0
    storage.users.get_all_users.return_value = []
    storage.scrape_logs.get_scrape_stats.return_value = {}
    storage.searches.get_all_searches.return_value = []
    storage.searches.get_search.return_value = None
    storage.searches.get_search_detail.return_value = None
    storage.listings.count_all_listings.return_value = 0
    storage.listings.get_orphan_listings_count.return_value = 0
    return storage


@pytest.fixture
def recherche_etrangere(storage):
    """Branche `get_search` ET `get_search_detail` sur la recherche de bob."""
    row = recherche_possession_etrange()
    detail = make_search_detail(
        id=row["id"], user_id=row["user_id"], username="bob",
        total_listings=0, recent_listings=[],
    )
    storage.searches.get_search.side_effect = lambda sid: row if sid == row["id"] else None
    storage.searches.get_search_detail.side_effect = lambda sid: detail if sid == row["id"] else None
    return row


@pytest.fixture
def admin_client_csrf(app, admin_user):
    """Client admin sur l'app avec protection CSRF ACTIVE (pattern #16)."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = admin_user["id"]
        sess["username"] = admin_user["username"]
    return client


def extraire_jeton_csrf(client) -> str:
    page = client.get("/admin").data.decode()
    correspondance = re.search(r'name="csrf-token"\s+content="([^"]+)"', page)
    assert correspondance, "le layout admin doit exposer <meta name=\"csrf-token\">"
    return correspondance.group(1)


# ---------------------------------------------------------------------------
# Édition des critères par l'admin
# ---------------------------------------------------------------------------


class TestEditionAdmin:
    def test_l_admin_edite_la_recherche_d_un_autre_utilisateur_sans_se_l_approprier(
        self, admin_client, storage, recherche_etrangere
    ):
        resp = admin_client.post(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit",
                                 data=formulaire_edition())

        assert resp.status_code in (200, 302, 303)
        args, kwargs = storage.searches.update_search.call_args
        assert args[0] == RECHERCHE_ETRANGERE_ID
        # Le point critique : la mise à jour passe sous L'IDENTITÉ DU
        # PROPRIÉTAIRE, pas celle de la session admin — le repository vérifie
        # cette correspondance, passer g.user ferait échouer (ou pire,
        # transférer) la recherche.
        assert args[1] == PROPRIETAIRE_ID
        assert kwargs["label"] == "Paris 13e édité"
        assert kwargs["ntfy_topic"] == "topic-edite"
        assert kwargs["scrape_interval"] == 10

    def test_les_criteres_persistes_sont_canoniques_avec_insee_code_intact(
        self, admin_client, storage, recherche_etrangere
    ):
        admin_client.post(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit",
                          data=formulaire_edition(price_max="1400"))

        _, kwargs = storage.searches.update_search.call_args
        criteres = kwargs["criteria"]
        assert criteres["transaction"] == "rent"
        assert criteres["priceMax"] == 1400, "les bornes sont converties en entiers"
        assert criteres["propertyTypes"] == ["apartment"]
        assert criteres["locations"] == [
            {"kind": "city", "city": "Paris", "postalCode": "75013", "inseeCode": "75113"}
        ], "l'inseeCode porté par le payload d'autocomplete ne doit jamais être perdu"

    def test_l_ancien_vocabulaire_seloger_est_renormalise_a_l_ecriture(
        self, admin_client, storage, recherche_etrangere
    ):
        """Une recherche stockée dans l'ancien vocabulaire (normalisée à la
        lecture par le repository) doit repartir CANONIQUE même si l'admin ne
        retouche que le label : jamais de terme de source re-persisté."""
        storage.searches.get_search.side_effect = lambda sid: make_search_row(
            id=RECHERCHE_ETRANGERE_ID, user_id=PROPRIETAIRE_ID,
            criteria={
                "distributionTypes": ["Rent"],
                "estateTypes": ["Apartment"],
                "spaceMin": "25",
                "locations": [dict(PARIS_PAYLOAD)],
            },
        ) if sid == RECHERCHE_ETRANGERE_ID else None

        admin_client.post(
            f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit",
            data=formulaire_edition(surface_min="30"),
        )

        _, kwargs = storage.searches.update_search.call_args
        criteres = kwargs["criteria"]
        assert "distributionTypes" not in criteres
        assert "estateTypes" not in criteres
        assert "spaceMin" not in criteres
        assert criteres["transaction"] == "rent"
        assert criteres["surfaceMin"] == 30

    def test_les_notifications_sont_editables_depuis_l_admin(self, admin_client, storage, recherche_etrangere):
        # Décochée : le marqueur est là, la case absente.
        admin_client.post(
            f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit",
            data=formulaire_edition(),  # notify_enabled présent ci-dessus -> on refait sans lui
        )
        _, kwargs = storage.searches.update_search.call_args
        assert kwargs["notify_enabled"] is True

        admin_client.post(
            f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit",
            data={k: v for k, v in formulaire_edition().items() if k != "notify_enabled"},
        )
        _, kwargs = storage.searches.update_search.call_args
        assert kwargs["notify_enabled"] is False, \
            "marqueur présent + case absente ⇒ notifications désactivées"

    def test_une_localisation_non_exploitable_refuse_l_ecriture_avec_un_message_clair(
        self, admin_client, storage, recherche_etrangere
    ):
        resp = admin_client.post(
            f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit",
            data=formulaire_edition(location_payload=[""], location_city=["Ville fantôme"]),
            follow_redirects=True,
        )

        assert resp.status_code == 200
        assert b"non exploitable" in resp.data
        assert "Ville fantôme".encode() in resp.data
        storage.searches.update_search.assert_not_called()

    def test_le_formulaire_en_erreur_est_reaffiche_avec_ce_qui_avait_ete_saisi(
        self, admin_client, storage, recherche_etrangere
    ):
        resp = admin_client.post(
            f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit",
            data=formulaire_edition(label="Mon label corrigé", ntfy_topic=""),
        )

        page = resp.data.decode()
        assert "Mon label corrigé" in page, "ce qui avait été saisi doit être réaffiché"
        storage.searches.update_search.assert_not_called()

    def test_label_ou_topic_manquant_refuse(self, admin_client, storage, recherche_etrangere):
        admin_client.post(
            f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit",
            data=formulaire_edition(label="  "),
        )

        storage.searches.update_search.assert_not_called()

    def test_l_ecriture_est_auditée_au_nom_de_l_admin(
        self, admin_client, storage, recherche_etrangere, admin_user
    ):
        admin_client.post(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit", data=formulaire_edition())

        action, details, performed_by = storage.admin.log_admin_action.call_args[0]
        assert action == "search_edited_admin"
        assert performed_by == admin_user["username"]
        assert str(RECHERCHE_ETRANGERE_ID) in details

    def test_le_formulaire_d_edition_affiche_les_criteres_stockes(
        self, admin_client, recherche_etrangere
    ):
        resp = admin_client.get(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit")

        page = resp.data.decode()
        assert resp.status_code == 200
        assert 'name="notify_enabled_present"' in page, "le marqueur #10 doit être rendu"
        # Le payload caché transporte l'inseeCode stocké tel quel.
        assert "75113" in page
        assert f'action="/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit"' in page
        assert "Propriétaire" in page, \
            "la fiche doit rappeler que la recherche appartient à quelqu'un d'autre"
        assert "window.SOURCE_CAPABILITIES" in page, \
            "l'autocomplete du formulaire a besoin des capacités déclarées par les sources"

    def test_une_recherche_inconnue_redirige_vers_la_liste(self, admin_client, storage):
        resp = admin_client.get("/admin/searches/404/edit")

        assert resp.status_code in (302, 303)
        assert "/admin/searches" in resp.headers["Location"]

    def test_la_fiche_propose_editer_et_dupliquer(self, admin_client, recherche_etrangere):
        resp = admin_client.get(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}")

        page = resp.data.decode()
        assert f'/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit' in page
        assert f'/admin/searches/{RECHERCHE_ETRANGERE_ID}/duplicate' in page


# ---------------------------------------------------------------------------
# Bloc Transports dans l'édition admin (#28)
# ---------------------------------------------------------------------------

TRANSIT_M14 = make_transit_selection()


class TestTransitEditionAdmin:
    """Le bloc Transports de l'édition admin (constats M2/M3 de la revue).

    Avant correctif, le formulaire admin n'embarquait NI le bloc
    [data-transit-block] NI le champ caché transit_payload : un POST d'édition
    écrasait les sélections stockées avec [] — perte silencieuse, et une
    recherche transit-seule devenait invalide au tick (donc jamais scrapée).
    Et faute de filet ValueError, un transit_payload corrompu remontait un 500
    au lieu du fragment d'erreur des routes utilisateur.
    """

    @pytest.fixture
    def recherche_avec_transit(self, storage):
        """La recherche de bob, portant locations + filtres + transit stockés."""
        critères = {
            "locations": [dict(PARIS_PAYLOAD)],
            "transaction": "rent",
            "propertyTypes": ["apartment"],
            "priceMax": 1400,
            "transit": [TRANSIT_M14],
        }
        row = recherche_possession_etrange(criteria=critères)
        detail = make_search_detail(
            id=row["id"], user_id=row["user_id"], username="bob",
            total_listings=0, recent_listings=[], criteria=critères,
        )
        storage.searches.get_search.side_effect = lambda sid: row if sid == row["id"] else None
        storage.searches.get_search_detail.side_effect = lambda sid: detail if sid == row["id"] else None
        return critères

    def test_le_formulaire_admin_rend_le_bloc_transit_hydrate(
        self, admin_client, recherche_avec_transit
    ):
        resp = admin_client.get(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit")

        page = resp.data.decode()
        assert resp.status_code == 200
        assert 'data-transit-block' in page
        assert 'name="transit_payload"' in page, \
            "sans ce champ caché, le POST écraserait les sélections stockées"
        assert TRANSIT_M14["line_id"] in page, \
            "le champ caché doit être hydraté depuis les critères stockés"
        assert TRANSIT_M14["stop_ids"][0] in page

    def test_un_post_qui_ne_change_que_le_label_presolve_toutes_les_cles_canoniques(
        self, admin_client, storage, recherche_avec_transit
    ):
        # Ce que le navigateur poste quand l'admin ne touche qu'au label :
        # les champs cachés repartent tels quels, transit compris.
        data = formulaire_edition(label="Seul le label change")
        data["transit_payload"] = json.dumps([TRANSIT_M14])

        admin_client.post(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit", data=data)

        _, kwargs = storage.searches.update_search.call_args
        assert kwargs["criteria"] == recherche_avec_transit, \
            "les critères canoniques doivent repartir identiques, transit inclus"

    def test_un_payload_transit_corrompu_repond_en_erreur_sans_ecrire(
        self, admin_client, storage, recherche_avec_transit
    ):
        resp = admin_client.post(
            f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/edit",
            data={**formulaire_edition(), "transit_payload": "{corrompu"},
            follow_redirects=True,
        )

        assert resp.status_code == 200, "jamais un 500 : même filet que les routes utilisateur"
        assert "corrompues" in resp.data.decode(), "le message français du parseur doit être affiché"
        storage.searches.update_search.assert_not_called(), "la recherche doit rester INTACTE en base"


# ---------------------------------------------------------------------------
# Duplication
# ---------------------------------------------------------------------------


class TestDuplicationAdmin:
    def test_dupliquer_cree_une_copie_inactive_au_meme_proprietaire(
        self, admin_client, storage, recherche_etrangere, admin_user
    ):
        copie = make_search_row(id=99, user_id=PROPRIETAIRE_ID, label="Copie de Paris 13e T2-T3")
        storage.searches.duplicate_search.return_value = copie

        resp = admin_client.post(
            f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/duplicate",
            data={"csrf_token": "x"},
        )

        assert resp.status_code in (200, 302, 303)
        storage.searches.duplicate_search.assert_called_once_with(
            RECHERCHE_ETRANGERE_ID, "Copie de Paris 13e T2-T3"
        )
        action, details, performed_by = storage.admin.log_admin_action.call_args[0]
        assert action == "search_duplicated"
        assert performed_by == admin_user["username"]

    def test_apres_duplication_on_retombe_sur_la_fiche_de_la_copie(
        self, admin_client, storage, recherche_etrangere
    ):
        copie = make_search_row(id=99, user_id=PROPRIETAIRE_ID, label="Copie de Paris 13e T2-T3")
        storage.searches.duplicate_search.return_value = copie
        storage.searches.get_search_detail.side_effect = lambda sid: (
            make_search_detail(id=sid, username="bob") if sid == 99 else None
        )

        resp = admin_client.post(
            f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/duplicate",
            data={"csrf_token": "x"}, headers={"HX-Request": "true"},
        )

        # En HTMX, HX-Push-Url porte l'URL affichée : elle doit pointer la COPIE.
        assert resp.headers.get("HX-Push-Url", "").endswith("/admin/searches/99")

    def test_si_la_source_disparait_la_duplication_echoue_proprement(
        self, admin_client, storage, recherche_etrangere
    ):
        storage.searches.duplicate_search.return_value = None

        resp = admin_client.post(
            f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/duplicate",
            data={"csrf_token": "x"}, follow_redirects=True,
        )

        assert resp.status_code == 200
        assert b"introuvable" in resp.data.lower() or b"introuvable" in resp.data
        storage.admin.log_admin_action.assert_not_called()

    def test_une_recherche_inconnue_n_est_pas_dupliquee(self, admin_client, storage):
        resp = admin_client.post("/admin/searches/404/duplicate", data={"csrf_token": "x"})

        assert resp.status_code in (302, 303)
        storage.searches.duplicate_search.assert_not_called()

    def test_la_liste_propose_aussi_le_bouton_dupliquer(self, admin_client, storage):
        # La liste admin vient d'une jointure users : la ligne porte `username`.
        storage.searches.get_all_searches.return_value = [
            recherche_possession_etrange(username="bob")
        ]

        resp = admin_client.get("/admin/searches")

        assert f'/admin/searches/{RECHERCHE_ETRANGERE_ID}/duplicate' in resp.data.decode()


# ---------------------------------------------------------------------------
# Debug : URLs natives construites par chaque source
#
# Les parsers sont RÉELS (exigence de l'issue : ≥2 sources, formats
# post-fixes #7/#9/#13 assertés). Les identifiants de lieu passent par les
# surcharges manuelles (century21.slugs, pap.geoIds) ou la formule pure
# INSEE (laforet) : construction 100 % hors-ligne, zéro réseau.
# ---------------------------------------------------------------------------

C21_BASE = "https://www.century21.fr"
PAP_BASE = "https://www.pap.fr"
LAFORET_BASE = "https://www.laforet.com"


def criteres_debug(sources: list[str]) -> dict:
    """Des critères canoniques dont CHAQUE source sait tirer son URL offline.

    Rennes en PREMIER : les surcharges manuelles sont appariées aux
    localisations dans l'ordre (zip strict=False, côté parser) — le geoId
    43618 et le slug cp-75001 doivent donc épouser la bonne ville."""
    return {
        "locations": [
            make_city_location("Rennes", "35000", "35238"),
            make_city_location("Paris", "75001", "75101"),
        ],
        "transaction": "rent",
        "propertyTypes": ["apartment"],
        "priceMax": 900,
        "sourceOverrides": {
            "century21": {"slugs": ["cp-75001"]},
            "pap": {"geoIds": ["43618"]},
        },
    }


class TestAdminSearchUrlsDebug:
    @pytest.fixture
    def recherche_multi_sources(self, storage):
        row = make_search_row(
            id=RECHERCHE_ETRANGERE_ID, user_id=PROPRIETAIRE_ID,
            sources=["century21", "pap"], criteria=criteres_debug(["century21", "pap"]),
        )
        storage.searches.get_search.side_effect = lambda sid: row if sid == row["id"] else None
        return row

    def test_deux_sources_reelles_recoivent_chacune_son_url_native(
        self, admin_client, recherche_multi_sources
    ):
        """Formats post-fixes, byte-for-byte :
        - Century21 (#7) : slug unique non fusionné + UN type sans filtres ⇒
          forme SANS « f » (/annonces/achat-… — ici location-appartement) ;
        - PAP (#9) : bloc g trié, série native avec suffixe de prix."""
        resp = admin_client.get(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/urls")

        page = resp.data.decode()
        assert resp.status_code == 200
        # Century21 : un type + un prix max ⇒ forme « f » avec le trio de
        # filtres atomique (s/st/b), slug manuel non fusionné (#7).
        assert f"{C21_BASE}/annonces/f/location-appartement/cp-75001/s-0-/st-0-/b-0-900/" in page
        # PAP : série fusionnée mono-périmètre, exactement celle des captures (#9).
        assert f"{PAP_BASE}/annonce/locations-appartement-rennes-g43618-jusqu-a-900-euros" in page

    def test_laforet_produit_l_ancre_departement_canonique_issue_13(self, admin_client, storage):
        row = make_search_row(
            id=RECHERCHE_ETRANGERE_ID, user_id=PROPRIETAIRE_ID,
            sources=["laforet"],
            criteria={
                "locations": [make_department_location("33", "Gironde")],
                "transaction": "rent",
                "propertyTypes": ["apartment"],
            },
        )
        storage.searches.get_search.side_effect = lambda sid: row if sid == row["id"] else None

        resp = admin_client.get(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/urls")
        page = resp.data.decode()

        # Dans le HTML rendu, le `&` de la querystring est échappé `&amp;`
        # (c'est exactement ce que le navigateur décode en ouvrant le lien).
        assert (
            f"{LAFORET_BASE}/departement/location-appartement-gironde"
            "?filter%5Btypes%5D%5B%5D=apartment&amp;filter%5Bdepartments%5D%5B%5D=33"
        ) in page
        # La note URL_NOTE (#13 : granularité commune entière) accompagne le lien.
        assert "commune enti" in page

    def test_les_liens_sont_des_externes_nofollow_noopener(self, admin_client, recherche_multi_sources):
        resp = admin_client.get(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/urls")
        page = resp.data.decode()

        assert 'target="_blank" rel="noopener"' in page
        assert page.count("https://www.") >= 2

    def test_une_source_en_echec_n_empeche_pas_les_autres(
        self, admin_client, monkeypatch, recherche_multi_sources
    ):
        """Isolation par source : SeLoger simulé en panne (exception à la
        construction), Century21 reste réel et doit toujours rendre son URL."""

        class ParserEnPanne:
            SOURCE_NAME = "Seloger"
            URL_NOTE = ""

            def build_search_urls(self, criteria):
                raise RuntimeError("résolution de lieu injoignable")

        from parsers.century21 import Century21Parser

        def fake_get_parser(source, storage=None):
            if source == "seloger":
                return ParserEnPanne()
            return Century21Parser()

        monkeypatch.setattr("parsers.get_parser", fake_get_parser)
        row = recherche_multi_sources
        # On restreint aux deux sources concernées.
        row["sources"] = ["seloger", "century21"]

        resp = admin_client.get(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/urls")
        page = resp.data.decode()

        assert resp.status_code == 200
        assert "URL non reconstructible" in page
        assert "résolution de lieu injoignable" in page
        # Century21, source saine : son URL native complète est bien là.
        assert f"{C21_BASE}/annonces/f/location-appartement/cp-75001/s-0-/st-0-/b-0-900/" in page

    def test_une_source_inconnue_est_signalee_sans_500(self, admin_client, storage):
        row = make_search_row(
            id=RECHERCHE_ETRANGERE_ID, user_id=PROPRIETAIRE_ID,
            sources=["source-fantome"], criteria=criteres_debug([]),
        )
        storage.searches.get_search.side_effect = lambda sid: row if sid == row["id"] else None

        resp = admin_client.get(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}/urls")

        assert resp.status_code == 200
        # Le registre liste les sources valides dans son message d'erreur :
        # la source inconnue est nommée, le reste du fragment reste rendu.
        assert "source-fantome" in resp.data.decode()

    def test_le_detail_charge_les_urls_a_la_demande(self, admin_client, recherche_etrangere):
        """Pas d'appel géo au chargement de la fiche : le fragment est demandé
        explicitement via hx-get vers la zone dédiée."""
        resp = admin_client.get(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}")
        page = resp.data.decode()

        assert 'hx-get="' in page and '/urls"' in page
        assert 'id="search-urls-zone"' in page

    def test_une_recherche_inconnue_rend_un_fragment_d_erreur(self, admin_client, storage):
        resp = admin_client.get("/admin/searches/404/urls")

        assert resp.status_code == 200
        assert "Recherche introuvable" in resp.data.decode()


# ---------------------------------------------------------------------------
# Critères lisibles
# ---------------------------------------------------------------------------


class TestCriteresLisibles:
    def test_la_fiche_affiche_un_resume_francais_structure(self, admin_client, recherche_etrangere):
        resp = admin_client.get(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}")
        page = resp.data.decode()

        for attendu in ("Localisation", "Paris (75013) · INSEE 75113", "Transaction",
                        "Location", "Appartement", "Prix"):
            assert attendu in page, f"le résumé lisible doit porter « {attendu} »"

    def test_le_json_brut_rester_disponible_mais_replie(self, admin_client, recherche_etrangere):
        """Le résumé français remplace le JSON brut comme affichage PRINCIPAL ;
         celui-ci survit replié en <details> pour le debug avancé."""
        resp = admin_client.get(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}")
        page = resp.data.decode()

        position_resume = page.index("INSEE 75113")
        position_details = page.index("JSON brut")
        position_json = page.index('"priceMax"')
        assert position_resume < position_details < position_json, \
            "le JSON brut doit venir APRÈS le résumé, dans le bloc <details>"

    def test_des_criteres_vides_ne_cassent_pas_la_fiche(self, admin_client, storage):
        row = make_search_row(id=RECHERCHE_ETRANGERE_ID, user_id=PROPRIETAIRE_ID, criteria={})
        detail = make_search_detail(id=row["id"], username="bob", total_listings=0, recent_listings=[])
        detail["criteria"] = {}
        storage.searches.get_search.side_effect = lambda sid: row if sid == row["id"] else None
        storage.searches.get_search_detail.side_effect = lambda sid: detail if sid == row["id"] else None

        resp = admin_client.get(f"/admin/searches/{RECHERCHE_ETRANGERE_ID}")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# CSRF sur les nouvelles routes (les gardes de privilège sont déjà couvertes
# paramétriquement par ADMIN_URLS dans test_admin.py, exhaustivité imposée)
# ---------------------------------------------------------------------------


class TestGardeCsrfNouvellesRoutes:
    @pytest.mark.parametrize(
        ("method", "url"),
        [
            pytest.param("POST", "/admin/searches/3/edit", id="edition"),
            pytest.param("POST", "/admin/searches/3/duplicate", id="duplication"),
        ],
    )
    def test_un_post_sans_jeton_est_refuse(self, admin_client_csrf, method, url):
        resp = admin_client_csrf.open(url, method=method, data={"label": "x"})

        assert resp.status_code == 400

    @pytest.mark.parametrize(
        ("method", "url"),
        [
            pytest.param("POST", "/admin/searches/3/edit", id="edition"),
            pytest.param("POST", "/admin/searches/3/duplicate", id="duplication"),
        ],
    )
    def test_len_tete_x_csrftoken_suffit_sur_les_nouvelles_routes(
        self, admin_client_csrf, storage, method, url
    ):
        jeton = extraire_jeton_csrf(admin_client_csrf)
        storage.searches.get_search.return_value = recherche_possession_etrange()
        storage.searches.duplicate_search.return_value = make_search_row(id=99)

        resp = admin_client_csrf.open(url, method=method, data={"label": "x"},
                                      headers={"X-CSRFToken": jeton})

        assert resp.status_code in (200, 302, 303)
