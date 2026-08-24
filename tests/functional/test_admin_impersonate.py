"""Vue administrateur lecture seule + stats par utilisateur (#22).

Trois contrats y sont éprouvés, par ordre de gravité :

1. LE GUARD SERVEUR : aucune route mutante de web_bp n'est exécutable pendant
   une impersonation. La paramétrisation est générée EN BALAYANT l'url_map du
   blueprint (pas une liste copiée à la main) : toute route mutante future
   apparaîtra automatiquement dans le balayage et fera échouer le test si elle
   n'est pas traitée. Un inventaire explicite complète le balayage pour rendre
   toute évolution de la surface mutante VISIBLE en revue de code.
2. L'ÉTAT DE SESSION : entrée admin-only tracée, sortie restauratrice même si
   l'admin ou la cible ont disparu, auto-impersonation et cascade refusées,
   admin inaccessible SAUF la sortie.
3. L'AFFICHAGE : bannière présente, actions masquées, cartes statistiques
   cohérentes avec ce que renvoient les repos.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from flask import Flask
from werkzeug.routing.converters import NumberConverter

from routes.web import web_bp
from tests.functional.conftest import ADMIN_USERNAME, make_dashboard_data, make_user_detail

# Préfixes d'écriture repris du balayage de test_authorization.py, étendus aux
# repères personnels (#26) : une route bloquée ne doit écrire NULLE part.
_PREFIXES_ECRITURE = ("create", "update", "delete", "toggle", "mark", "save", "set", "reset",
                      "purge", "truncate", "import", "link")


def _ecritures_storage(storage: MagicMock) -> list[str]:
    """Toutes les méthodes d'écriture appelées sur le storage, tous repos."""
    appelees = []
    for repo_name in ("users", "searches", "listings", "scrape_logs",
                      "admin", "settings", "seloger_geo", "map_pins"):
        repo = getattr(storage, repo_name)
        for attr in dir(repo):
            if attr.startswith("_") or not attr.startswith(_PREFIXES_ECRITURE):
                continue
            if getattr(getattr(repo, attr), "called", False):
                appelees.append(f"{repo_name}.{attr}")
    return appelees

# ---------------------------------------------------------------------------
# Balayage dynamique des routes mutantes de web_bp (source des paramètres)
# ---------------------------------------------------------------------------

METHODES_MUTANTES = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Inventaire EXPLICITE des endpoints mutants connus. Ce set sert de garde-fou
# documentaire : une route mutante ajoutée à web_bp fait échouer
# `test_l_inventaire_des_routes_mutantes_est_explicite` et doit être RELUE
# (même si le guard before_request la couvre déjà automatiquement).
ENDPOINTS_MUTANTS_ATTENDUS = {
    "web.cleanup",
    "web.create_pin",
    "web.delete_pin",
    "web.delete_search_web",
    "web.edit_search",
    "web.scrape_search_web",
    "web.search_logs_import",
    "web.searches",
    "web.toggle_search_active_web",
    "web.toggle_search_notify_web",
    "web.update_blacklist_agencies",
    "web.update_blacklist_mode",
    "web.login",
    "web.update_interval_web",
    "web.update_pin",
}


def _regles_mutantes_webbp() -> list[tuple[str, str]]:
    """Toutes les règles mutantes de web_bp, résolues en `(méthode, URL)`.

    Une mini-app suffit : l'enregistrement du blueprint est pure déclaration.
    Les convertisseurs d'arguments reçoivent une valeur neutre (1 pour un int,
    « x » sinon) juste assez valide pour atteindre le guard.
    """
    app = Flask(__name__)
    app.register_blueprint(web_bp)
    adaptateur = app.url_map.bind("")
    regles: list[tuple[str, str]] = []
    for regle in app.url_map.iter_rules():
        if not regle.endpoint.startswith("web.") or regle.endpoint == "web.static":
            continue
        for methode in sorted(regle.methods & METHODES_MUTANTES):
            valeurs = {
                arg: (1 if isinstance(conv, NumberConverter) else "x")
                for arg, conv in getattr(regle, "_converters", {}).items()
            }
            url = adaptateur.build(regle.endpoint, valeurs)
            regles.append((methode, url))
    return sorted(regles)


REGLES_MUTANTES = [
    pytest.param(methode, url, id=f"{methode} {url}")
    for methode, url in _regles_mutantes_webbp()
]


def poser_session_impersonnee(client, *, cible_id=1, cible_username="alice"):
    """Rejoue exactement ce que `/admin/users/<id>/impersonate` écrit."""
    with client.session_transaction() as sess:
        sess["user_id"] = cible_id
        sess["username"] = cible_username
        sess["impersonator_user_id"] = 99
        sess["impersonator_username"] = ADMIN_USERNAME
        sess["impersonated_username"] = cible_username
    return client


@pytest.fixture
def identites(storage):
    """`get_user_by_id` résout alice (1) ET l'admin (99), comme en prod."""
    alice = {"id": 1, "username": "alice", "created_at": datetime(2026, 1, 1)}
    admin = {"id": 99, "username": ADMIN_USERNAME, "created_at": datetime(2026, 1, 1)}
    storage.users.get_user_by_id.side_effect = (
        lambda uid: {1: alice, 99: admin}.get(uid)
    )
    return {"alice": alice, "admin": admin}


@pytest.fixture
def impersonate_client(app_without_csrf, identites):
    """Client connecté SOUS IMPERSONATION : session marquée, CSRF désactivé."""
    client = app_without_csrf.test_client()
    return poser_session_impersonnee(client)


# ---------------------------------------------------------------------------
# 1. Le guard serveur — balayage exhaustif
# ---------------------------------------------------------------------------


class TestGuardServeur:
    @pytest.mark.parametrize(("methode", "url"), REGLES_MUTANTES)
    def test_toute_route_mutante_repond_403_en_impersonation(
        self, app_without_csrf, storage, methode, url
    ):
        """Balayage DYNAMIQUE : chaque règle mutante de web_bp (celles d'aujourd'hui
        ET celles ajoutées demain) doit répondre 403 sous impersonation — et ne
        rien écrire. Un oubli futur est donc structurellement détecté ici."""
        client = poser_session_impersonnee(app_without_csrf.test_client())

        resp = client.open(url, method=methode)

        assert resp.status_code == 403, f"{methode} {url} a répondu {resp.status_code}"
        assert "lecture seule" in resp.get_data(as_text=True).lower()
        assert _ecritures_storage(storage) == [], f"{methode} {url} a écrit malgré le guard"

    @pytest.mark.parametrize(("methode", "url"), REGLES_MUTANTES)
    def test_hors_impersonation_la_meme_route_ne_repond_pas_403(
        self, web_client, storage, methode, url
    ):
        """Contre-épreuve : le guard est conditionnel. Hors impersonation, la
        même requête suit son cours normal (redirection métier, validation de
        payload…) — jamais le 403 lecture seule."""
        resp = web_client.open(url, method=methode)

        assert resp.status_code != 403, f"{methode} {url} bloquée à tort hors impersonation"

    def test_les_get_restent_ouverts_en_impersonation(self, impersonate_client, storage, user):
        """C'est le BUT de la vue administrateur : voir. Les lectures (dashboard,
        liste des recherches) doivent rester rendables pendant l'impersonation."""
        storage.users.get_dashboard_data.return_value = make_dashboard_data()

        assert impersonate_client.get("/dashboard").status_code == 200
        assert impersonate_client.get("/searches").status_code == 200

    def test_l_inventaire_des_routes_mutantes_est_explicite(self):
        """Garde-fou documentaire : si ce test échoue, la surface mutante de
        web_bp a changé. Relisez la nouvelle route (le guard before_request la
        couvre déjà), puis mettez à jour ENDPOINTS_MUTANTS_ATTENDUS."""
        endpoints_trouves = {
            endpoint
            for endpoint, regles in _endpoints_par_regles().items()
            if regles & METHODES_MUTANTES
        }

        assert endpoints_trouves == ENDPOINTS_MUTANTS_ATTENDUS


def _endpoints_par_regles() -> dict[str, frozenset[str]]:
    app = Flask(__name__)
    app.register_blueprint(web_bp)
    return {
        regle.endpoint: regle.methods - {"HEAD", "OPTIONS"}
        for regle in app.url_map.iter_rules()
        if regle.endpoint.startswith("web.") and regle.endpoint != "web.static"
    }


# ---------------------------------------------------------------------------
# 2. Entrée / sortie d'impersonation
# ---------------------------------------------------------------------------


class TestEntreeImpersonation:
    def test_l_admin_bascule_vers_le_dashboard_de_la_cible(
        self, admin_client, storage, identites
    ):
        resp = admin_client.post("/admin/users/1/impersonate")

        assert resp.status_code == 302
        assert resp.headers["Location"] == "/dashboard"
        with admin_client.session_transaction() as sess:
            assert sess["user_id"] == 1
            assert sess["username"] == "alice"
            # La marque explicite d'état : c'est ELLE que tous les guards lisent.
            assert sess["impersonator_user_id"] == 99
            assert sess["impersonated_username"] == "alice"

    def test_l_entree_est_tracee_avec_les_deux_identites(
        self, admin_client, storage, identites
    ):
        admin_client.post("/admin/users/1/impersonate")

        action, details, performed_by = storage.admin.log_admin_action.call_args[0]
        assert action == "impersonation_started"
        # Les DEUX identités figurent dans l'entrée d'audit (#22) : qui regarde,
        # et qui est regardé.
        assert "ID:99" in details
        assert "ID:1" in details
        assert performed_by == ADMIN_USERNAME

    def test_s_impersonner_soi_meme_est_refuse(self, admin_client, storage):
        resp = admin_client.post("/admin/users/99/impersonate")

        assert resp.status_code == 302
        with admin_client.session_transaction() as sess:
            assert "impersonator_user_id" not in sess
        storage.admin.log_admin_action.assert_not_called()

    def test_le_compte_admin_est_refuse_meme_sous_un_autre_id(self, admin_client, storage):
        """Un second compte porterait ADMIN_USERNAME ? Il resterait refusé :
        c'est le verrou anti-cascade côté username."""
        storage.users.get_user_by_id.side_effect = lambda uid: (
            {"id": uid, "username": ADMIN_USERNAME} if uid == 5 else None
        )

        resp = admin_client.post("/admin/users/5/impersonate")

        assert resp.status_code == 302
        with admin_client.session_transaction() as sess:
            assert "impersonator_user_id" not in sess
        storage.admin.log_admin_action.assert_not_called()

    def test_une_cible_inexistante_redirige_vers_la_liste(self, admin_client, storage):
        storage.users.get_user_by_id.return_value = None

        resp = admin_client.post("/admin/users/1234/impersonate")

        assert resp.status_code == 302
        with admin_client.session_transaction() as sess:
            assert "impersonator_user_id" not in sess
        storage.admin.log_admin_action.assert_not_called()

    def test_un_non_admin_ne_peut_pas_lancer_d_impersonation(
        self, web_client, storage, user
    ):
        resp = web_client.post("/admin/users/2/impersonate")

        assert resp.status_code == 302
        with web_client.session_transaction() as sess:
            assert "impersonator_user_id" not in sess
        storage.admin.log_admin_action.assert_not_called()

    def test_un_anonyme_est_envoye_au_login(self, app_without_csrf, storage):
        resp = app_without_csrf.test_client().post("/admin/users/1/impersonate")

        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
        storage.admin.log_admin_action.assert_not_called()

    def test_la_cascade_est_impossible_en_vue_active(
        self, impersonate_client, storage, identites
    ):
        """Déjà en impersonation, repartir vers une AUTRE cible est refusé par
        le blocage de l'admin : impossible d'enchaîner les vues."""
        resp = impersonate_client.post("/admin/users/5/impersonate")

        assert resp.status_code == 403
        with impersonate_client.session_transaction() as sess:
            assert sess["impersonated_username"] == "alice"

    def test_l_entree_sans_jeton_csrf_est_refusee(self, app, admin_user, storage):
        """La protection CSRF globale s'applique à l'entrée comme à tout POST
        admin : sans jeton, 400 avant la vue."""
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = admin_user["id"]
            sess["username"] = admin_user["username"]

        resp = client.post("/admin/users/1/impersonate")

        assert resp.status_code == 400


class TestSortieImpersonation:
    def test_la_sortie_restaure_exactement_la_session_admin(
        self, impersonate_client, storage, identites
    ):
        resp = impersonate_client.post("/admin/impersonate/exit")

        assert resp.status_code == 302
        assert "/admin" in resp.headers["Location"]
        with impersonate_client.session_transaction() as sess:
            assert sess["user_id"] == 99
            assert sess["username"] == ADMIN_USERNAME
            assert "impersonator_user_id" not in sess
            assert "impersonator_username" not in sess
            assert "impersonated_username" not in sess

    def test_la_sortie_est_tracee_au_nom_de_l_admin_restaure(
        self, impersonate_client, storage, identites
    ):
        impersonate_client.post("/admin/impersonate/exit")

        action, details, performed_by = storage.admin.log_admin_action.call_args[0]
        assert action == "impersonation_ended"
        assert "ID:99" in details
        assert performed_by == ADMIN_USERNAME

    def test_la_cible_supprimee_n_empeche_pas_la_sortie(self, app_without_csrf, storage):
        """Seule la ligne admin doit encore exister : la restauration lit les
        clés figées de la session, jamais la ligne cible."""
        admin = {"id": 99, "username": ADMIN_USERNAME}
        storage.users.get_user_by_id.side_effect = lambda uid: admin if uid == 99 else None
        client = poser_session_impersonnee(app_without_csrf.test_client())

        resp = client.post("/admin/impersonate/exit")

        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert sess["user_id"] == 99

    def test_un_admin_supprime_pendant_la_vue_est_renvoye_au_login(
        self, app_without_csrf, storage
    ):
        storage.users.get_user_by_id.return_value = None
        client = poser_session_impersonnee(app_without_csrf.test_client())

        resp = client.post("/admin/impersonate/exit")

        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
        with client.session_transaction() as sess:
            # Aucune identité résiduelle : ni cible, ni admin, ni marque de vue
            # (le flash de session peut subsister, lui).
            for cle in ("user_id", "username", "impersonator_user_id",
                        "impersonator_username", "impersonated_username"):
                assert cle not in sess

    def test_hors_impersonation_la_sortie_ne_change_rien(
        self, web_client, storage, user
    ):
        resp = web_client.post("/admin/impersonate/exit")

        assert resp.status_code == 302
        with web_client.session_transaction() as sess:
            assert sess["user_id"] == user["id"]

    def test_un_anonyme_est_envoye_au_login(self, app_without_csrf, storage):
        resp = app_without_csrf.test_client().post("/admin/impersonate/exit")

        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_la_sortie_sans_jeton_csrf_est_refusee(self, app, storage):
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "alice"
            sess["impersonator_user_id"] = 99

        resp = client.post("/admin/impersonate/exit")

        assert resp.status_code == 400
        with client.session_transaction() as sess:
            assert sess["user_id"] == 1


class TestAdminInaccessibleSaufSortie:
    """Choix documenté (#22) : pendant une vue administrateur, TOUTES les
    routes admin répondent 403 — la seule action disponible est de sortir."""

    @pytest.mark.parametrize(
        ("methode", "url"),
        [
            pytest.param("GET", "/admin", id="GET-dashboard"),
            pytest.param("GET", "/admin?tab=users", id="GET-tab-users"),
            pytest.param("POST", "/admin/users/1/delete", id="POST-delete-user"),
            pytest.param("POST", "/admin/database/truncate", id="POST-truncate"),
            pytest.param("POST", "/admin/scheduler/pause", id="POST-pause-scheduler"),
        ],
    )
    def test_une_route_admin_repond_403_en_vue_active(
        self, impersonate_client, storage, methode, url
    ):
        kwargs = {"data": {"table_name": "users"}} if url.endswith("truncate") else {}
        resp = impersonate_client.open(url, method=methode, **kwargs)

        assert resp.status_code == 403
        assert _ecritures_storage(storage) == []

    def test_hors_vue_active_l_admin_redevient_accessible(self, admin_client, storage, admin_stats):
        assert admin_client.get("/admin").status_code == 200


# ---------------------------------------------------------------------------
# 3. Affichage : bannière, actions masquées, page 403
# ---------------------------------------------------------------------------


class TestBanniereEtRendu:
    def test_le_dashboard_impersonne_porte_la_banniere_et_le_bouton_sortie(
        self, impersonate_client, storage
    ):
        storage.users.get_dashboard_data.return_value = make_dashboard_data()

        page = impersonate_client.get("/dashboard").get_data(as_text=True)

        assert "Vue administrateur" in page
        assert "lecture seule" in page
        assert 'action="/admin/impersonate/exit"' in page
        # La cible est nommée : l'admin sait QUI il consulte.
        assert "alice" in page

    def test_hors_impersonation_il_n_y_a_pas_de_banniere(self, web_client, storage):
        storage.users.get_dashboard_data.return_value = make_dashboard_data()

        page = web_client.get("/dashboard").get_data(as_text=True)

        assert "Vue administrateur" not in page

    def test_le_formulaire_destructeur_est_masque_sur_le_dashboard_impersonne(
        self, impersonate_client, storage
    ):
        storage.users.get_dashboard_data.return_value = make_dashboard_data()

        page = impersonate_client.get("/dashboard").get_data(as_text=True)

        assert "Nettoyer maintenant" not in page

    def test_le_refus_mutant_affiche_la_page_francaise(self, impersonate_client, storage):
        resp = impersonate_client.post("/cleanup", data={"days": "4"})

        assert resp.status_code == 403
        assert "Lecture seule" in resp.get_data(as_text=True)

    def test_le_lien_admin_disparait_pendant_la_vue(self, impersonate_client, storage):
        """session['username'] vaut la cible : le lien Admin du layout ne doit
        plus s'afficher (il pointerait vers une zone inaccessible)."""
        storage.users.get_dashboard_data.return_value = make_dashboard_data()

        page = impersonate_client.get("/dashboard").get_data(as_text=True)

        assert 'href="/admin"' not in page


# ---------------------------------------------------------------------------
# 4. Stats détaillées de la fiche utilisateur
# ---------------------------------------------------------------------------

STATS_FIXTURES = {
    "recherches_total": 5,
    "recherches_actives": 3,
    "recherches_inactives": 2,
    "annonces_7j": 12,
    "annonces_30j": 47,
    "sources_utilisees": ["laforet", "seloger"],
}


@pytest.fixture
def detail_utilisateur(storage):
    storage.users.get_user_detail.return_value = make_user_detail(id=1, username="alice")
    storage.users.get_user_stats.return_value = dict(STATS_FIXTURES)
    storage.scrape_logs.get_dernier_scrape_resultat.return_value = {
        "dernier_succes": datetime(2026, 8, 20, 14, 30),
        "dernier_echec": datetime(2026, 8, 21, 9, 15),
    }
    return storage.users.get_user_detail.return_value


class TestStatsUtilisateur:
    def test_les_cartes_statistiques_sont_alimentees_par_les_repos(
        self, admin_client, storage, detail_utilisateur
    ):
        page = admin_client.get("/admin/users/1").get_data(as_text=True)

        assert "Recherches actives" in page
        assert "Recherches en pause" in page
        assert ">3<" in page and ">2<" in page
        assert "Annonces trouvées (7 j)" in page and ">12<" in page
        assert "Annonces trouvées (30 j)" in page and ">47<" in page
        assert "seloger" in page and "laforet" in page
        # Les dates sont formatées à la française (#12) ; l'heure est
        # convertie vers Europe/Paris, on n'affirme que la date du jour UTC→FR.
        assert "20/08/2026" in page
        assert "21/08/2026" in page

    def test_les_repos_recoivent_le_bon_perimetre(
        self, admin_client, storage, detail_utilisateur
    ):
        admin_client.get("/admin/users/1")

        storage.users.get_user_stats.assert_called_once_with(1)
        # Le dernier scrape est demandé pour LES recherches du détail reçu.
        storage.scrape_logs.get_dernier_scrape_resultat.assert_called_once_with([1])

    def test_un_echec_de_stats_ne_casse_pas_la_fiche(self, admin_client, storage):
        """Fail-open : une lecture de stats qui lève prive sa carte (« — »),
        jamais la fiche entière (même contrat que les zones vivantes #17/#21)."""
        storage.users.get_user_detail.return_value = make_user_detail(id=1)
        storage.users.get_user_stats.side_effect = RuntimeError("boom")
        storage.scrape_logs.get_dernier_scrape_resultat.side_effect = RuntimeError("boom")

        resp = admin_client.get("/admin/users/1")

        assert resp.status_code == 200
        assert "Dernier scrape réussi" in resp.get_data(as_text=True)

    def test_aucun_scrape_affiche_l_etat_neutre(self, admin_client, storage, detail_utilisateur):
        storage.scrape_logs.get_dernier_scrape_resultat.return_value = {
            "dernier_succes": None,
            "dernier_echec": None,
        }

        page = admin_client.get("/admin/users/1").get_data(as_text=True)

        assert "Dernier scrape réussi" in page
        # Pas de date formatée pour les scrapes : les cellules montrent « — ».
        assert "20/08/2026" not in page


# ---------------------------------------------------------------------------
# 5. Non-régression : hors impersonation, rien n'a changé
# ---------------------------------------------------------------------------


class TestNonRegression:
    def test_le_flux_normal_des_actions_utilisateur_passe(
        self, web_client, storage, owned_search
    ):
        """Une action mutante emblématique (toggle-active, badge #10 au même
        titre que toggle-notify) reste exécutable hors impersonation."""
        storage.searches.toggle_search_active.return_value = False

        resp = web_client.post(f"/searches/{owned_search['id']}/toggle-active")

        assert resp.status_code == 302
        storage.searches.toggle_search_active.assert_called_once_with(owned_search["id"])

    def test_les_onglets_admin_rendent_normalement(self, admin_client, storage, admin_stats):
        assert admin_client.get("/admin?tab=users").status_code == 200
