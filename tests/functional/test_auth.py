"""Les deux décorateurs de `routes/auth.py`, exercés par de vraies routes.

Ils sont testés à travers des endpoints réels (`/dashboard`, `/admin`) plutôt
que sur des fonctions jouets : ce qui compte, c'est que la route protégée ne
s'exécute jamais sans utilisateur valide.

L'authentification passe par la session Flask : `require_login` et
`require_admin` résolvent l'utilisateur via `get_user_by_id(session["user_id"])`
à CHAQUE requête — supprimer le compte en admin déconnecte donc immédiatement
ses sessions, au lieu de les laisser vivre jusqu'à expiration du cookie.
"""

from __future__ import annotations

import pytest

from tests.functional.conftest import ADMIN_USERNAME, make_admin_stats, make_dashboard_data


class TestRequireLogin:
    def test_anonymous_visitor_is_redirected_to_login(self, client, storage):
        resp = client.get("/dashboard")

        assert resp.status_code == 302
        assert resp.headers["Location"] == "/login"
        storage.users.get_dashboard_data.assert_not_called()

    def test_logged_in_user_reaches_the_route(self, web_client, storage, user):
        storage.users.get_dashboard_data.return_value = make_dashboard_data()

        resp = web_client.get("/dashboard")

        assert resp.status_code == 200
        storage.users.get_dashboard_data.assert_called_once_with(user["id"])
        # L'identité vient de la ligne résolue en base par `get_user_by_id`,
        # pas d'une quelconque donnée de session autre que `user_id`.
        storage.users.get_user_by_id.assert_called_once_with(user["id"])

    def test_a_session_whose_user_no_longer_exists_is_cleared(self, web_client, storage):
        """La ligne est REVALIDÉE en base à chaque requête.

        Un compte supprimé (ou une base restaurée) rend `get_user_by_id` muet :
        la session est vidée sur-le-champ plutôt que laissée à moitié vivante.
        """
        storage.users.get_user_by_id.side_effect = None
        storage.users.get_user_by_id.return_value = None

        resp = web_client.get("/dashboard")

        assert resp.status_code == 302
        assert resp.headers["Location"] == "/login"
        storage.users.get_dashboard_data.assert_not_called()
        # La session a été vidée : la requête suivante est bien anonyme.
        with web_client.session_transaction() as sess:
            assert "user_id" not in sess


class TestRequireAdmin:
    def test_anonymous_visitor_is_redirected_to_login(self, client, storage):
        resp = client.get("/admin")

        assert resp.status_code == 302
        assert resp.headers["Location"] == "/login"
        storage.admin.get_enhanced_admin_stats.assert_not_called()

    def test_a_session_whose_user_no_longer_exists_is_cleared(self, admin_client, storage):
        storage.users.get_user_by_id.side_effect = None
        storage.users.get_user_by_id.return_value = None

        resp = admin_client.get("/admin")

        assert resp.status_code == 302
        assert resp.headers["Location"] == "/login"
        storage.admin.get_enhanced_admin_stats.assert_not_called()

    def test_non_admin_user_is_flashed_and_sent_to_the_dashboard(self, web_client, storage):
        resp = web_client.get("/admin")

        assert resp.status_code == 302
        assert resp.headers["Location"] == "/dashboard"
        storage.admin.get_enhanced_admin_stats.assert_not_called()

        storage.users.get_dashboard_data.return_value = make_dashboard_data()
        page = web_client.get("/dashboard").get_data(as_text=True)
        assert "Accès refusé" in page

    def test_matching_username_grants_access(self, admin_client, storage):
        storage.admin.get_enhanced_admin_stats.return_value = make_admin_stats()

        resp = admin_client.get("/admin")

        assert resp.status_code == 200
        storage.admin.get_enhanced_admin_stats.assert_called_once_with()

    def test_admin_check_is_fail_closed_when_admin_username_is_unset(
        self, admin_client, storage, monkeypatch
    ):
        """Sans `ADMIN_USERNAME`, PERSONNE n'est admin.

        L'alternative — traiter l'absence comme « tout le monde » ou comparer à
        une valeur par défaut — ferait d'une variable d'environnement oubliée
        au déploiement une élévation de privilège complète.
        """
        monkeypatch.delenv("ADMIN_USERNAME", raising=False)

        resp = admin_client.get("/admin")

        assert resp.status_code == 302
        assert resp.headers["Location"] == "/dashboard"
        storage.admin.get_enhanced_admin_stats.assert_not_called()

    @pytest.mark.parametrize(
        "username",
        [
            ADMIN_USERNAME.upper(),
            ADMIN_USERNAME.capitalize(),
            f" {ADMIN_USERNAME} ",
            f"{ADMIN_USERNAME}x",
        ],
    )
    def test_admin_comparison_is_exact_and_case_sensitive(
        self, app_without_csrf, storage, username
    ):
        """La comparaison est un `!=` strict : aucune variante ne passe.

        À noter : les usernames sont enregistrés en minuscules
        (`.strip().lower()` à l'inscription), donc un `ADMIN_USERNAME` contenant
        une majuscule ne peut correspondre à AUCUN compte — fail-closed, mais
        silencieusement.
        """
        from tests.helpers.factories import make_user_row

        row = make_user_row(id=99, username=username)
        storage.users.get_user_by_id.side_effect = lambda user_id: row if user_id == row["id"] else None
        client = app_without_csrf.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = row["id"]
            sess["username"] = row["username"]

        resp = client.get("/admin")

        assert resp.status_code == 302
        assert resp.headers["Location"] == "/dashboard"
        storage.admin.get_enhanced_admin_stats.assert_not_called()
