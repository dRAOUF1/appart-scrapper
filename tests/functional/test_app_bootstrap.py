"""Câblage de `main.create_app()` : ce que l'app garantit avant toute route.

Aucun de ces tests n'existait, alors que c'est ici que vivent les décisions les
plus structurantes : le fail-fast sur `SECRET_KEY`, l'absence de toute mutation
sous `/api/*` à exempter du CSRF, l'emprunt/restitution de connexion à chaque
requête, et les filtres de date que tous les templates utilisent.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

FR_TZ = ZoneInfo("Europe/Paris")


class TestStartup:
    def test_health_endpoint_answers_ok(self, client):
        resp = client.get("/health")

        assert resp.status_code == 200
        assert resp.data == b"OK"

    def test_missing_secret_key_fails_fast(self, app_factory, monkeypatch):
        """Sans SECRET_KEY, l'app refuse de démarrer.

        C'est le seul garde-fou contre un déploiement où Flask signerait les
        cookies de session avec une clé vide ou devinable.
        """
        monkeypatch.delenv("SECRET_KEY", raising=False)

        with pytest.raises(RuntimeError, match="SECRET_KEY manquante"):
            app_factory()

    def test_empty_secret_key_also_fails_fast(self, app_factory, monkeypatch):
        """Une chaîne vide est traitée comme une absence (`if not secret_key`)."""
        monkeypatch.setenv("SECRET_KEY", "")

        with pytest.raises(RuntimeError, match="SECRET_KEY manquante"):
            app_factory()

    def test_session_cookie_hardening_is_configured(self, app):
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_storage_and_notifier_are_wired_on_the_app(self, app, storage, notifier):
        assert app.storage is storage
        assert app.notifier is notifier

    @pytest.mark.parametrize(
        ("endpoint", "expected_rule"),
        [
            ("api.search_locations_endpoint", "/api/locations"),
            ("web.dashboard", "/dashboard"),
            ("admin.admin", "/admin"),
        ],
    )
    def test_all_three_blueprints_are_registered(self, app, endpoint, expected_rule):
        """L'API est préfixée `/api`, le web et l'admin sont à la racine."""
        rules = {r.endpoint: str(r) for r in app.url_map.iter_rules()}

        assert rules[endpoint] == expected_rule


class TestCsrfExemption:
    """La protection CSRF ne doit couvrir QUE ce qui en a besoin.

    L'API n'expose plus aucune mutation depuis la suppression du token (#30) :
    sa seule route est un GET d'autocomplete, hors du périmètre CSRF. Le cœur
    du test reste le refus des POST web/admin sans jeton, avec la contre-épreuve
    qui prouve que le refus vient bien du jeton et pas d'une route cassée.
    """

    def test_the_api_surface_has_no_mutation_to_exempt(self, client, storage):
        """Un POST sur l'unique blueprint API ne touche rien : il n'existe
        aucune route mutante sous `/api/*` à protéger ou à exempter."""
        resp = client.post("/api/locations")

        assert resp.status_code == 405
        storage.users.create_user.assert_not_called()

    @pytest.mark.parametrize(
        ("path", "data"),
        [
            ("/login", {"username": "bob"}),
            ("/cleanup", {"days": "4"}),
            ("/searches/1/delete", {}),
            ("/admin/database/truncate", {"table_name": "users"}),
        ],
    )
    def test_web_and_admin_posts_are_rejected_without_csrf_token(self, client, storage, path, data):
        resp = client.post(path, data=data)

        assert resp.status_code == 400
        # Rien n'a été touché : le refus arrive avant la vue.
        assert storage.users.create_user.call_args_list == []
        assert storage.listings.delete_old_listings.call_args_list == []
        assert storage.searches.delete_search.call_args_list == []
        assert storage.admin.truncate_table.call_args_list == []

    def test_web_post_succeeds_with_a_valid_csrf_token(self, client, storage):
        """Contre-épreuve : le refus ci-dessus vient bien du jeton, pas de la route.

        Le jeton est repris du formulaire réel, comme le ferait un navigateur —
        c'est le seul moyen d'avoir un jeton cohérent avec le cookie de session.
        """
        storage.users.create_user.return_value = {"id": 7, "username": "bob"}
        page = client.get("/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

        resp = client.post("/login", data={"username": "bob", "csrf_token": token})

        assert resp.status_code == 302
        storage.users.create_user.assert_called_once_with("bob")


class TestConnectionLifecycle:
    """`before_request` emprunte une connexion, `teardown_request` la rend."""

    @pytest.mark.parametrize("path", ["/health", "/api/locations?q=x", "/login", "/nope-404"])
    def test_every_request_borrows_and_returns_one_connection(self, client, storage, path):
        conn = object()
        storage._get_conn.return_value = conn

        client.get(path)

        assert storage._get_conn.call_count == 1
        storage.release_to_pool.assert_called_once_with(conn)

    def test_teardown_releases_even_when_the_view_raises(self, app, storage):
        """Une vue qui explose ne doit pas fuir la connexion du pool."""
        conn = object()
        storage._get_conn.return_value = conn
        storage.users.get_user_by_id.side_effect = RuntimeError("boom")
        app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "alice"

        resp = client.get("/dashboard")

        assert resp.status_code == 500
        storage.release_to_pool.assert_called_once_with(conn)

    def test_a_failing_release_is_swallowed(self, client, storage):
        """`teardown_request` avale l'erreur de restitution : la réponse passe."""
        storage.release_to_pool.side_effect = RuntimeError("pool cassé")

        resp = client.get("/health")

        assert resp.status_code == 200

    def test_a_failing_borrow_yields_an_opaque_500(self, app, storage):
        """BUG : `before_request` n'a aucune gestion d'erreur.

        Si le pool est épuisé ou la base injoignable, `_get_conn()` lève et
        CHAQUE route — y compris `/health`, sonde de disponibilité qui n'a
        aucun besoin de la base — renvoie un 500 sans message exploitable.
        Une sonde de santé devrait pouvoir répondre sans emprunter de connexion.
        """
        storage._get_conn.side_effect = RuntimeError("pool épuisé")
        app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)

        resp = app.test_client().get("/health")

        assert resp.status_code == 500
        assert "pool épuisé" not in resp.get_data(as_text=True)
        # La connexion n'a jamais été obtenue : rien à rendre.
        storage.release_to_pool.assert_not_called()


class TestJinjaFilters:
    """`parse_iso_date` et `fr_time` : tous les templates les enchaînent."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-07-01T10:30:00", datetime(2026, 7, 1, 10, 30)),
            ("2026-07-01T10:30:00Z", datetime(2026, 7, 1, 10, 30, tzinfo=UTC)),
            ("2026-07-01T10:30:00+02:00", datetime(2026, 7, 1, 10, 30, tzinfo=ZoneInfo("Europe/Paris"))),
        ],
    )
    def test_parse_iso_date_parses_iso_strings(self, app, value, expected):
        parse_iso_date = app.jinja_env.filters["parse_iso_date"]

        assert parse_iso_date(value) == expected

    @pytest.mark.parametrize("value", ["", None, "pas une date", 42, [], "2026-13-45"])
    def test_parse_iso_date_returns_none_on_anything_unusable(self, app, value):
        """Un champ `creation_date` vide ou pourri ne doit pas casser une page."""
        parse_iso_date = app.jinja_env.filters["parse_iso_date"]

        assert parse_iso_date(value) is None

    def test_fr_time_treats_naive_datetimes_as_utc(self, app):
        """Les colonnes TIMESTAMP sont naïves et valent de l'UTC : +2h en été."""
        fr_time = app.jinja_env.filters["fr_time"]

        result = fr_time(datetime(2026, 7, 1, 10, 0))

        assert result.hour == 12
        assert result.tzinfo == FR_TZ

    def test_fr_time_converts_aware_datetimes(self, app):
        fr_time = app.jinja_env.filters["fr_time"]

        result = fr_time(datetime(2026, 1, 15, 23, 0, tzinfo=UTC))

        assert (result.day, result.hour) == (16, 0)

    def test_fr_time_promotes_a_plain_date_to_midnight_paris(self, app):
        """`DATE(found_at)` (activité 7 jours) arrive comme `date`, pas `datetime`."""
        fr_time = app.jinja_env.filters["fr_time"]

        result = fr_time(date(2026, 7, 1))

        assert result == datetime(2026, 7, 1, tzinfo=FR_TZ)

    def test_fr_time_passes_none_through(self, app):
        assert app.jinja_env.filters["fr_time"](None) is None


class TestContextProcessor:
    def test_inject_admin_exposes_admin_username_from_the_environment(self, app):
        with app.test_request_context("/"):
            context = {}
            for processor in app.template_context_processors[None]:
                context.update(processor())

        assert context["admin_username"] == "root-admin"

    def test_inject_admin_falls_back_to_literal_admin(self, app, monkeypatch):
        """Sans ADMIN_USERNAME, le context processor annonce « admin ».

        À rapprocher de `require_admin`, qui lui est FAIL-CLOSED dans le même
        cas : la navigation peut donc proposer l'onglet Admin à un utilisateur
        nommé « admin » à qui toutes les routes seront refusées.
        """
        monkeypatch.delenv("ADMIN_USERNAME", raising=False)

        with app.test_request_context("/"):
            context = {}
            for processor in app.template_context_processors[None]:
                context.update(processor())

        assert context["admin_username"] == "admin"

    def test_inject_admin_exposes_now_and_location_label(self, app):
        from core.criteria import location_label

        with app.test_request_context("/"):
            context = {}
            for processor in app.template_context_processors[None]:
                context.update(processor())

        assert context["location_label"] is location_label
        assert context["now"]().tzinfo == FR_TZ
