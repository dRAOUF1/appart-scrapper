"""Tests for security fixes: IDOR, CSRF, read-only admin queries, token leak."""
import threading
import time
from unittest.mock import MagicMock
from flask import Flask
from flask_wtf import CSRFProtect

from repositories.admin_repo import AdminRepository
from core.scrape_control import submit_scrape


def _make_app():
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


class TestIDORFixes:
    def test_update_criteria_rejects_other_users_search(self):
        app = _make_app()
        app.storage.searches.get_search.return_value = {"user_id": 2, "criteria": {}}
        app.storage.users.get_user_by_token.return_value = {"id": 1, "username": "test"}

        with app.test_client() as client:
            resp = client.put(
                "/api/searches/1/criteria",
                json={"criteria": {"placeIds": ["x"]}},
                headers={"X-API-Token": "tok"},
            )
            assert resp.status_code == 404
            app.storage.searches.update_search_criteria.assert_not_called()

    def test_update_criteria_allows_owner(self):
        app = _make_app()
        app.storage.searches.get_search.return_value = {"user_id": 1, "criteria": {}}
        app.storage.users.get_user_by_token.return_value = {"id": 1, "username": "test"}

        with app.test_client() as client:
            resp = client.put(
                "/api/searches/1/criteria",
                json={"criteria": {"placeIds": ["x"]}},
                headers={"X-API-Token": "tok"},
            )
            assert resp.status_code == 200
            app.storage.searches.update_search_criteria.assert_called_once()


class TestCriteriaValidation:
    def test_create_search_rejects_invalid_criteria(self):
        app = _make_app()
        app.storage.users.get_user_by_token.return_value = {"id": 1, "username": "test"}

        with app.test_client() as client:
            resp = client.post(
                "/api/searches",
                json={
                    "label": "Test", "ntfy_topic": "topic",
                    "criteria": {"priceMax": "not-a-number"},
                },
                headers={"X-API-Token": "tok"},
            )
            assert resp.status_code == 400
            app.storage.searches.create_search.assert_not_called()

    def test_create_search_rejects_invalid_scrape_interval(self):
        app = _make_app()
        app.storage.users.get_user_by_token.return_value = {"id": 1, "username": "test"}

        with app.test_client() as client:
            resp = client.post(
                "/api/searches",
                json={
                    "label": "Test", "ntfy_topic": "topic",
                    "criteria": {"placeIds": ["x"]}, "scrape_interval": "abc",
                },
                headers={"X-API-Token": "tok"},
            )
            assert resp.status_code == 400
            app.storage.searches.create_search.assert_not_called()

    def test_update_criteria_rejects_invalid_payload(self):
        app = _make_app()
        app.storage.searches.get_search.return_value = {"user_id": 1, "criteria": {}}
        app.storage.users.get_user_by_token.return_value = {"id": 1, "username": "test"}

        with app.test_client() as client:
            resp = client.put(
                "/api/searches/1/criteria",
                json={"criteria": {"priceMax": "not-a-number"}},
                headers={"X-API-Token": "tok"},
            )
            assert resp.status_code == 400
            app.storage.searches.update_search_criteria.assert_not_called()


class TestTokenLeak:
    def test_login_does_not_return_api_token(self):
        app = _make_app()
        app.storage.users.get_user_by_username.return_value = {
            "id": 1, "username": "test", "api_token": "super-secret-token",
        }

        with app.test_client() as client:
            resp = client.post("/api/users/login", json={"username": "test"})
            assert resp.status_code == 200
            data = resp.get_json()
            assert "api_token" not in data


class TestCSRFProtection:
    def _make_protected_app(self):
        app = _make_app()
        csrf = CSRFProtect(app)
        from routes import api_bp
        csrf.exempt(api_bp)
        return app

    def test_web_post_without_token_is_rejected(self):
        app = self._make_protected_app()
        with app.test_client() as client:
            resp = client.post("/login", data={"username": "test"})
            assert resp.status_code == 400

    def test_api_post_without_csrf_token_still_works(self):
        app = self._make_protected_app()
        app.storage.users.create_user.return_value = {
            "id": 1, "username": "test", "api_token": "tok",
        }
        with app.test_client() as client:
            resp = client.post("/api/users", json={"username": "test"})
            assert resp.status_code == 201


class TestAdminExecuteQueryReadOnly:
    def test_read_only_query_succeeds_and_never_commits(self):
        repo = AdminRepository.__new__(AdminRepository)
        conn = MagicMock()
        cur = MagicMock()
        cur.description = None
        cur.rowcount = 0
        conn.cursor.return_value.__enter__.return_value = cur
        repo._get_conn_for_request = lambda: conn
        repo._release_conn = lambda c: None

        rows, row_count, error = repo.execute_query("SELECT 1")

        assert error is None
        executed = [call.args[0] for call in cur.execute.call_args_list]
        assert executed[0] == "SET TRANSACTION READ ONLY"
        assert executed[1] == "SELECT 1"
        conn.commit.assert_not_called()
        assert conn.rollback.call_count >= 1

    def test_write_attempt_is_rejected_by_postgres_and_reported(self):
        repo = AdminRepository.__new__(AdminRepository)
        conn = MagicMock()
        cur = MagicMock()
        cur.execute.side_effect = [None, Exception("cannot execute DELETE in a read-only transaction")]
        conn.cursor.return_value.__enter__.return_value = cur
        repo._get_conn_for_request = lambda: conn
        repo._release_conn = lambda c: None

        rows, row_count, error = repo.execute_query("DELETE FROM users")

        assert error is not None
        assert "read-only" in error
        conn.commit.assert_not_called()


class TestScrapeSubmissionConcurrency:
    def test_concurrent_submit_only_starts_one_job(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        class FakeApp:
            pass

        app = FakeApp()
        app._scrape_futures = {}
        app._scrape_executor = ThreadPoolExecutor(max_workers=4)
        app.storage = MagicMock()
        app.notifier = MagicMock()

        release = threading.Event()
        call_count = {"n": 0}

        class FakeScrapeService:
            def __init__(self, storage, notifier):
                pass

            def execute(self, search_id, user_id):
                call_count["n"] += 1
                release.wait(timeout=2)
                return 0

        results = []
        results_lock = threading.Lock()

        def worker():
            res = submit_scrape(app, 1, 1)
            with results_lock:
                results.append(res)

        with patch("services.scrape_service.ScrapeService", FakeScrapeService):
            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            time.sleep(0.1)
            for t in threads:
                t.join(timeout=2)
            release.set()
            app._scrape_executor.shutdown(wait=True)

        submitted_count = sum(1 for ok, _ in results if ok)
        assert submitted_count == 1
        assert call_count["n"] == 1
