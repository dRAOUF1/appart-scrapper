"""Tests for route blueprints and auth decorators."""
from unittest.mock import MagicMock, patch
from flask import Flask


def _make_app():
    """Create a minimal Flask app with storage mock and all blueprints."""
    app = Flask(__name__, template_folder="templates")
    app.secret_key = "test-secret"
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
            assert resp.data == b"OK"


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
