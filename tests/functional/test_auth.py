"""Les trois décorateurs de `routes/auth.py`, exercés par de vraies routes.

Ils sont testés à travers des endpoints réels (`/api/stats`, `/dashboard`,
`/admin`) plutôt que sur des fonctions jouets : ce qui compte n'est pas que le
décorateur renvoie 401, c'est que la route protégée ne s'exécute jamais.
"""

from __future__ import annotations

import pytest

from tests.functional.conftest import ADMIN_USERNAME, make_admin_stats, make_dashboard_data, make_user_stats


class TestRequireToken:
    def test_missing_header_is_rejected_with_a_named_error(self, client, storage):
        resp = client.get("/api/stats")

        assert resp.status_code == 401
        assert resp.get_json() == {"error": "Header X-API-Token manquant"}
        # Le token n'est même pas cherché en base : pas de requête pour rien.
        storage.users.get_user_by_token.assert_not_called()
        storage.users.get_user_stats.assert_not_called()

    def test_empty_header_is_treated_as_missing(self, client, storage):
        """`""` est falsy : on ne va pas interroger la base avec un token vide."""
        resp = client.get("/api/stats", headers={"X-API-Token": ""})

        assert resp.status_code == 401
        assert resp.get_json()["error"] == "Header X-API-Token manquant"
        storage.users.get_user_by_token.assert_not_called()

    def test_unknown_token_is_rejected(self, client, storage, user):
        resp = client.get("/api/stats", headers={"X-API-Token": "token-inconnu"})

        assert resp.status_code == 401
        assert resp.get_json() == {"error": "Token invalide"}
        storage.users.get_user_by_token.assert_called_once_with("token-inconnu")
        storage.users.get_user_stats.assert_not_called()

    def test_valid_token_populates_g_user_for_the_route(self, api_client, storage, user):
        """La route lit `g.user["id"]` : c'est l'identité effective de la requête."""
        storage.users.get_user_stats.return_value = make_user_stats(searches=4)

        resp = api_client.get("/api/stats")

        assert resp.status_code == 200
        assert resp.get_json()["searches"] == 4
        storage.users.get_user_stats.assert_called_once_with(user["id"])

    def test_session_cookie_alone_does_not_authenticate_the_api(self, web_client, storage):
        """Une session web ne vaut pas jeton : l'API exige le header.

        Sans ça, une page tierce pourrait piloter l'API du navigateur de la
        victime (l'API étant exemptée de CSRF).
        """
        resp = web_client.get("/api/stats")

        assert resp.status_code == 401
        storage.users.get_user_stats.assert_not_called()


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

    def test_revoked_token_clears_the_session_and_redirects(self, web_client, storage):
        """Le token de session est REVALIDÉ en base à chaque requête.

        C'est ce qui rend `/admin/users/<id>/reset-token` effectif : le token
        remplacé déconnecte immédiatement les sessions qui le portaient, au lieu
        de les laisser vivre jusqu'à expiration du cookie.
        """
        storage.users.get_user_by_token.side_effect = None
        storage.users.get_user_by_token.return_value = None

        resp = web_client.get("/dashboard")

        assert resp.status_code == 302
        assert resp.headers["Location"] == "/login"
        storage.users.get_dashboard_data.assert_not_called()
        # La session a été vidée : la requête suivante est bien anonyme.
        with web_client.session_transaction() as sess:
            assert "user_id" not in sess
            assert "api_token" not in sess

    def test_a_session_without_api_token_is_rejected(self, app_without_csrf, storage, user):
        """`session["user_id"]` seul ne suffit pas : le token est indispensable."""
        client = app_without_csrf.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user["id"]
            sess["username"] = user["username"]

        resp = client.get("/dashboard")

        assert resp.status_code == 302
        assert resp.headers["Location"] == "/login"
        storage.users.get_user_by_token.assert_called_once_with("")

    def test_identity_comes_from_the_token_not_from_the_session_user_id(
        self, app_without_csrf, storage, user
    ):
        """Un `user_id` forgé dans la session est sans effet.

        `g.user` vient exclusivement de la ligne retrouvée par token : usurper
        `session["user_id"]` (si le cookie était falsifiable) ne changerait pas
        l'utilisateur vu par les routes.
        """
        client = app_without_csrf.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = 4242
            sess["username"] = user["username"]
            sess["api_token"] = user["api_token"]
        storage.users.get_dashboard_data.return_value = make_dashboard_data()

        resp = client.get("/dashboard")

        assert resp.status_code == 200
        storage.users.get_dashboard_data.assert_called_once_with(user["id"])


class TestRequireAdmin:
    def test_anonymous_visitor_is_redirected_to_login(self, client, storage):
        resp = client.get("/admin")

        assert resp.status_code == 302
        assert resp.headers["Location"] == "/login"
        storage.admin.get_enhanced_admin_stats.assert_not_called()

    def test_revoked_token_clears_the_session_and_redirects(self, admin_client, storage):
        storage.users.get_user_by_token.side_effect = None
        storage.users.get_user_by_token.return_value = None

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

        row = make_user_row(id=99, username=username, api_token="token-x")
        storage.users.get_user_by_token.side_effect = lambda t: row if t == "token-x" else None
        client = app_without_csrf.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = row["id"]
            sess["username"] = row["username"]
            sess["api_token"] = row["api_token"]

        resp = client.get("/admin")

        assert resp.status_code == 302
        assert resp.headers["Location"] == "/dashboard"
        storage.admin.get_enhanced_admin_stats.assert_not_called()
