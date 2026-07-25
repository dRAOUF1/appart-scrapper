"""Tests for route blueprints and auth decorators."""
import os
from unittest.mock import MagicMock, patch
from flask import Flask
from flask_wtf.csrf import generate_csrf

_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)


def _make_app():
    """Create a minimal Flask app with storage mock and all blueprints.

    Registers the csrf_token() Jinja global (so templates using it render)
    without enabling CSRFProtect's enforcement — that's tested separately in
    tests/test_security.py::TestCSRFProtection, and enforcing it here would
    break every other test in this file that POSTs without a token.
    """
    app = Flask(__name__, template_folder=_TEMPLATES_DIR)
    app.secret_key = "test-secret"
    app.jinja_env.globals["csrf_token"] = generate_csrf
    app.storage = MagicMock()
    app._scrape_futures = {}
    app._scrape_executor = MagicMock()

    from routes import api_bp, web_bp, admin_bp
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(web_bp)
    app.register_blueprint(admin_bp)
    return app


class TestAuthDecorators:
    def test_require_token_missing_header(self):
        from routes.auth import require_token

        app = _make_app()
        with app.test_request_context("/api/test", method="GET"):
            from flask import jsonify

            @require_token
            def protected():
                return jsonify({"ok": True})

            result = protected()
            assert result[1] == 401

    def test_require_token_invalid_token(self):
        from routes.auth import require_token

        app = _make_app()
        app.storage.users.get_user_by_token.return_value = None

        with app.test_request_context("/api/test", method="GET", headers={"X-API-Token": "bad"}):
            @require_token
            def protected():
                pass

            result = protected()
            assert result[1] == 401

    def test_require_token_valid(self):
        from routes.auth import require_token

        app = _make_app()
        app.storage.users.get_user_by_token.return_value = {"id": 1, "username": "test"}

        with app.test_request_context("/api/test", method="GET", headers={"X-API-Token": "good"}):
            from flask import g, jsonify

            @require_token
            def protected():
                return jsonify({"user": g.user["username"]}), 200

            result = protected()
            assert result[0].get_json()["user"] == "test"
            assert result[1] == 200


class TestAPIRoutes:
    def test_get_sources(self):
        app = _make_app()
        with app.test_client() as client:
            with patch("routes.api.list_sources", return_value=[{"id": "seloger", "name": "SeLoger"}]):
                resp = client.get("/api/sources")
                assert resp.status_code == 200
                data = resp.get_json()
                assert len(data) == 1

    def test_create_user(self):
        app = _make_app()
        app.storage.users.create_user.return_value = {"id": 1, "username": "test", "api_token": "tok123"}

        with app.test_client() as client:
            resp = client.post("/api/users", json={"username": "test"})
            assert resp.status_code == 201
            data = resp.get_json()
            assert data["username"] == "test"

    def test_create_user_missing_username(self):
        app = _make_app()
        with app.test_client() as client:
            resp = client.post("/api/users", json={})
            assert resp.status_code == 400

    def test_login_user_found(self):
        app = _make_app()
        app.storage.users.get_user_by_username.return_value = {"id": 1, "username": "test", "api_token": "tok"}

        with app.test_client() as client:
            resp = client.post("/api/users/login", json={"username": "test"})
            assert resp.status_code == 200

    def test_login_user_not_found(self):
        app = _make_app()
        app.storage.users.get_user_by_username.return_value = None

        with app.test_client() as client:
            resp = client.post("/api/users/login", json={"username": "nope"})
            assert resp.status_code == 404

    def test_scrape_search_already_running(self):
        app = _make_app()
        app.storage.searches.get_search.return_value = {"user_id": 1, "criteria": {"placeIds": ["123"]}}
        app.storage.users.get_user_by_token.return_value = {"id": 1, "username": "test"}
        mock_future = MagicMock()
        mock_future.done.return_value = False
        app._scrape_futures[1] = mock_future

        with app.test_client() as client:
            resp = client.post(
                "/api/scrape/1",
                headers={"X-API-Token": "tok"},
            )
            assert resp.status_code == 409

    def test_get_stats(self):
        app = _make_app()
        app.storage.users.get_user_stats.return_value = {"searches": 2, "total_listings": 10, "new_today": 3}

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["api_token"] = "tok"
            app.storage.users.get_user_by_token.return_value = {"id": 1, "username": "test"}
            resp = client.get("/api/stats", headers={"X-API-Token": "tok"})
            assert resp.status_code == 200
            assert resp.get_json()["searches"] == 2


class TestWebRoutes:
    def test_index_redirects_to_login(self):
        app = _make_app()
        with app.test_client() as client:
            resp = client.get("/", follow_redirects=False)
            assert resp.status_code in (301, 302, 303)

    def test_health(self):
        app = _make_app()
        with app.test_client() as client:
            resp = client.get("/health")
            assert resp.status_code == 200

    def test_login_page_renders(self):
        app = _make_app()
        with app.test_client() as client:
            resp = client.get("/login")
            assert resp.status_code == 200

    def test_login_post_empty_username_shows_error(self):
        app = _make_app()
        with app.test_client() as client:
            resp = client.post("/login", data={"username": ""})
            assert resp.status_code == 200
            app.storage.users.get_user_by_username.assert_not_called()

    def test_login_post_existing_user_logs_in(self):
        app = _make_app()
        app.storage.users.get_user_by_username.return_value = {
            "id": 1, "username": "existing", "api_token": "tok-existing",
        }
        with app.test_client() as client:
            resp = client.post("/login", data={"username": "existing"}, follow_redirects=False)
            assert resp.status_code in (301, 302, 303)
            with client.session_transaction() as sess:
                assert sess["user_id"] == 1
                assert sess["username"] == "existing"
                assert sess["api_token"] == "tok-existing"
        app.storage.users.create_user.assert_not_called()

    def test_login_post_new_username_auto_creates_account(self):
        app = _make_app()
        app.storage.users.get_user_by_username.return_value = None
        app.storage.users.create_user.return_value = {
            "id": 2, "username": "newbie", "api_token": "tok-newbie",
        }
        with app.test_client() as client:
            resp = client.post("/login", data={"username": "newbie"}, follow_redirects=False)
            assert resp.status_code in (301, 302, 303)
        app.storage.users.create_user.assert_called_once_with("newbie")

    def test_login_post_create_user_value_error_shows_error(self):
        app = _make_app()
        app.storage.users.get_user_by_username.return_value = None
        app.storage.users.create_user.side_effect = ValueError("boom")
        with app.test_client() as client:
            resp = client.post("/login", data={"username": "conflict"})
            assert resp.status_code == 200

    def test_searches_create_form_reopens_after_validation_error(self):
        """The create-search form is collapsed by default once searches
        exist, but a failed validation must reopen it — otherwise the error
        toast appears with no visible form to act on it."""
        app = _make_app()
        app.storage.users.get_user_by_token.return_value = {"id": 1, "username": "u", "api_token": "tok"}
        app.storage.searches.get_user_searches.return_value = [
            {"id": 1, "label": "Existing", "sources": ["laforet"], "source": "laforet", "criteria": {}}
        ]
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["username"] = "u"
                sess["api_token"] = "tok"
            client.post("/searches", data={"label": "Bad", "ntfy_topic": "test"}, follow_redirects=False)
            resp = client.get("/searches")
        assert b'<details class="card create-search-card" open>' in resp.data

    def test_logout_clears_session_and_redirects(self):
        app = _make_app()
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["username"] = "someone"
            resp = client.get("/logout", follow_redirects=False)
            assert resp.status_code in (301, 302, 303)
            with client.session_transaction() as sess:
                assert "user_id" not in sess


def _make_admin_client(app, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    app.storage.users.get_user_by_token.return_value = {"id": 1, "username": "admin"}
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["api_token"] = "tok"
        sess["username"] = "admin"
    return client


class TestAdminRoutes:
    def test_admin_requires_admin_user(self):
        app = _make_app()
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = 1
                sess["api_token"] = "tok"
            app.storage.users.get_user_by_token.return_value = {"id": 1, "username": "notadmin"}
            resp = client.get("/admin", follow_redirects=False)
            assert resp.status_code in (301, 302, 303)

    def test_admin_dashboard_accessible_to_real_admin(self, monkeypatch):
        app = _make_app()
        app.storage.admin.get_enhanced_admin_stats.return_value = {
            "users": 0, "searches": 0, "total_listings": 0, "new_today": 0,
            "avg_listings_per_search": 0, "search_listings": 0,
            "orphan_listings": 0, "users_without_searches": 0,
            "top_searches": [], "top_users": [], "sources_breakdown": [],
            "activity_": [],
        }
        app.storage.settings.get_setting.return_value = "true"
        client = _make_admin_client(app, monkeypatch)

        resp = client.get("/admin")
        assert resp.status_code == 200

    def test_admin_execute_query_select_returns_rows(self, monkeypatch):
        app = _make_app()
        app.storage.admin.execute_query.return_value = ([{"n": 1}], 1, None)
        app.storage.admin.get_db_stats.return_value = {}
        client = _make_admin_client(app, monkeypatch)

        resp = client.post("/admin/database/query", data={"sql": "SELECT 1"})

        assert resp.status_code == 200
        app.storage.admin.execute_query.assert_called_once_with("SELECT 1")
        app.storage.admin.log_admin_action.assert_called_once()

    def test_admin_execute_query_rejects_empty_sql(self, monkeypatch):
        app = _make_app()
        client = _make_admin_client(app, monkeypatch)

        resp = client.post("/admin/database/query", data={"sql": ""}, follow_redirects=False)

        assert resp.status_code in (301, 302, 303)
        app.storage.admin.execute_query.assert_not_called()

    def test_admin_delete_user(self, monkeypatch):
        app = _make_app()
        app.storage.users.get_user_detail.return_value = {"id": 2, "username": "victim"}
        client = _make_admin_client(app, monkeypatch)

        resp = client.post("/admin/users/2/delete", follow_redirects=False)

        assert resp.status_code in (301, 302, 303)
        app.storage.users.delete_user.assert_called_once_with(2)
        app.storage.admin.log_admin_action.assert_called_once()

    def test_admin_delete_user_missing_user_is_noop(self, monkeypatch):
        app = _make_app()
        app.storage.users.get_user_detail.return_value = None
        client = _make_admin_client(app, monkeypatch)

        client.post("/admin/users/999/delete", follow_redirects=False)

        app.storage.users.delete_user.assert_not_called()

    def test_admin_reset_token(self, monkeypatch):
        app = _make_app()
        app.storage.users.get_user_detail.return_value = {"id": 2, "username": "someone"}
        app.storage.users.reset_user_token.return_value = "new-token-value"
        client = _make_admin_client(app, monkeypatch)

        resp = client.post("/admin/users/2/reset-token", follow_redirects=False)

        assert resp.status_code in (301, 302, 303)
        app.storage.users.reset_user_token.assert_called_once_with(2)

    def test_admin_create_user(self, monkeypatch):
        app = _make_app()
        app.storage.users.create_user.return_value = {"id": 3, "username": "newuser", "api_token": "tok3"}
        client = _make_admin_client(app, monkeypatch)

        resp = client.post("/admin/users/create", data={"username": "newuser"}, follow_redirects=False)

        assert resp.status_code in (301, 302, 303)
        app.storage.users.create_user.assert_called_once_with("newuser")

    def test_admin_create_user_missing_username(self, monkeypatch):
        app = _make_app()
        client = _make_admin_client(app, monkeypatch)

        client.post("/admin/users/create", data={"username": ""}, follow_redirects=False)

        app.storage.users.create_user.assert_not_called()

    def test_admin_truncate_table_rejects_table_outside_whitelist(self, monkeypatch):
        app = _make_app()
        client = _make_admin_client(app, monkeypatch)

        resp = client.post(
            "/admin/database/truncate", data={"table_name": "pg_catalog"}, follow_redirects=False,
        )

        assert resp.status_code in (301, 302, 303)
        app.storage.admin.truncate_table.assert_not_called()

    def test_admin_truncate_table_allows_whitelisted_table(self, monkeypatch):
        app = _make_app()
        app.storage.admin.truncate_table.return_value = True
        client = _make_admin_client(app, monkeypatch)

        client.post("/admin/database/truncate", data={"table_name": "listings"}, follow_redirects=False)

        app.storage.admin.truncate_table.assert_called_once_with("listings")


class TestParseSearchCriteriaFromForm:
    def test_form_produces_canonical_vocabulary_only(self):
        """Le formulaire est commun à toutes les sources : il ne doit jamais
        produire le vocabulaire de l'une d'elles."""
        from routes.web import _parse_search_criteria_from_form

        import json

        form = {
            "location_city": "Paris (75014)",
            "location_postal_code": "75014",
            "location_payload": json.dumps({
                "kind": "city", "city": "Paris", "postalCode": "75014", "inseeCode": "75114",
            }),
            "transaction": "buy", "price_max": "500000",
            "surface_min": "40",
        }
        criteria = _parse_search_criteria_from_form(form)
        assert criteria["transaction"] == "buy"
        assert criteria["priceMax"] == 500000
        assert criteria["surfaceMin"] == 40
        assert criteria["locations"] == [
            {"kind": "city", "city": "Paris", "postalCode": "75014", "inseeCode": "75114"},
        ]
        for source_specific in ("placeIds", "distributionTypes", "estateTypes", "spaceMin", "order"):
            assert source_specific not in criteria

    def test_a_chosen_area_is_transported_as_a_payload(self):
        """L'autocomplete peut proposer une région, un département ou une ville
        entière : le périmètre choisi voyage en JSON dans un champ caché, plutôt
        qu'éclaté en un champ de formulaire par niveau et par attribut."""
        import json
        from routes.web import _parse_search_criteria_from_form

        idf = {"kind": "region", "label": "Île-de-France (région)",
               "name": "Île-de-France", "code": "11", "departments": ["75", "92"]}
        criteria = _parse_search_criteria_from_form({
            "location_city": idf["label"],
            "location_payload": json.dumps(idf),
        })
        assert criteria["locations"] == [{
            "kind": "region", "name": "Île-de-France", "code": "11",
            "departments": ["75", "92"],
        }]
        # `label` n'est qu'un texte d'affichage, il n'a rien à faire en base.
        assert "label" not in criteria["locations"][0]

    def test_several_levels_in_one_search(self):
        import json
        from werkzeug.datastructures import MultiDict
        from routes.web import _parse_search_criteria_from_form

        form = MultiDict([
            ("location_city", "Gironde (33)"),
            ("location_payload", json.dumps({"kind": "department", "name": "Gironde", "code": "33"})),
            ("location_postal_code", ""),
            ("location_city", "Poitiers (86000)"),
            ("location_payload", json.dumps({"kind": "city", "city": "Poitiers",
                                             "postalCode": "86000", "inseeCode": "86194"})),
            ("location_postal_code", "86000"),
        ])
        criteria = _parse_search_criteria_from_form(form)
        assert [loc["kind"] for loc in criteria["locations"]] == ["department", "city"]

    def test_a_hand_typed_line_without_a_payload_is_read_as_a_city(self):
        """Le champ de localisation est unique : le code postal n'est plus
        ressaisi à part, il est lu dans le texte tapé."""
        from routes.web import _parse_search_criteria_from_form

        for texte in ("Poitiers 86000", "Poitiers (86000)", "86000 Poitiers"):
            criteria = _parse_search_criteria_from_form({
                "location_city": texte, "location_payload": "",
            })
            assert criteria["locations"] == [
                {"kind": "city", "city": "Poitiers", "postalCode": "86000"}
            ], texte

    def test_a_typed_line_without_a_postal_code_is_dropped(self):
        """Sans code postal, la commune n'est pas identifiable — et il ne faut
        surtout pas en deviner une, plusieurs pouvant porter le même nom."""
        from routes.web import _parse_search_criteria_from_form

        criteria = _parse_search_criteria_from_form({
            "location_city": "Poitiers", "location_payload": "",
        })
        assert "locations" not in criteria

    def test_a_corrupted_payload_falls_back_to_the_typed_text(self):
        """Un payload illisible ne doit pas faire perdre la ligne."""
        from routes.web import _parse_search_criteria_from_form

        criteria = _parse_search_criteria_from_form({
            "location_city": "Poitiers (86000)",
            "location_payload": "{pas du json",
        })
        assert criteria["locations"] == [{"kind": "city", "city": "Poitiers", "postalCode": "86000"}]

    def test_manual_override_is_kept_apart_from_the_criteria(self):
        from routes.web import _parse_search_criteria_from_form

        form = {
            "location_city": "Paris (75014)",
            "override_seloger": "AD08FR12345",
        }
        criteria = _parse_search_criteria_from_form(form)
        assert criteria["sourceOverrides"]["seloger"] == {"placeIds": ["AD08FR12345"]}
        assert "placeIds" not in criteria

    def test_multiple_location_rows_build_locations_list(self):
        """Une recherche peut couvrir plusieurs localisations — une ligne, donc
        un champ, par périmètre."""
        from werkzeug.datastructures import MultiDict
        from routes.web import _parse_search_criteria_from_form

        form = MultiDict([
            ("location_city", "Paris (75014)"),
            ("location_city", "Lyon (69007)"),
        ])
        criteria = _parse_search_criteria_from_form(form)
        assert criteria["locations"] == [
            {"kind": "city", "city": "Paris", "postalCode": "75014"},
            {"kind": "city", "city": "Lyon", "postalCode": "69007"},
        ]

    def test_incomplete_location_rows_are_skipped(self):
        """Une ligne vide (l'utilisateur en a ajouté une sans la remplir) ou
        sans code postal ne doit pas produire de localisation bancale."""
        from werkzeug.datastructures import MultiDict
        from routes.web import _parse_search_criteria_from_form

        form = MultiDict([
            ("location_city", "Paris (75014)"),
            ("location_city", ""),
            ("location_city", "Lyon"),
        ])
        criteria = _parse_search_criteria_from_form(form)
        assert criteria["locations"] == [{"kind": "city", "city": "Paris", "postalCode": "75014"}]

    def test_no_location_rows_means_no_locations_key(self):
        from routes.web import _parse_search_criteria_from_form

        criteria = _parse_search_criteria_from_form({"override_seloger": "AD08FR12345"})
        assert "locations" not in criteria


class TestValidateSourcesCriteria:
    """Per-source validation must give a precise, actionable reason instead
    of one generic message covering every selected source indiscriminately."""

    def test_laforet_valid_with_city_and_postal_code(self):
        from routes.web import _validate_sources_criteria

        results = _validate_sources_criteria(["laforet"], {"city": "Paris", "postalCode": "75018"})
        assert results == [{"id": "laforet", "name": "Laforêt", "ok": True, "reason": ""}]

    def test_laforet_invalid_without_location_gives_generic_reason(self):
        from routes.web import _validate_sources_criteria

        results = _validate_sources_criteria(["laforet"], {})
        assert len(results) == 1
        assert results[0]["ok"] is False
        assert "localisation" in results[0]["reason"]

    def test_seloger_invalid_gives_source_specific_help_text(self):
        """La raison de SeLoger doit expliquer ce qui manque VRAIMENT (le code
        INSEE, qu'on obtient en choisissant la ville dans les suggestions), pas
        un « Ville et code postal requis » générique alors qu'ils sont remplis."""
        from routes.web import _validate_sources_criteria

        results = _validate_sources_criteria(["seloger"], {"city": "Paris", "postalCode": "75018"})
        assert results[0]["ok"] is False
        assert "INSEE" in results[0]["reason"]

    def test_source_that_cannot_honour_a_property_type_is_reported(self):
        """Régression : Laforet acceptait « parking » à la validation puis
        levait une ValueError pendant le scrape."""
        from routes.web import _validate_sources_criteria

        results = _validate_sources_criteria(["laforet"], {
            "locations": [{"city": "Paris", "postalCode": "75018"}],
            "propertyTypes": ["parking"],
        })
        assert results[0]["ok"] is False
        assert "Parking" in results[0]["reason"]

    def test_unknown_source_reported_as_invalid(self):
        from routes.web import _validate_sources_criteria

        results = _validate_sources_criteria(["totally_unknown"], {"city": "Paris", "postalCode": "75018"})
        assert results == [{"id": "totally_unknown", "name": "totally_unknown", "ok": False, "reason": "Source inconnue"}]

    def test_multiple_sources_validated_independently(self):
        from routes.web import _validate_sources_criteria

        results = _validate_sources_criteria(
            ["seloger", "laforet"], {"city": "Paris", "postalCode": "75018"}
        )
        by_id = {r["id"]: r for r in results}
        # Laforet se contente de ville + code postal ; SeLoger a besoin du code
        # INSEE que fournit l'autocomplete pour résoudre son placeId.
        assert by_id["seloger"]["ok"] is False
        assert by_id["laforet"]["ok"] is True


class TestValidationErrorMessage:
    def test_lists_only_failing_sources_with_their_reason(self):
        from routes.web import _validation_error_message

        results = [
            {"id": "seloger", "name": "SeLoger", "ok": False, "reason": "besoin d'un Place ID"},
            {"id": "laforet", "name": "Laforêt", "ok": True, "reason": ""},
        ]
        message = _validation_error_message(results)
        assert "SeLoger" in message
        assert "besoin d'un Place ID" in message
        assert "Laforêt" not in message

    def test_empty_when_all_valid(self):
        from routes.web import _validation_error_message

        results = [{"id": "laforet", "name": "Laforêt", "ok": True, "reason": ""}]
        assert _validation_error_message(results) == ""


class TestRouteIntegrity:
    def test_all_blueprints_importable(self):
        from routes import api_bp, web_bp, admin_bp
        assert api_bp is not None
        assert web_bp is not None
        assert admin_bp is not None

    def test_auth_decorators_importable(self):
        from routes.auth import require_token, require_login, require_admin
        assert require_token is not None
        assert require_login is not None
        assert require_admin is not None
