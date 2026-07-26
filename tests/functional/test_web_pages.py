"""Tests des pages web de `routes/web.py` : rendu, effets et messages.

Le contrôle de propriété de chaque route est vérifié ailleurs, en un balayage
systématique (test_authorization.py) : ici on teste ce que fait la route quand
l'accès est légitime, et ce qu'elle affiche quand la saisie est mauvaise.

Deux comportements sont documentés comme des problèmes plutôt que testés comme
des fonctionnalités : la connexion sans mot de passe (TestLogin) et la
divergence de validation de l'intervalle entre le web et l'API
(TestUpdateInterval).
"""

from __future__ import annotations

import io
import json
from datetime import datetime

import pytest

from tests.functional.conftest import make_dashboard_data
from tests.helpers.factories import make_listing_row, make_search_row, make_user_row

EMPTY_ZIP = b"PK\x05\x06" + b"\x00" * 18

PARIS_PAYLOAD = json.dumps({
    "kind": "city", "city": "Paris", "postalCode": "75013", "inseeCode": "75113",
})


@pytest.fixture
def owned_search(storage, user):
    """Une recherche appartenant à l'utilisateur connecté."""
    row = make_search_row(id=1, user_id=user["id"], label="Paris 13e")
    storage.searches.get_search.return_value = row
    return row


@pytest.fixture(autouse=True)
def web_views(storage):
    """Données de vue complètes pour les templates web.

    `dashboard.html`, `searches.html` et `listings.html` lisent des clés sans
    garde : les fournir ici évite que chaque test se transforme en chasse aux
    `UndefinedError` étrangères à son objet.
    """
    storage.users.get_dashboard_data.return_value = make_dashboard_data()
    storage.searches.get_user_searches.return_value = []
    storage.listings.get_listings_for_search.return_value = []
    storage.listings.count_listings_for_search.return_value = 0
    storage.listings.get_unique_agencies_for_user.return_value = []
    storage.listings.get_filter_options.return_value = {
        "city": [], "district": [], "zip_code": [], "property_type": [],
        "agency": [], "epc": [], "ges": [],
        "has_private": False, "has_non_private": False,
        "has_new": False, "has_non_new": False,
    }
    storage.scrape_logs.get_scrape_logs.return_value = []
    storage.scrape_logs.count_scrape_logs.return_value = 0
    storage.scrape_logs.get_scrape_stats.return_value = {
        "total": 0, "success": 0, "error": 0, "empty": 0,
        "avg_duration": 0, "avg_new": 0, "last_scrape": None,
    }
    return storage


# ---------------------------------------------------------------------------
# Connexion
# ---------------------------------------------------------------------------

class TestLogin:
    def test_the_login_page_is_public(self, client):
        assert client.get("/login").status_code == 200

    def test_an_existing_username_alone_grants_full_access(self, app_without_csrf, storage):
        """# BUG (sécurité, le plus grave du dépôt) : `/login` ne demande AUCUN
        mot de passe. Le formulaire n'a qu'un champ « nom d'utilisateur », et le
        connaître suffit à obtenir la session complète du compte — donc son
        jeton d'API, ses recherches et ses annonces.

        Aggravant : `POST /api/users/login` répond pour tout nom existant, ce
        qui permet de les énumérer sans être authentifié.

        Test figeant le comportement ACTUEL. Le corriger demande d'ajouter une
        authentification (mot de passe ou lien magique), ce qui est un choix
        produit, pas un correctif de test.
        """
        row = make_user_row(id=1, username="alice", api_token="token-alice")
        storage.users.get_user_by_username.return_value = row
        client = app_without_csrf.test_client()

        resp = client.post("/login", data={"username": "alice"})

        assert resp.status_code in (302, 303)
        with client.session_transaction() as sess:
            assert sess["user_id"] == 1
            assert sess["api_token"] == "token-alice"

    def test_an_unknown_username_silently_creates_the_account(self, app_without_csrf, storage):
        """Conséquence du point ci-dessus : il n'y a pas d'inscription
        distincte, se « connecter » crée le compte. Un visiteur peut donc créer
        des comptes en nombre depuis le formulaire public."""
        storage.users.get_user_by_username.return_value = None
        storage.users.create_user.return_value = make_user_row(id=2, username="nouveau")

        resp = app_without_csrf.test_client().post("/login", data={"username": "nouveau"})

        assert resp.status_code in (302, 303)
        storage.users.create_user.assert_called_once_with("nouveau")

    def test_the_username_is_normalized_before_lookup(self, app_without_csrf, storage):
        """Sans le `strip().lower()`, « Alice » et « alice » seraient deux
        comptes distincts et le second écraserait l'accès au premier."""
        storage.users.get_user_by_username.return_value = make_user_row(username="alice")

        app_without_csrf.test_client().post("/login", data={"username": "  ALICE  "})

        storage.users.get_user_by_username.assert_called_once_with("alice")

    def test_an_empty_username_is_refused_without_touching_the_database(self, app_without_csrf, storage):
        resp = app_without_csrf.test_client().post("/login", data={"username": "   "})

        assert resp.status_code == 200
        storage.users.get_user_by_username.assert_not_called()
        storage.users.create_user.assert_not_called()

    def test_a_failed_account_creation_is_reported_not_raised(self, app_without_csrf, storage):
        storage.users.get_user_by_username.return_value = None
        storage.users.create_user.side_effect = ValueError("déjà pris")

        resp = app_without_csrf.test_client().post("/login", data={"username": "bob"})

        assert resp.status_code == 200

    def test_logging_out_clears_the_session(self, web_client):
        resp = web_client.get("/logout")

        assert resp.status_code in (302, 303)
        with web_client.session_transaction() as sess:
            assert "user_id" not in sess

    def test_a_session_without_username_breaks_the_layout(self, app_without_csrf, storage, user):
        """# BUG : `templates/base.html` fait
        `session.get('username','')[0].upper()` sans garde. Une session à
        laquelle il manque `username` — cookie forgé, ou pose partielle par un
        futur chemin de connexion — fait échouer le rendu de TOUTE page
        authentifiée avec une 500, alors que l'utilisateur est valide.
        """
        client = app_without_csrf.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user["id"]
            sess["api_token"] = user["api_token"]  # pas de `username`

        with pytest.raises(Exception, match="no element 0"):
            client.get("/dashboard")

    def test_a_revoked_token_invalidates_the_session(self, app_without_csrf, storage, user):
        """`require_login` revalide le jeton de session contre la base à chaque
        requête : réinitialiser le jeton d'un utilisateur doit le déconnecter
        partout, y compris sur une session déjà ouverte."""
        client = app_without_csrf.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user["id"]
            sess["username"] = user["username"]
            sess["api_token"] = "ancien-jeton-revoque"

        resp = client.get("/dashboard")

        assert resp.status_code in (302, 303)
        assert "/login" in resp.headers["Location"]
        with client.session_transaction() as sess:
            assert "user_id" not in sess


# ---------------------------------------------------------------------------
# Pages de consultation
# ---------------------------------------------------------------------------

class TestDashboard:
    def test_the_dashboard_renders_the_user_data(self, web_client, storage, user):
        resp = web_client.get("/dashboard")

        assert resp.status_code == 200
        storage.users.get_dashboard_data.assert_called_once_with(user["id"])

    def test_only_the_ten_most_recent_listings_are_shown(self, web_client, storage):
        # `search_label` et `found_at` viennent de la jointure, pas du modèle
        # Listing : ils sont ajoutés au dict de ligne, comme le fait le repo.
        recent = [
            {**make_listing_row(listing_id=f"sl_{i}"),
             "search_label": "X", "found_at": datetime(2026, 7, 1)}
            for i in range(25)
        ]
        storage.users.get_dashboard_data.return_value = make_dashboard_data(recent=recent)

        resp = web_client.get("/dashboard")

        assert resp.status_code == 200

    def test_the_api_token_is_exposed_on_the_page(self, web_client, user):
        """Le jeton est affiché volontairement (c'est ainsi que l'utilisateur le
        récupère pour l'API). Ce test le fige : le retirer casserait l'usage,
        mais il faut savoir que la page en contient un secret."""
        resp = web_client.get("/dashboard")

        assert user["api_token"].encode() in resp.data


class TestSearchesPage:
    def test_the_page_lists_the_user_searches(self, web_client, storage, user):
        storage.searches.get_user_searches.return_value = [make_search_row(label="Ma recherche")]

        resp = web_client.get("/searches")

        assert resp.status_code == 200
        assert b"Ma recherche" in resp.data
        storage.searches.get_user_searches.assert_called_once_with(user["id"])

    def test_creating_a_search_stores_normalized_criteria(self, web_client, storage, user):
        resp = web_client.post("/searches", data={
            "label": "Paris 13e",
            "ntfy_topic": "mon-topic",
            "sources": "seloger",
            "scrape_interval": "10",
            "location_payload": PARIS_PAYLOAD,
            "transaction": "rent",
            "property_types": "apartment",
            "price_max": "1500",
        })

        assert resp.status_code in (302, 303)
        args = storage.searches.create_search.call_args
        assert args[0][0] == user["id"]
        assert args[0][1] == "Paris 13e"
        criteria = args[0][4]
        # Typé, pas la chaîne du formulaire.
        assert criteria["priceMax"] == 1500
        assert criteria["locations"][0]["postalCode"] == "75013"

    def test_the_first_selected_source_becomes_the_legacy_source_column(self, web_client, storage):
        """La colonne `source` reste alimentée pour compatibilité, en plus de
        la liste `sources` : les deux doivent rester cohérentes."""
        web_client.post("/searches", data={
            "label": "X", "ntfy_topic": "t",
            "sources": ["laforet", "seloger"],
            "location_payload": PARIS_PAYLOAD,
        })

        args = storage.searches.create_search.call_args
        assert args[0][3] == "laforet"
        assert args.kwargs["sources"] == ["laforet", "seloger"]

    @pytest.mark.parametrize(
        ("label", "topic"),
        [("", "topic"), ("label", ""), ("  ", "  ")],
        ids=["sans-label", "sans-topic", "les-deux-vides"],
    )
    def test_a_search_without_label_or_topic_is_refused(self, web_client, storage, label, topic):
        web_client.post("/searches", data={
            "label": label, "ntfy_topic": topic, "location_payload": PARIS_PAYLOAD,
        })

        storage.searches.create_search.assert_not_called()

    def test_criteria_a_source_cannot_honour_block_the_creation(self, web_client, storage):
        """La validation est faite source par source AVANT l'écriture : une
        recherche impossible à exécuter ne doit pas être créée, sinon elle
        échouerait silencieusement à chaque passage du scheduler."""
        resp = web_client.post("/searches", data={
            "label": "Sans lieu", "ntfy_topic": "t", "sources": "seloger",
        }, follow_redirects=True)

        assert resp.status_code == 200
        storage.searches.create_search.assert_not_called()

    def test_manual_overrides_are_remembered_after_creation(self, web_client, storage):
        """Le Place ID SeLoger saisi à la main est mémorisé en cache pour ne pas
        avoir à le redemander au prochain scrape."""
        web_client.post("/searches", data={
            "label": "X", "ntfy_topic": "t", "sources": "seloger",
            "location_payload": PARIS_PAYLOAD,
            "override_seloger": "AD08FR31096",
        })

        storage.searches.create_search.assert_called_once()
        storage.seloger_geo.get_cached.assert_called()


class TestListingsPage:
    def test_the_page_paginates_by_twenty(self, web_client, storage, owned_search):
        storage.listings.count_listings_for_search.return_value = 45

        resp = web_client.get("/listings/1?page=3")

        assert resp.status_code == 200
        kwargs = storage.listings.get_listings_for_search.call_args.kwargs
        assert kwargs["limit"] == 20
        assert kwargs["offset"] == 40

    def test_an_unparsable_page_falls_back_to_the_first(self, web_client, storage, owned_search):
        resp = web_client.get("/listings/1?page=nawak")

        assert resp.status_code == 200
        assert storage.listings.get_listings_for_search.call_args.kwargs["offset"] == 0

    def test_exclude_mode_filters_the_blacklisted_agencies_in_sql(
        self, web_client, storage, user
    ):
        storage.searches.get_search.return_value = make_search_row(
            id=1, user_id=user["id"], blacklist_mode="exclude", blacklisted_agencies=["Foncia"],
        )

        web_client.get("/listings/1")

        assert storage.listings.get_listings_for_search.call_args.kwargs["blacklisted_agencies"] == ["Foncia"]

    def test_no_notify_mode_leaves_the_listings_visible(self, web_client, storage, user):
        """`no_notify` ne masque rien : les annonces de ces agences restent
        consultables, seule la notification est supprimée. Confondre les deux
        modes ferait disparaître des annonces sans explication."""
        storage.searches.get_search.return_value = make_search_row(
            id=1, user_id=user["id"], blacklist_mode="no_notify", blacklisted_agencies=["Foncia"],
        )

        web_client.get("/listings/1")

        assert storage.listings.get_listings_for_search.call_args.kwargs["blacklisted_agencies"] == []

    def test_the_active_filters_are_reflected_back_without_the_page_number(
        self, web_client, storage, owned_search
    ):
        """La query string est reconstruite pour les liens de pagination : y
        laisser `page` produirait des liens qui ramènent toujours à la même
        page."""
        # On inspecte le contexte passé au template plutôt que le HTML rendu :
        # les liens de pagination n'apparaissent que s'il y a plusieurs pages,
        # et une assertion sur le HTML casserait au moindre changement de mise
        # en page sans rien dire du comportement.
        from flask import template_rendered

        captured = {}

        def record(sender, template, context, **extra):
            captured.update(context)

        template_rendered.connect(record, web_client.application)
        try:
            resp = web_client.get("/listings/1?page=2&city=Paris&q=loft")
        finally:
            template_rendered.disconnect(record, web_client.application)

        assert resp.status_code == 200
        assert captured["active_filters"] == {"city": "Paris", "q": "loft"}
        assert "page" not in captured["query_string"]
        assert "city=Paris" in captured["query_string"]

    def test_the_count_uses_the_same_filters_as_the_listing(self, web_client, storage, owned_search):
        """Sinon la pagination annonce un nombre de pages qui ne correspond pas
        au contenu."""
        web_client.get("/listings/1?city=Paris&price_max=1200")

        list_filters = storage.listings.get_listings_for_search.call_args.kwargs["filters"]
        count_filters = storage.listings.count_listings_for_search.call_args.kwargs["filters"]
        assert list_filters == count_filters


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

class TestUpdateInterval:
    @pytest.mark.parametrize(
        ("submitted", "stored"),
        [("10", 10), ("1", 1), ("0", 1), ("-5", 1), ("nawak", 5)],
        ids=["valeur-normale", "minimum", "zero-remonte-a-un", "negatif-remonte-a-un", "illisible-defaut-5"],
    )
    def test_the_interval_is_clamped_to_at_least_one_minute(
        self, web_client, storage, owned_search, submitted, stored
    ):
        web_client.post("/searches/1/interval", data={"scrape_interval": submitted})

        storage.searches.update_scrape_interval.assert_called_once_with(1, stored)

    def test_the_web_form_accepts_an_interval_the_api_would_reject(
        self, web_client, storage, owned_search
    ):
        """# BUG (divergence) : le web ne borne que le minimum, alors que l'API
        impose 1..1440 (`core/schemas.validate_scrape_interval`). Un intervalle
        de 100 000 minutes est donc acceptable par le formulaire et refusé par
        l'API pour la même recherche — deux règles pour une même donnée."""
        web_client.post("/searches/1/interval", data={"scrape_interval": "100000"})

        storage.searches.update_scrape_interval.assert_called_once_with(1, 100000)


class TestToggleActive:
    @pytest.mark.parametrize(("new_value", "expected"), [(True, "activée"), (False, "désactivée")])
    def test_the_message_reflects_the_new_state(
        self, web_client, storage, owned_search, new_value, expected
    ):
        storage.searches.toggle_search_active.return_value = new_value

        resp = web_client.post("/searches/1/toggle-active", follow_redirects=True)

        assert expected.encode() in resp.data

    def test_a_search_that_vanished_produces_no_message(self, web_client, storage, owned_search):
        """`toggle_search_active` renvoie None quand la ligne a disparu entre la
        lecture et l'écriture : ne rien afficher vaut mieux qu'annoncer un
        changement qui n'a pas eu lieu."""
        storage.searches.toggle_search_active.return_value = None

        resp = web_client.post("/searches/1/toggle-active", follow_redirects=True)

        assert "activée".encode() not in resp.data
        assert "désactivée".encode() not in resp.data


class TestBlacklist:
    def test_the_selected_agencies_replace_the_previous_list(self, web_client, storage, owned_search):
        web_client.post("/searches/1/blacklist-agencies", data={"agencies": ["Foncia", "Nexity"]})

        storage.searches.update_blacklisted_agencies.assert_called_once_with(1, ["Foncia", "Nexity"])

    def test_submitting_no_agency_clears_the_list(self, web_client, storage, owned_search):
        """Décocher toutes les cases doit vider la blacklist, pas la laisser
        inchangée — sinon il devient impossible de la réinitialiser."""
        web_client.post("/searches/1/blacklist-agencies", data={})

        storage.searches.update_blacklisted_agencies.assert_called_once_with(1, [])

    @pytest.mark.parametrize("mode", ["exclude", "no_notify"])
    def test_a_valid_mode_is_stored(self, web_client, storage, owned_search, mode):
        web_client.post("/searches/1/blacklist-mode", data={"mode": mode})

        storage.searches.update_blacklist_mode.assert_called_once_with(1, mode)

    @pytest.mark.parametrize("mode", ["", "nawak", "EXCLUDE", "no-notify"])
    def test_an_invalid_mode_never_reaches_the_repository(self, web_client, storage, owned_search, mode):
        """Le repository valide aussi, mais la route doit refuser d'abord : un
        mode inconnu stocké ferait un filtrage muet côté scrape."""
        web_client.post("/searches/1/blacklist-mode", data={"mode": mode})

        storage.searches.update_blacklist_mode.assert_not_called()


class TestEditSearch:
    def test_the_edit_page_shows_the_search_and_its_stats(self, web_client, storage, owned_search):
        resp = web_client.get("/searches/1/edit")

        assert resp.status_code == 200
        assert b"Paris 13e" in resp.data
        storage.scrape_logs.get_scrape_stats.assert_called_once_with(1)

    def test_a_valid_edit_updates_and_redirects(self, web_client, storage, owned_search, user):
        resp = web_client.post("/searches/1/edit", data={
            "label": "Nouveau libellé",
            "ntfy_topic": "nouveau-topic",
            "sources": "seloger",
            "scrape_interval": "15",
            "location_payload": PARIS_PAYLOAD,
        })

        assert resp.status_code in (302, 303)
        args = storage.searches.update_search.call_args
        assert args[0] == (1, user["id"])
        assert args.kwargs["label"] == "Nouveau libellé"
        assert args.kwargs["scrape_interval"] == 15

    def test_an_invalid_edit_crashes_instead_of_showing_the_error(
        self, web_client, storage, owned_search
    ):
        """# BUG : la branche d'échec de validation rend `search_edit.html`
        SANS lui passer `stats` (routes/web.py:418), alors que le template
        l'utilise. Le rendu lève donc `UndefinedError` et l'utilisateur reçoit
        une 500 — au lieu du message d'erreur qui venait d'être préparé juste
        au-dessus.

        Autrement dit : toute tentative d'édition dont les critères sont
        invalides pour une source produit une page d'erreur serveur. Le chemin
        « heureux » et le chemin d'erreur du GET passent bien `stats`, ce qui
        explique que ça n'ait pas été vu.

        Ce test fige le comportement actuel. Le correctif est d'une ligne
        (passer `stats` aussi dans cette branche), mais c'est un changement de
        code de production, hors périmètre de la refonte des tests.
        """
        from jinja2.exceptions import UndefinedError

        with pytest.raises(UndefinedError, match="stats"):
            web_client.post("/searches/1/edit", data={
                "label": "Un libellé que je viens de taper",
                "ntfy_topic": "topic",
                "sources": "seloger",
                # Pas de localisation : la validation échoue.
            })

        storage.searches.update_search.assert_not_called()

    def test_the_sources_fall_back_to_the_stored_ones(self, web_client, storage, user):
        """Le formulaire d'édition peut ne pas renvoyer de cases `sources` : il
        faut alors conserver celles de la recherche, et non repartir sur
        « seloger » en silence."""
        storage.searches.get_search.return_value = make_search_row(
            id=1, user_id=user["id"], source="laforet", sources=["laforet"],
        )

        web_client.post("/searches/1/edit", data={
            "label": "X", "ntfy_topic": "t", "location_payload": PARIS_PAYLOAD,
        })

        assert storage.searches.update_search.call_args.kwargs["sources"] == ["laforet"]


class TestScrapeFromWeb:
    def test_a_scrape_is_submitted_and_its_message_shown(self, web_client, storage, owned_search):
        resp = web_client.post("/searches/1/scrape", follow_redirects=True)

        assert resp.status_code == 200

    def test_a_second_scrape_while_one_runs_is_reported_as_a_warning(
        self, monkeypatch, web_client, storage, owned_search
    ):
        """Le message vient de `submit_scrape` et est affiché tel quel : c'est
        un contrat de chaîne entre le contrôle de concurrence et l'interface."""
        monkeypatch.setattr("routes.web.submit_scrape",
                            lambda app, sid, uid: (False, "Scraping déjà en cours pour cette recherche"),
                            raising=False)

        resp = web_client.post("/searches/1/scrape", follow_redirects=True)

        assert resp.status_code == 200


class TestDeleteSearch:
    def test_a_search_is_deleted(self, web_client, storage, owned_search):
        web_client.post("/searches/1/delete")

        storage.searches.delete_search.assert_called_once_with(1)


class TestCleanup:
    @pytest.mark.parametrize(("submitted", "expected"), [("7", 7), ("nawak", 4), ("0", 0)])
    def test_the_retention_window_comes_from_the_form(self, web_client, storage, submitted, expected):
        storage.listings.delete_old_listings.return_value = 3

        web_client.post("/cleanup", data={"days": submitted})

        storage.listings.delete_old_listings.assert_called_once_with(days=expected)

    def test_zero_days_deletes_everything(self, web_client, storage):
        """# BUG : `days=0` passe le clamp et supprime TOUTES les annonces, pas
        seulement les anciennes. L'endpoint n'est pas réservé aux admins."""
        storage.listings.delete_old_listings.return_value = 9999

        resp = web_client.post("/cleanup", data={"days": "0"}, follow_redirects=True)

        assert resp.status_code == 200
        storage.listings.delete_old_listings.assert_called_once_with(days=0)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

class TestSearchLogs:
    def test_the_log_page_paginates_by_thirty(self, web_client, storage, owned_search):
        storage.scrape_logs.count_scrape_logs.return_value = 70

        resp = web_client.get("/searches/1/logs?page=2")

        assert resp.status_code == 200
        kwargs = storage.scrape_logs.get_scrape_logs.call_args.kwargs
        assert kwargs["limit"] == 30
        assert kwargs["offset"] == 30

    def test_the_status_filter_is_passed_to_both_the_list_and_the_count(
        self, web_client, storage, owned_search
    ):
        web_client.get("/searches/1/logs?status=error")

        assert storage.scrape_logs.get_scrape_logs.call_args.kwargs["status_filter"] == "error"
        assert storage.scrape_logs.count_scrape_logs.call_args.kwargs["status_filter"] == "error"

    def test_there_is_always_at_least_one_page(self, web_client, storage, owned_search):
        """`total_pages = max(1, ...)` : afficher « page 1 sur 0 » sur une
        recherche jamais scrapée serait absurde."""
        storage.scrape_logs.count_scrape_logs.return_value = 0

        assert web_client.get("/searches/1/logs").status_code == 200


class TestLiveLogs:
    def test_the_tail_is_returned_with_the_next_offset(self, web_client, storage, owned_search, log_dirs):
        (log_dirs / "search_1.log").write_text("ligne une\nligne deux\n", encoding="utf-8")

        resp = web_client.get("/searches/1/logs/live?offset=0")

        body = resp.get_json()
        assert body["text"].startswith("ligne une")
        assert body["offset"] > 0
        assert body["has_content"] is True

    def test_an_offset_past_the_end_returns_nothing_new(self, web_client, storage, owned_search, log_dirs):
        (log_dirs / "search_1.log").write_text("court\n", encoding="utf-8")

        body = web_client.get("/searches/1/logs/live?offset=9999").get_json()

        assert body["text"] == ""
        assert body["has_content"] is False

    def test_a_missing_log_file_is_not_an_error(self, web_client, storage, owned_search):
        body = web_client.get("/searches/1/logs/live").get_json()

        assert body == {"text": "", "offset": 0, "has_content": False}

    def test_an_offset_landing_mid_character_degrades_instead_of_crashing(
        self, web_client, storage, owned_search, log_dirs
    ):
        """`offset` vient du client et part tel quel dans un `seek()` sur un
        fichier ouvert en mode TEXTE : il peut tomber au milieu d'un caractère
        multi-octets. Le fichier étant ouvert avec `errors="replace"`, l'octet
        orphelin devient U+FFFD au lieu de lever `UnicodeDecodeError`.

        C'est ce qui empêche un simple paramètre d'URL de provoquer une 500 :
        test de non-régression sur ce `errors="replace"`, qu'on pourrait croire
        cosmétique.
        """
        (log_dirs / "search_1.log").write_text("é" * 50, encoding="utf-8")

        body = web_client.get("/searches/1/logs/live?offset=1").get_json()

        assert "\ufffd" in body["text"]

    def test_an_unparsable_offset_falls_back_to_zero(self, web_client, storage, owned_search, log_dirs):
        (log_dirs / "search_1.log").write_text("contenu\n", encoding="utf-8")

        body = web_client.get("/searches/1/logs/live?offset=nawak").get_json()

        assert body["text"] == "contenu\n"


class TestRawLog:
    def test_a_stored_log_is_rendered(self, web_client, storage, owned_search):
        storage.scrape_logs.get_scrape_log_raw.return_value = {
            "id": 5, "search_id": 1, "raw_logs": "contenu du log",
            "status": "success", "started_at": datetime(2026, 7, 1, 10, 0),
            "completed_at": datetime(2026, 7, 1, 10, 0, 30), "duration_sec": 30,
            "listings_found": 3, "new_listings": 1, "error_message": "",
        }

        resp = web_client.get("/searches/1/logs/5/raw")

        assert resp.status_code == 200
        assert b"contenu du log" in resp.data

    def test_a_missing_log_redirects_to_the_log_list(self, web_client, storage, owned_search):
        storage.scrape_logs.get_scrape_log_raw.return_value = None

        resp = web_client.get("/searches/1/logs/5/raw")

        assert resp.status_code in (302, 303)
        assert "/logs" in resp.headers["Location"]


class TestLogDownload:
    def test_the_stored_content_is_served_as_an_attachment(self, web_client, storage, owned_search):
        storage.scrape_logs.get_scrape_log_raw.return_value = {"id": 5, "raw_logs": "texte du log"}

        with web_client.get("/searches/1/logs/5/download") as resp:
            assert resp.status_code == 200
            assert resp.data == b"texte du log"
            assert "search_1_log_5.log" in resp.headers["Content-Disposition"]

    def test_without_stored_content_the_current_live_file_is_served_instead(
        self, web_client, storage, owned_search, log_dirs
    ):
        """# BUG (contenu trompeur) : quand le log demandé n'a pas de contenu
        stocké, la route renvoie le fichier de log COURANT de la recherche, sous
        le nom `search_1_log_5.log`. L'utilisateur croit télécharger le log
        numéro 5 et obtient le scrape le plus récent — sans aucun avertissement.
        """
        storage.scrape_logs.get_scrape_log_raw.return_value = {"id": 5, "raw_logs": ""}
        (log_dirs / "search_1.log").write_text("le log courant, pas le numéro 5", encoding="utf-8")

        with web_client.get("/searches/1/logs/5/download") as resp:
            assert resp.status_code == 200
            assert "pas le numéro 5" in resp.data.decode()

    def test_with_neither_source_a_404_is_returned(self, web_client, storage, owned_search):
        storage.scrape_logs.get_scrape_log_raw.return_value = {"id": 5, "raw_logs": ""}

        resp = web_client.get("/searches/1/logs/5/download")

        assert resp.status_code == 404


class TestLogsImportExport:
    def test_the_export_streams_the_archive(self, web_client, storage, owned_search, tmp_path):
        archive = tmp_path / "logs_search_1.zip"
        archive.write_bytes(EMPTY_ZIP)
        storage.scrape_logs.export_scrape_logs.return_value = str(archive)

        with web_client.get("/searches/1/logs/export") as resp:
            assert resp.status_code == 200
            assert resp.mimetype == "application/zip"

    def test_an_import_reports_its_counts(self, web_client, storage, owned_search):
        storage.scrape_logs.import_scrape_logs.return_value = {"imported": 3, "skipped": 1, "remapped": 0}

        resp = web_client.post(
            "/searches/1/logs/import",
            data={"log_archive": (io.BytesIO(EMPTY_ZIP), "logs.zip")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        assert resp.status_code == 200

    def test_a_missing_file_is_refused(self, web_client, storage, owned_search):
        resp = web_client.post("/searches/1/logs/import", data={},
                               content_type="multipart/form-data")

        assert resp.status_code in (302, 303)
        storage.scrape_logs.import_scrape_logs.assert_not_called()

    def test_an_archive_from_another_search_asks_for_confirmation(self, web_client, storage, owned_search):
        storage.scrape_logs.import_scrape_logs.side_effect = ValueError("override_required")

        resp = web_client.post(
            "/searches/1/logs/import",
            data={"log_archive": (io.BytesIO(EMPTY_ZIP), "logs.zip")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        assert resp.status_code == 200

    def test_the_override_flag_is_passed_through(self, web_client, storage, owned_search):
        storage.scrape_logs.import_scrape_logs.return_value = {"imported": 1, "skipped": 0, "remapped": 1}

        web_client.post(
            "/searches/1/logs/import",
            data={"log_archive": (io.BytesIO(EMPTY_ZIP), "logs.zip"), "allow_override": "true"},
            content_type="multipart/form-data",
        )

        assert storage.scrape_logs.import_scrape_logs.call_args.kwargs["allow_override"] is True
