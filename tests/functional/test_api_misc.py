"""`routes/api.py` — l'autocomplete de localisation, seule route API restante.

L'API JSON publique (utilisateurs, sources, recherches, annonces, stats, logs)
a été supprimée avec le token (#30) : les classes qui la testaient ont disparu
avec elle, et un balayage en fin de fichier garantit qu'aucun ancien chemin ne
réapparaît par accident.
"""

from __future__ import annotations

import pytest


class TestRemovedEndpoints:
    """La surface API a été volontairement réduite à `/api/locations`.

    Chaque paramètre est un ancien endpoint documenté du README : s'il
    réapparaît (régression de fusion, copier-coller depuis une vieille branche),
    ce test échoue et force à se poser la question de la surface d'attaque.
    """

    REMOVED = [
        pytest.param("POST", "/api/users", id="create-user"),
        pytest.param("POST", "/api/users/login", id="login-user"),
        pytest.param("GET", "/api/sources", id="sources"),
        pytest.param("GET", "/api/stats", id="stats"),
        pytest.param("POST", "/api/searches", id="create-search"),
        pytest.param("GET", "/api/searches", id="list-searches"),
        pytest.param("DELETE", "/api/searches/1", id="delete-search"),
        pytest.param("GET", "/api/searches/1/urls", id="search-urls"),
        pytest.param("PUT", "/api/searches/1/criteria", id="update-criteria"),
        pytest.param("POST", "/api/searches/1/toggle-active", id="toggle-active"),
        pytest.param("PUT", "/api/searches/1/blacklist-mode", id="blacklist-mode"),
        pytest.param("PUT", "/api/searches/1/blacklist-agencies", id="blacklist-agencies"),
        pytest.param("POST", "/api/scrape/1", id="scrape"),
        pytest.param("GET", "/api/listings/1", id="listings"),
        pytest.param("POST", "/api/cleanup", id="cleanup"),
        pytest.param("GET", "/api/searches/1/logs/export", id="logs-export"),
        pytest.param("POST", "/api/searches/1/logs/import", id="logs-import"),
    ]

    @pytest.mark.parametrize(("method", "path"), REMOVED)
    def test_a_removed_endpoint_answers_404(self, client, storage, method, path):
        resp = client.open(path, method=method)

        assert resp.status_code == 404, f"{method} {path} répond encore !"
        # Aucun effet de bord : la route n'existe plus, rien n'a été touché.
        assert not any(
            getattr(repo, attr).called
            for repo in (storage.users, storage.searches, storage.listings, storage.scrape_logs)
            for attr in dir(repo)
            if not attr.startswith("_") and getattr(getattr(repo, attr), "called", False)
        )

    def test_the_locations_autocomplete_is_the_sole_survivor(self, client, monkeypatch):
        """Le formulaire (static/search_form.js) dépend de cet endpoint : sa
        disparition casserait la saisie de lieu de TOUTE création de recherche."""
        import core.geocode

        monkeypatch.setattr(core.geocode, "search_locations", lambda query, limit=20: [])

        resp = client.get("/api/locations?q=test")

        assert resp.status_code == 200


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

    def test_it_needs_no_authentication(self, client, storage, monkeypatch):
        """Endpoint public assumé : il alimente le formulaire AVANT connexion,
        et ne révèle que des noms de communes publics."""
        import core.geocode

        monkeypatch.setattr(core.geocode, "_query", lambda url, params: [])

        client.get("/api/locations?q=poitiers")

        storage.users.get_user_by_id.assert_not_called()

    def test_outbound_failures_degrade_to_an_empty_list(self, client):
        """Le réseau est coupé par le socle de test : la route répond quand même.

        Comportement voulu — l'autocomplete ne doit pas rendre le formulaire
        inutilisable si geo.api.gouv.fr est indisponible.
        """
        resp = client.get("/api/locations?q=poitiers")

        assert resp.status_code == 200
        assert resp.get_json() == []
