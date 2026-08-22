"""`routes/api.py` — comptes, sources, localisations, annonces, stats, logs.

Le cycle de vie des recherches est dans test_api_searches.py.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl

import pytest
from werkzeug.datastructures import FileStorage, MultiDict

from tests.functional.conftest import make_filter_options, make_user_stats, make_view_listing
from tests.helpers.factories import make_search_row


def _zip_bytes(name: str = "logs.jsonl", content: bytes = b"{}\n") -> bytes:
    """Une archive zip minimale mais valide, pour les tests d'upload."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, content)
    return buf.getvalue()


class TestCreateUser:
    def test_registration_is_open_to_anyone_without_authentication(self, client, storage):
        """FAILLE : `POST /api/users` n'est pas protégé.

        Aucun jeton, aucun secret d'inscription, aucun rate limit : n'importe qui
        peut créer autant de comptes qu'il veut, et chaque compte créé reçoit un
        token API valide. Combiné à `/api/users/login` (énumération) et à
        `/login` web (connexion sans mot de passe), c'est la surface d'entrée du
        dépôt.
        """
        storage.users.create_user.return_value = {"id": 7, "username": "bob", "api_token": "tok-bob"}

        resp = client.post("/api/users", json={"username": "bob"})

        assert resp.status_code == 201
        assert resp.get_json()["api_token"] == "tok-bob"
        storage.users.create_user.assert_called_once_with("bob")

    @pytest.mark.parametrize(
        ("submitted", "stored"),
        [
            ("  Bob  ", "bob"),
            ("ALICE", "alice"),
            ("Éva", "éva"),
        ],
    )
    def test_username_is_stripped_and_lowercased(self, client, storage, submitted, stored):
        """La normalisation est le seul garant de l'unicité des comptes."""
        storage.users.create_user.return_value = {"id": 1, "username": stored, "api_token": "t"}

        client.post("/api/users", json={"username": submitted})

        storage.users.create_user.assert_called_once_with(stored)

    @pytest.mark.parametrize("payload", [{}, {"username": ""}, {"username": "   "}, {"username": None}])
    def test_a_blank_username_is_rejected(self, client, storage, payload):
        if payload.get("username") is None and "username" in payload:
            pytest.skip("null déclenche un AttributeError, couvert plus bas")

        resp = client.post("/api/users", json=payload)

        assert resp.status_code == 400
        assert resp.get_json()["error"] == "username requis"
        storage.users.create_user.assert_not_called()

    def test_a_duplicate_username_yields_409(self, client, storage):
        storage.users.create_user.side_effect = ValueError("Username déjà pris")

        resp = client.post("/api/users", json={"username": "bob"})

        assert resp.status_code == 409
        assert resp.get_json()["error"] == "Username déjà pris"

    def test_a_null_username_crashes_with_a_500(self, app, storage):
        """BUG : `data.get("username", "")` renvoie None si la clé vaut `null`.

        Le défaut de `.get()` ne s'applique qu'à une clé ABSENTE : un
        `{"username": null}` explicite atteint `.strip()` sur None et produit un
        500 au lieu du 400 « username requis ».
        """
        app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)

        resp = app.test_client().post("/api/users", json={"username": None})

        assert resp.status_code == 500
        storage.users.create_user.assert_not_called()


class TestLoginUser:
    def test_it_is_an_enumeration_oracle(self, client, storage, user):
        """FAILLE : `POST /api/users/login` distingue « existe » de « n'existe pas ».

        Aucune authentification n'est demandée et la réponse diffère (200 vs 404)
        selon l'existence du compte. Un attaquant énumère donc les usernames,
        puis se connecte à n'importe lequel via `/login` web, qui ne demande pas
        de mot de passe. La chaîne complète est une prise de contrôle de compte à
        partir du seul nom d'utilisateur.
        """
        existing = client.post("/api/users/login", json={"username": "alice"})
        missing = client.post("/api/users/login", json={"username": "inexistant"})

        assert existing.status_code == 200
        assert existing.get_json() == {"id": user["id"], "username": "alice"}
        assert missing.status_code == 404
        assert missing.get_json() == {"error": "Utilisateur introuvable"}

    def test_the_api_token_is_never_returned(self, client, storage, user):
        """Le seul point correct de cet endpoint : la réponse est une liste blanche.

        Renvoyer la ligne complète livrerait le token API — donc l'API entière —
        à quiconque connaît un username.
        """
        body = client.post("/api/users/login", json={"username": "alice"}).get_json()

        assert set(body) == {"id", "username"}
        assert user["api_token"] not in str(body)

    @pytest.mark.parametrize("submitted", ["  ALICE  ", "Alice", "alice"])
    def test_lookup_is_normalized_like_registration(self, client, storage, user, submitted):
        resp = client.post("/api/users/login", json={"username": submitted})

        assert resp.status_code == 200
        storage.users.get_user_by_username.assert_called_with("alice")

    @pytest.mark.parametrize("payload", [{}, {"username": "  "}])
    def test_a_blank_username_is_rejected_before_any_query(self, client, storage, payload):
        resp = client.post("/api/users/login", json=payload)

        assert resp.status_code == 400
        storage.users.get_user_by_username.assert_not_called()


class TestSources:
    def test_lists_the_registered_sources_publicly(self, client):
        resp = client.get("/api/sources")

        assert resp.status_code == 200
        by_id = {s["id"]: s for s in resp.get_json()}
        assert {"seloger", "laforet"} <= set(by_id)

    def test_each_source_declares_its_capabilities(self, client):
        """Le front s'en sert pour prévenir qu'une case cochée ne sera pas honorée."""
        by_id = {s["id"]: s for s in client.get("/api/sources").get_json()}

        laforet = by_id["laforet"]
        assert set(laforet) == {
            "id", "name", "description", "supported_transactions",
            "supported_property_types", "manual_override_label",
            "manual_override_help", "url_note",
        }
        # Laforêt ne référence ni parking ni terrain : c'est déclaré, pas deviné.
        assert "parking" not in laforet["supported_property_types"]

    def test_it_needs_no_authentication(self, client, storage):
        client.get("/api/sources")

        storage.users.get_user_by_token.assert_not_called()


class TestLocations:
    @pytest.fixture
    def recorded_search(self, monkeypatch):
        """Intercepte `core.geocode.search_locations` et note ses arguments."""
        import core.geocode

        calls = []

        def fake(query, limit=20):
            calls.append((query, limit))
            return [{"kind": "city", "city": "Poitiers", "postalCode": "86000", "label": "Poitiers (86000)"}]

        monkeypatch.setattr(core.geocode, "search_locations", fake)
        return calls

    def test_forwards_the_query_and_returns_suggestions(self, client, recorded_search):
        resp = client.get("/api/locations?q=poitiers")

        assert resp.status_code == 200
        assert resp.get_json()[0]["city"] == "Poitiers"
        assert recorded_search == [("poitiers", 10)]

    @pytest.mark.parametrize(
        ("submitted", "effective"),
        [
            ("1", 1),
            ("5", 5),
            ("20", 20),
            ("21", 20),
            ("9999", 20),
            ("0", 1),
            ("-3", 1),
            ("abc", 10),
            ("", 10),
        ],
    )
    def test_limit_is_clamped_between_1_and_20(self, client, recorded_search, submitted, effective):
        """`max(1, min(limit, 20))` : une limite absurde ne fait pas exploser
        le nombre d'appels sortants vers geo.api.gouv.fr."""
        client.get(f"/api/locations?limit={submitted}&q=poitiers")

        assert recorded_search[0][1] == effective

    def test_a_short_query_is_answered_without_any_outbound_call(self, client, monkeypatch):
        """`search_locations` coupe court sous 2 caractères."""
        import core.geocode

        queried = []
        monkeypatch.setattr(core.geocode, "_query", lambda url, params: queried.append(url) or [])

        resp = client.get("/api/locations?q=p")

        assert resp.get_json() == []
        assert queried == []

    def test_it_is_an_unauthenticated_amplifier(self, client, storage, monkeypatch):
        """FAILLE (DoS/abus) : endpoint public, 3 appels HTTP sortants, aucun cache.

        Chaque requête anonyme déclenche un appel vers `/regions`,
        `/departements` et `/communes` de geo.api.gouv.fr. Aucun cache ne
        mémoïse les résultats de l'autocomplete (les caches de `core.geocode` ne
        portent que sur la résolution INSEE), et aucun rate limit n'existe : une
        boucle sur `?q=` amplifie x3 vers un service tiers et sature les threads
        du serveur, sans compte ni jeton.
        """
        import core.geocode

        queried = []

        def fake_query(url, params):
            queried.append(url)
            return []

        monkeypatch.setattr(core.geocode, "_query", fake_query)

        client.get("/api/locations?q=poitiers")

        assert len(queried) == 3
        storage.users.get_user_by_token.assert_not_called()

    def test_outbound_failures_degrade_to_an_empty_list(self, client):
        """Le réseau est coupé par le socle de test : la route répond quand même.

        Comportement voulu — l'autocomplete ne doit pas rendre le formulaire
        inutilisable si geo.api.gouv.fr est indisponible.
        """
        resp = client.get("/api/locations?q=poitiers")

        assert resp.status_code == 200
        assert resp.get_json() == []


class TestGetListings:
    def test_returns_the_search_its_total_and_its_listings(self, api_client, storage, owned_search):
        listings = [make_view_listing(found_at=None) for _ in range(3)]
        storage.listings.get_listings_for_search.return_value = listings
        storage.listings.count_listings_for_search.return_value = 57

        resp = api_client.get("/api/listings/1")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["total"] == 57
        assert len(body["listings"]) == 3
        assert body["search"]["id"] == 1
        assert (body["limit"], body["offset"]) == (50, 0)

    @pytest.mark.parametrize(
        ("submitted", "effective"),
        [("10", 10), ("200", 200), ("201", 200), ("100000", 200), ("abc", 50), ("", 50)],
    )
    def test_limit_is_capped_at_200(self, api_client, storage, owned_search, submitted, effective):
        api_client.get(f"/api/listings/1?limit={submitted}")

        assert storage.listings.get_listings_for_search.call_args[1]["limit"] == effective

    @pytest.mark.parametrize("submitted", ["0", "100", "999999999"])
    def test_offset_is_not_capped_at_all(self, api_client, storage, owned_search, submitted):
        """BUG : aucune borne sur `offset`, contrairement à `limit`.

        Un `OFFSET 999999999` fait parcourir toute la table à Postgres pour
        renvoyer zéro ligne. C'est un déni de service à un seul paramètre, sur un
        endpoint authentifié mais accessible à tout compte (et l'inscription est
        ouverte).
        """
        api_client.get(f"/api/listings/1?offset={submitted}")

        assert storage.listings.get_listings_for_search.call_args[1]["offset"] == int(submitted)

    @pytest.mark.parametrize("submitted", ["-10", "abc"])
    def test_a_negative_or_invalid_offset_falls_back_or_passes_through(
        self, api_client, storage, owned_search, submitted
    ):
        api_client.get(f"/api/listings/1?offset={submitted}")

        expected = 0 if submitted == "abc" else -10
        assert storage.listings.get_listings_for_search.call_args[1]["offset"] == expected

    def test_exclude_mode_filters_blacklisted_agencies_in_sql(self, api_client, storage, user):
        storage.searches.get_search.return_value = make_search_row(
            id=1, user_id=user["id"], blacklist_mode="exclude", blacklisted_agencies=["Foncia"],
        )

        api_client.get("/api/listings/1")

        assert storage.listings.get_listings_for_search.call_args[1]["blacklisted_agencies"] == ["Foncia"]
        assert storage.listings.count_listings_for_search.call_args[1]["blacklisted_agencies"] == ["Foncia"]

    def test_no_notify_mode_returns_blacklisted_listings(self, api_client, storage, user):
        """`no_notify` ne filtre rien en SQL : la blacklist n'agit qu'aux notifications.

        C'est la sémantique voulue, et c'est précisément ce qui distingue les deux
        modes — la liste reste complète, seules les alertes sont muettes.
        """
        storage.searches.get_search.return_value = make_search_row(
            id=1, user_id=user["id"], blacklist_mode="no_notify", blacklisted_agencies=["Foncia"],
        )

        api_client.get("/api/listings/1")

        assert storage.listings.get_listings_for_search.call_args[1]["blacklisted_agencies"] == []

    def test_an_empty_blacklist_filters_nothing(self, api_client, storage, user):
        storage.searches.get_search.return_value = make_search_row(
            id=1, user_id=user["id"], blacklist_mode="exclude", blacklisted_agencies=[],
        )

        api_client.get("/api/listings/1")

        assert storage.listings.get_listings_for_search.call_args[1]["blacklisted_agencies"] == []

    @pytest.mark.parametrize("sort", ["price_asc", "found_at_desc", "n'importe quoi", "id; DROP TABLE"])
    def test_sort_is_forwarded_raw_to_the_repository(self, api_client, storage, owned_search, sort):
        """La route ne valide pas `sort` : c'est `_build_order_clause` qui doit
        retomber sur un tri connu (la validation vit au niveau du repository, cf.
        ses tests unitaires) — mais rien ici ne le garantit."""
        api_client.get("/api/listings/1", query_string={"sort": sort})

        assert storage.listings.get_listings_for_search.call_args[1]["sort"] == sort

    def test_filters_are_forwarded_to_both_queries(self, api_client, storage, owned_search):
        """La liste et le total doivent voir les MÊMES filtres, sinon la
        pagination affiche un nombre de pages faux."""
        api_client.get("/api/listings/1?q=loft&price_max=900&city=Paris&is_new=true")

        expected = {"q": "loft", "price_max": 900.0, "city": "Paris", "is_new": True}
        assert storage.listings.get_listings_for_search.call_args[1]["filters"] == expected
        assert storage.listings.count_listings_for_search.call_args[1]["filters"] == expected


class TestListingFiltersParity:
    """`_parse_listing_filters` est DUPLIQUÉ entre api.py et web.py.

    Deux implémentations différentes (une boucle contre une suite de `if`) pour
    un même contrat : une évolution appliquée d'un seul côté ferait divergier
    silencieusement la liste web de la liste API. Ce test verrouille l'égalité —
    il échouera dès qu'une des deux copies changera seule.
    """

    QUERY_STRINGS = [
        "",
        "q=loft",
        "q=%20%20%20",
        "price_min=100&price_max=2000",
        "price_min=abc&price_max=",
        "price_min=1e3",
        "price_min=-50",
        "surface_min=20&surface_max=90",
        "rooms_min=2&rooms_max=4",
        "rooms_min=2.5",
        "city=Paris&district=13e&zip_code=75013&property_type=apartment&agency=Foncia&epc=C&ges=B",
        "city=+Paris+",
        "is_private=true&is_new=false",
        "is_private=TRUE",
        "is_private=1",
        "is_private=",
        "date_min=2026-01-01",
        "date_min=pas-une-date",
        "sort=price_asc&page=3&limit=10",
        "q=loft&price_min=100&city=Paris&is_private=true&date_min=2026-01-01",
    ]

    @pytest.mark.parametrize("query_string", QUERY_STRINGS)
    def test_both_implementations_agree(self, query_string):
        from routes.api import _parse_listing_filters as api_parse
        from routes.web import _parse_listing_filters as web_parse

        args = MultiDict(parse_qsl(query_string, keep_blank_values=True))

        api_result = api_parse(args)
        web_result = web_parse(args)

        assert api_result == web_result
        # Même ordre d'insertion : les deux produisent des dicts interchangeables
        # jusque dans la construction du WHERE.
        assert list(api_result) == list(web_result)

    @pytest.mark.parametrize("value", ["inf", "-inf", "nan"])
    def test_both_accept_non_finite_floats(self, value):
        """BUG partagé : `float()` accepte « inf » et « nan ».

        Les deux copies laissent donc passer un `price_min=inf` jusqu'au
        paramètre SQL. Postgres refuse un `numeric` non fini : la page d'annonces
        rend alors une 500 à partir d'une simple query string.
        """
        from routes.api import _parse_listing_filters as api_parse
        from routes.web import _parse_listing_filters as web_parse

        args = MultiDict([("price_min", value)])

        assert "price_min" in api_parse(args)
        assert "price_min" in web_parse(args)


class TestStats:
    def test_returns_the_callers_own_stats(self, api_client, storage, user):
        storage.users.get_user_stats.return_value = make_user_stats(total_listings=99)

        resp = api_client.get("/api/stats")

        assert resp.status_code == 200
        assert resp.get_json()["total_listings"] == 99
        storage.users.get_user_stats.assert_called_once_with(user["id"])


class TestCleanup:
    @pytest.mark.parametrize(
        ("submitted", "effective"),
        [("7", 7), ("", 4), ("abc", 4), ("-1", -1)],
    )
    def test_days_is_read_from_the_query_string(self, api_client, storage, submitted, effective):
        storage.listings.delete_old_listings.return_value = 3

        resp = api_client.post(f"/api/cleanup?days={submitted}")

        assert resp.status_code == 200
        assert resp.get_json() == {"deleted": 3, "days": effective}
        storage.listings.delete_old_listings.assert_called_once_with(days=effective)

    def test_the_default_is_four_days(self, api_client, storage):
        storage.listings.delete_old_listings.return_value = 0

        api_client.post("/api/cleanup")

        storage.listings.delete_old_listings.assert_called_once_with(days=4)

    def test_days_zero_deletes_every_listing_of_every_user(self, api_client, storage):
        """FAILLE : suppression globale par n'importe quel compte authentifié.

        Trois défauts cumulés :
          1. `days` n'est pas borné — `days=0` supprime TOUT (`found_at < now`),
             et une valeur négative supprimerait même le futur ;
          2. `delete_old_listings` est GLOBAL, pas limité à `g.user` : les
             annonces de tous les utilisateurs partent ;
          3. l'endpoint n'exige que `require_token`, pas `require_admin`, et
             l'inscription est ouverte à tous.
        Un seul `POST /api/cleanup?days=0` vide donc la base de production.
        """
        storage.listings.delete_old_listings.return_value = 12345

        resp = api_client.post("/api/cleanup?days=0")

        assert resp.status_code == 200
        assert resp.get_json()["deleted"] == 12345
        storage.listings.delete_old_listings.assert_called_once_with(days=0)


class TestExportLogs:
    def test_streams_the_archive_produced_by_the_repository(
        self, api_client, storage, owned_search, tmp_path
    ):
        archive = tmp_path / "logs_search_1.zip"
        archive.write_bytes(_zip_bytes())
        storage.scrape_logs.export_scrape_logs.return_value = str(archive)

        # `send_file` renvoie un flux adossé au fichier : le client de test ne le
        # referme pas tout seul, et le ResourceWarning qui en découle est une
        # erreur ici (filterwarnings = error). En production, c'est le serveur
        # WSGI qui ferme la réponse.
        with api_client.get("/api/searches/1/logs/export") as resp:
            assert resp.status_code == 200
            assert resp.mimetype == "application/zip"
            assert "logs_search_1.zip" in resp.headers["Content-Disposition"]
            assert resp.data == archive.read_bytes()
        storage.scrape_logs.export_scrape_logs.assert_called_once_with(1)


class TestImportLogs:
    def test_imports_an_archive_and_returns_the_repository_result(
        self, api_client, storage, owned_search, user
    ):
        storage.scrape_logs.import_scrape_logs.return_value = {"imported": 4, "skipped": 1}

        resp = api_client.post(
            "/api/searches/1/logs/import",
            data={"log_archive": (io.BytesIO(_zip_bytes()), "logs.zip")},
            content_type="multipart/form-data",
        )

        assert resp.status_code == 200
        assert resp.get_json() == {"imported": 4, "skipped": 1}
        kwargs = storage.scrape_logs.import_scrape_logs.call_args[1]
        assert kwargs["allow_override"] is False
        assert kwargs["performed_by"] == user["username"]

    def test_the_temporary_path_is_predictable_and_world_writable(
        self, api_client, storage, owned_search
    ):
        """FAILLE : archive écrite dans `/tmp` sous un nom devinable.

        Le chemin est `/tmp/logs_import_{search_id}_{timestamp_seconde}.zip` :
        `search_id` est connu et l'horodatage est à la seconde. Sur une machine
        multi-utilisateurs, un attaquant local peut pré-créer ce chemin en lien
        symbolique et faire écrire l'upload ailleurs (`upload.save()` suit les
        liens), ou lire l'archive d'un autre utilisateur avant sa suppression.
        `tempfile.mkstemp` existe exactement pour ça.
        """
        seen = []
        storage.scrape_logs.import_scrape_logs.side_effect = (
            lambda search_id, path, **kw: seen.append(path) or {"imported": 0, "skipped": 0}
        )

        api_client.post(
            "/api/searches/1/logs/import",
            data={"log_archive": (io.BytesIO(_zip_bytes()), "logs.zip")},
            content_type="multipart/form-data",
        )

        assert len(seen) == 1
        name = Path(seen[0]).name
        assert Path(seen[0]).parent == Path("/tmp")
        assert name.startswith("logs_import_1_") and name.endswith(".zip")
        # Le fichier temporaire est bien nettoyé dans le `finally`.
        assert not Path(seen[0]).exists()

    def test_the_temporary_file_is_removed_even_on_failure(self, api_client, storage, owned_search):
        seen = []

        def boom(search_id, path, **kw):
            seen.append(path)
            raise ValueError("archive corrompue")

        storage.scrape_logs.import_scrape_logs.side_effect = boom

        resp = api_client.post(
            "/api/searches/1/logs/import",
            data={"log_archive": (io.BytesIO(_zip_bytes()), "logs.zip")},
            content_type="multipart/form-data",
        )

        assert resp.status_code == 400
        assert resp.get_json()["error"] == "archive corrompue"
        assert not Path(seen[0]).exists()

    def test_allow_override_is_read_from_the_query_string(self, api_client, storage, owned_search):
        storage.scrape_logs.import_scrape_logs.return_value = {"imported": 0, "skipped": 0}

        api_client.post(
            "/api/searches/1/logs/import?allow_override=true",
            data={"log_archive": (io.BytesIO(_zip_bytes()), "logs.zip")},
            content_type="multipart/form-data",
        )

        assert storage.scrape_logs.import_scrape_logs.call_args[1]["allow_override"] is True

    @pytest.mark.parametrize("value", ["false", "1", "TRUE", "yes", ""])
    def test_only_the_exact_string_true_enables_override(self, api_client, storage, owned_search, value):
        storage.scrape_logs.import_scrape_logs.return_value = {"imported": 0, "skipped": 0}

        api_client.post(
            f"/api/searches/1/logs/import?allow_override={value}",
            data={"log_archive": (io.BytesIO(_zip_bytes()), "logs.zip")},
            content_type="multipart/form-data",
        )

        assert storage.scrape_logs.import_scrape_logs.call_args[1]["allow_override"] is False

    def test_override_required_maps_to_409(self, api_client, storage, owned_search):
        """L'archive vient d'un autre `search_id` : conflit, pas erreur de format."""
        storage.scrape_logs.import_scrape_logs.side_effect = ValueError("override_required")

        resp = api_client.post(
            "/api/searches/1/logs/import",
            data={"log_archive": (io.BytesIO(_zip_bytes()), "logs.zip")},
            content_type="multipart/form-data",
        )

        assert resp.status_code == 409
        assert resp.get_json() == {"error": "override_required"}

    @pytest.mark.parametrize(
        "data",
        [{}, {"log_archive": (io.BytesIO(b""), "")}],
        ids=["aucun-fichier", "nom-vide"],
    )
    def test_a_missing_file_yields_400(self, api_client, storage, owned_search, data):
        resp = api_client.post(
            "/api/searches/1/logs/import", data=data, content_type="multipart/form-data",
        )

        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Fichier manquant"
        storage.scrape_logs.import_scrape_logs.assert_not_called()

    def test_an_archive_over_200mb_yields_413(self, api_client, storage, owned_search, monkeypatch):
        """Le plafond est vérifié via `seek(0, 2)` puis `tell()`.

        La taille est simulée en interceptant `tell()` sur le FileStorage :
        allouer 200 Mo dans un test coûterait autant de mémoire pour la même
        assertion. Ce que le test vérifie est le comportement de la route face à
        une taille annoncée trop grande.
        """
        monkeypatch.setattr(FileStorage, "tell", lambda self: 200 * 1024 * 1024 + 1, raising=False)

        resp = api_client.post(
            "/api/searches/1/logs/import",
            data={"log_archive": (io.BytesIO(_zip_bytes()), "logs.zip")},
            content_type="multipart/form-data",
        )

        assert resp.status_code == 413
        assert "trop volumineux" in resp.get_json()["error"]
        storage.scrape_logs.import_scrape_logs.assert_not_called()

    def test_exactly_200mb_is_accepted(self, api_client, storage, owned_search, monkeypatch):
        """La borne est stricte (`>`), donc 200 Mo pile passe."""
        monkeypatch.setattr(FileStorage, "tell", lambda self: 200 * 1024 * 1024, raising=False)
        storage.scrape_logs.import_scrape_logs.return_value = {"imported": 0, "skipped": 0}

        resp = api_client.post(
            "/api/searches/1/logs/import",
            data={"log_archive": (io.BytesIO(_zip_bytes()), "logs.zip")},
            content_type="multipart/form-data",
        )

        assert resp.status_code == 200

    def test_the_size_check_happens_before_anything_is_written(
        self, api_client, storage, owned_search, monkeypatch
    ):
        """Aucun fichier de 200 Mo ne doit atterrir dans /tmp avant le refus."""
        monkeypatch.setattr(FileStorage, "tell", lambda self: 10**12, raising=False)
        saved = []
        monkeypatch.setattr(FileStorage, "save", lambda self, dst, **kw: saved.append(dst))

        resp = api_client.post(
            "/api/searches/1/logs/import",
            data={"log_archive": (io.BytesIO(_zip_bytes()), "logs.zip")},
            content_type="multipart/form-data",
        )

        assert resp.status_code == 413
        assert saved == []


class TestFilterOptionsAreNotExposedByTheApi:
    def test_the_api_listing_endpoint_does_not_return_filter_facets(
        self, api_client, storage, owned_search
    ):
        """Divergence API/web assumée : les facettes ne sont servies qu'au web.

        `/listings/<id>` (web) appelle `get_filter_options` et
        `get_unique_agencies_for_user` ; l'API, non. Un client API ne peut donc
        pas proposer les mêmes filtres que la page — à savoir en cas de
        réunification des deux.
        """
        storage.listings.get_filter_options.return_value = make_filter_options()

        body = api_client.get("/api/listings/1").get_json()

        assert "filter_options" not in body
        storage.listings.get_filter_options.assert_not_called()
        storage.listings.get_unique_agencies_for_user.assert_not_called()
