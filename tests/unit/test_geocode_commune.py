"""Tests unitaires de services/geocode_commune.py — le fallback géocodage
commune de l'issue #26.

Le contrat central : BEST EFFORT. Un geo.api.gouv.fr down, lent ou muet ne
doit JAMAIS faire échouer un scrape — ni lever, ni stocker un 0.0/0.0. Le
cache (succès seuls) est lu puis écrit via le repo injecté ; aucun appel réel
n'est jamais tenté (le socle bloque le transport HTTP).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from models.listing import Listing
from repositories.commune_geo_repo import CommuneGeoRepository
from services import geocode_commune as geo
from tests.helpers.factories import make_listing
from tests.helpers.fakes import RecordingConnection, bind_repository

# Réponse réelle geo.api.gouv.fr (capture toulouse_centre.json — GeoJSON :
# coordinates = [longitude, latitude]).
TOULOUSE = {
    "nom": "Toulouse", "code": "31555",
    "centre": {"type": "Point", "coordinates": [1.4328, 43.6007]},
}
CASTANET = {
    "nom": "Castanet-Tolosan", "code": "31316",
    "centre": {"type": "Point", "coordinates": [1.4973, 43.5175]},
}


@pytest.fixture
def repo():
    double = MagicMock(spec=CommuneGeoRepository)
    double.get_cached.return_value = None
    return double


def listing_sans_coords(**overrides) -> Listing:
    defaults = {
        "zip_code": "31500",
        "city": "Toulouse",
        "latitude": None,
        "longitude": None,
        "location_precision": "",
    }
    defaults.update(overrides)
    return make_listing(**defaults)


class TestResolveUncached:
    def test_a_postal_code_resolves_to_the_commune_centre(self, requests_mock):
        mock = requests_mock.get(geo.GEO_API_URL, json=[TOULOUSE])

        assert geo._resolve_uncached("31500", "Toulouse") == (43.6007, 1.4328)

        request = mock.last_request
        assert request.qs["codepostal"] == ["31500"]
        assert request.qs["fields"] == ["centre"]

    def test_geojson_order_is_lon_first(self, requests_mock):
        """Inverser [lon, lat] mettrait Toulouse au pôle Nord-Est."""
        requests_mock.get(geo.GEO_API_URL, json=[TOULOUSE])

        lat, lon = geo._resolve_uncached("31500", "Toulouse")

        assert lat == 43.6007
        assert lon == 1.4328

    def test_the_city_name_disambiguates_a_shared_postal_code(self, requests_mock):
        requests_mock.get(geo.GEO_API_URL, json=[TOULOUSE, CASTANET])

        commune = geo._pick_commune([TOULOUSE, CASTANET], "castanet-tolosan")

        assert commune is CASTANET

    @pytest.mark.parametrize(
        "payload",
        [[], [{"nom": "X"}], [{"centre": {"coordinates": []}}]],
        ids=["aucune-commune", "sans-centre", "coordonnees-vides"],
    )
    def test_unexpected_responses_yield_none(self, payload, requests_mock):
        requests_mock.get(geo.GEO_API_URL, json=payload)

        assert geo._resolve_uncached("00000", "Nulle part") is None

    @pytest.mark.parametrize(
        "status", [400, 404, 429, 500, 503],
        ids=["bad-request", "not-found", "rate-limited", "server-error", "unavailable"],
    )
    def test_http_errors_are_swallowed_not_raised(self, status, requests_mock):
        requests_mock.get(geo.GEO_API_URL, status_code=status, json={})

        assert geo._resolve_uncached("31500", "Toulouse") is None

    def test_a_network_failure_is_swallowed_not_raised(self, requests_mock):
        requests_mock.get(geo.GEO_API_URL, exc=requests.ConnectTimeout("timeout"))

        assert geo._resolve_uncached("31500", "Toulouse") is None


class TestCompleterCoordonneesManquantes:
    def test_listings_without_zip_are_left_untouched(self, repo, requests_mock):
        # Le CP manquant n'est jamais résolu ; celui qui en a un passe par le
        # réseau simulé.
        requests_mock.get(geo.GEO_API_URL, json=[TOULOUSE])
        sans_cp = listing_sans_coords(zip_code="")
        avec_cp = listing_sans_coords()

        completes = geo.completer_coordonnees_manquantes([sans_cp, avec_cp], repo)

        assert completes == 1
        assert sans_cp.latitude is None and sans_cp.location_precision == ""
        assert avec_cp.latitude == 43.6007

    def test_geolocated_listings_are_never_reprocessed(self, repo):
        deja_placee = listing_sans_coords(latitude=44.85, longitude=-0.57,
                                          location_precision="exacte")

        assert geo.completer_coordonnees_manquantes([deja_placee], repo) == 0
        assert repo.get_cached.call_count == 0

    def test_the_cache_short_circuits_the_network(self, repo, monkeypatch):
        repo.get_cached.return_value = {
            "area_key": "postal:75013", "latitude": 48.8289, "longitude": 2.3561,
        }

        def refuse(*args, **kwargs):
            raise AssertionError("Aucun réseau attendu quand le cache répond")

        monkeypatch.setattr(geo.requests, "get", refuse)

        annonce = listing_sans_coords(zip_code="75013", city="Paris")

        completes = geo.completer_coordonnees_manquantes([annonce], repo)

        assert completes == 1
        assert (annonce.latitude, annonce.longitude) == (48.8289, 2.3561)

    def test_one_resolution_is_shared_by_all_listings_of_the_same_commune(self, repo, monkeypatch):
        asked: list[tuple[str, str]] = []

        def fake_resolve(cp, ville):
            asked.append((cp, ville))
            return (43.6007, 1.4328)

        monkeypatch.setattr(geo, "_resolve_uncached", fake_resolve)
        annonces = [
            listing_sans_coords(title=f"Annonce {i}") for i in range(3)
        ]

        completes = geo.completer_coordonnees_manquantes(annonces, repo)

        assert completes == 3
        assert len(asked) == 1  # UNE résolution réseau pour tout le CP
        repo.set_cached.assert_called_once_with("postal:31500", 43.6007, 1.4328)
        for annonce in annonces:
            assert annonce.location_precision == "commune"

    def test_a_failed_resolution_leaves_everything_untouched_and_raises_nothing(self, repo, monkeypatch):
        monkeypatch.setattr(geo, "_resolve_uncached", lambda cp, ville: None)
        annonces = [listing_sans_coords(), listing_sans_coords()]

        completes = geo.completer_coordonnees_manquantes(annonces, repo)

        assert completes == 0
        repo.set_cached.assert_not_called()
        for annonce in annonces:
            assert annonce.latitude is None
            assert annonce.location_precision == ""

    def test_a_repo_crash_is_propagated_to_no_one_but_caught_upstream(self):
        """Un repo en échec lève ici (le service ne masque pas les bugs DB) ;
        c'est l'appelant ScrapeService qui garantit le « jamais bloquant » par
        son try/except externe."""
        repo_en_panique = MagicMock(spec=CommuneGeoRepository)
        repo_en_panique.get_cached.side_effect = RuntimeError("DB down")

        with pytest.raises(RuntimeError):
            geo.completer_coordonnees_manquantes([listing_sans_coords()], repo_en_panique)


class TestCacheContract:
    def test_only_successes_are_cached(self, requests_mock):
        """Contrairement aux caches de résolution par source (échec gelé 7
        jours), un CP non résolu est re-tenté au scrape suivant."""
        conn = RecordingConnection(results=[None])
        repo_reel = bind_repository(CommuneGeoRepository, conn)

        repo_reel.set_cached("postal:31500", 43.6007, 1.4328)

        sql = conn.sql[0]
        assert "INSERT INTO commune_centres" in sql
        assert "ON CONFLICT" in sql
        # Pas de NULL possible dans les colonnes géo du cache : un centre
        # mémorisé est toujours utilisable.
        assert "latitude" in sql and "longitude" in sql

    def test_a_never_resolved_key_returns_none(self):
        conn = RecordingConnection(results=[None])
        repo_reel = bind_repository(CommuneGeoRepository, conn)

        assert repo_reel.get_cached("postal:31500") is None

    def test_a_cached_centre_round_trips(self):
        conn = RecordingConnection(
            results=[{"area_key": "postal:31500", "latitude": 43.6007,
                      "longitude": 1.4328, "resolved_at": "2026-08-23T10:00:00"}]
        )
        repo_reel = bind_repository(CommuneGeoRepository, conn)

        cached = repo_reel.get_cached("postal:31500")

        assert cached["latitude"] == 43.6007
        assert cached["longitude"] == 1.4328
