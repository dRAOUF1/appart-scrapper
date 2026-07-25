"""Tests for services/seloger_geocode.py (no real network calls)."""
from unittest.mock import MagicMock, patch

import pytest

from services.seloger_geocode import (
    _find_city_place_id,
    _find_wide_area_place_id,
    _pick_best_match,
    _query_autocomplete,
    area_cache_key,
    remember_manual_place_id,
    resolve_place_id,
)

# Des périmètres canoniques, tels que l'autocomplete les produit.
PARIS_15 = {"kind": "city", "city": "Paris", "postalCode": "75015", "inseeCode": "75115"}
NOWHERE = {"kind": "city", "city": "Nawak", "postalCode": "00000", "inseeCode": "99999"}

PARIS_WHOLE_CITY = {
    "id": "AD08FR31096", "type_key": "AD08", "labels": ["Paris (75)"],
    "postal_codes": [f"750{i:02d}" for i in range(21)],
}
PARIS_15E = {
    "id": "AD09FR40", "type_key": "AD09", "labels": ["Paris 15ème arrondissement (75015)"],
    "postal_codes": ["75015"],
}
NANTES = {
    "id": "AD08FR17221", "type_key": "AD08", "labels": ["Nantes (44)"],
    "postal_codes": ["44000", "44100", "44200", "44300"],
}


class TestQueryAutocomplete:
    def test_posts_expected_payload(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = [PARIS_15E]
        resp.raise_for_status.return_value = None
        with patch("services.seloger_geocode.requests.post", return_value=resp) as mock_post:
            results = _query_autocomplete("75015")
        assert results == [PARIS_15E]
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["text"] == "75015"
        assert kwargs["json"]["locale"] == "fr"

    def test_short_text_returns_empty_without_network_call(self):
        with patch("services.seloger_geocode.requests.post") as mock_post:
            assert _query_autocomplete("p") == []
        mock_post.assert_not_called()

    def test_raises_on_http_error(self):
        resp = MagicMock(status_code=500)
        resp.raise_for_status.side_effect = Exception("boom")
        with patch("services.seloger_geocode.requests.post", return_value=resp):
            with pytest.raises(Exception):
                _query_autocomplete("75015")


class TestPickBestMatch:
    def test_exact_match_beats_broad_match_regardless_of_order(self):
        """The whole-city AD08 entry technically "contains" 75015 in its
        postal_codes list too — the specific arrondissement/POCO entry
        (postal_codes == exactly [75015]) must win, not the broad one,
        regardless of which order the API returned them in."""
        match = _pick_best_match([PARIS_WHOLE_CITY, PARIS_15E], "75015")
        assert match["id"] == "AD09FR40"
        match2 = _pick_best_match([PARIS_15E, PARIS_WHOLE_CITY], "75015")
        assert match2["id"] == "AD09FR40"

    def test_falls_back_to_a_broader_match_containing_the_postal_code(self):
        match = _pick_best_match([NANTES], "44100")
        assert match["id"] == "AD08FR17221"

    def test_falls_back_to_first_result_when_nothing_contains_it(self):
        """Defensive fallback — shouldn't normally happen since we only get
        here after a non-empty query, but never crash on an unexpected shape."""
        match = _pick_best_match([NANTES], "99999")
        assert match["id"] == "AD08FR17221"

    def test_empty_results_returns_none(self):
        assert _pick_best_match([], "75015") is None


class TestFindCityPlaceId:
    def test_queries_by_postal_code_only(self):
        """No city-name fallback: a plain city-name query caps at 10
        results, which for a 20-arrondissement city can push the specific
        match we need out of the window entirely — the postal-code query
        reliably returns it directly, so there's no fallback to fall
        through to a wrong, broader match with."""
        with patch("services.seloger_geocode._query_autocomplete", return_value=[PARIS_15E]) as mock_query:
            place_id = _find_city_place_id("75015")
        assert place_id == "AD09FR40"
        mock_query.assert_called_once_with("75015")

    def test_empty_postal_code_query_returns_none_not_a_broader_guess(self):
        with patch("services.seloger_geocode._query_autocomplete", return_value=[]) as mock_query:
            place_id = _find_city_place_id("44000")
        assert place_id is None
        mock_query.assert_called_once_with("44000")

    def test_returns_none_when_nothing_found(self):
        with patch("services.seloger_geocode._query_autocomplete", return_value=[]):
            assert _find_city_place_id("99999") is None


class TestFindWideAreaPlaceId:
    """Les périmètres larges se cherchent par NOM, et le type d'entrée SeLoger
    discrimine le niveau voulu. Vérifié en live : « Île-de-France » -> AD04FR5,
    « Gironde » -> AD06FR34, « Paris » -> AD08FR31096."""

    IDF = {"id": "AD04FR5", "type_key": "AD04", "labels": ["Ile-de-France"], "postal_codes": []}
    GIRONDE_DEPT = {"id": "AD06FR34", "type_key": "AD06", "labels": ["Gironde (33)"], "postal_codes": []}
    GIRONDE_COMMUNE = {"id": "AD08FR13223", "type_key": "AD08",
                       "labels": ["Gironde-sur-Dropt (33190)"], "postal_codes": ["33190"]}

    def test_region_is_found_by_name(self):
        with patch("services.seloger_geocode._query_autocomplete", return_value=[self.IDF]) as q:
            assert _find_wide_area_place_id("Île-de-France", "region") == "AD04FR5"
        q.assert_called_once_with("Île-de-France")

    def test_department_is_found_by_name(self):
        with patch("services.seloger_geocode._query_autocomplete", return_value=[self.GIRONDE_DEPT]):
            assert _find_wide_area_place_id("Gironde", "department") == "AD06FR34"

    def test_the_type_discriminates_a_homonym(self):
        """« Gironde » désigne aussi des communes : sans filtrage sur le type,
        une recherche départementale tomberait sur Gironde-sur-Dropt."""
        results = [self.GIRONDE_COMMUNE, self.GIRONDE_DEPT]
        with patch("services.seloger_geocode._query_autocomplete", return_value=results):
            assert _find_wide_area_place_id("Gironde", "department") == "AD06FR34"

    def test_whole_city_uses_the_city_level_entry(self):
        paris = {"id": "AD08FR31096", "type_key": "AD08", "labels": ["Paris (75)"],
                 "postal_codes": [f"750{i:02d}" for i in range(21)]}
        with patch("services.seloger_geocode._query_autocomplete", return_value=[paris]):
            assert _find_wide_area_place_id("Paris", "whole_city") == "AD08FR31096"

    def test_no_matching_type_returns_none(self):
        with patch("services.seloger_geocode._query_autocomplete", return_value=[self.GIRONDE_COMMUNE]):
            assert _find_wide_area_place_id("Gironde", "department") is None

    def test_missing_name_returns_none_without_a_call(self):
        with patch("services.seloger_geocode._query_autocomplete") as q:
            assert _find_wide_area_place_id("", "region") is None
        q.assert_not_called()


class TestAreaCacheKey:
    """La clé de cache doit distinguer les niveaux : le département 75 (Paris)
    et la région 75 (Nouvelle-Aquitaine) existent tous les deux."""

    def test_city_keeps_the_bare_insee_code(self):
        """Convention d'avant les périmètres larges, conservée pour ne pas
        invalider les résolutions déjà en cache."""
        assert area_cache_key(PARIS_15) == "75115"

    def test_each_level_has_its_own_namespace(self):
        assert area_cache_key({"kind": "whole_city", "inseeCode": "33063"}) == "city:33063"
        assert area_cache_key({"kind": "department", "code": "75"}) == "dept:75"
        assert area_cache_key({"kind": "region", "code": "75"}) == "region:75"

    def test_same_code_at_two_levels_never_collides(self):
        dept = area_cache_key({"kind": "department", "code": "75"})
        region = area_cache_key({"kind": "region", "code": "75"})
        assert dept != region

    def test_unidentifiable_area_has_no_key(self):
        assert area_cache_key({"kind": "region", "name": "Nulle part"}) is None
        assert area_cache_key({"kind": "city", "city": "X", "postalCode": "99999"}) is None


class TestResolveUncached:
    def test_returns_none_on_network_error(self):
        """L'exception est rattrapée ici, pas plus bas : les fonctions de
        recherche la laissent remonter."""
        from services.seloger_geocode import _resolve_uncached
        with patch("services.seloger_geocode._query_autocomplete", side_effect=Exception("boom")):
            assert _resolve_uncached(PARIS_15) is None
            assert _resolve_uncached({"kind": "region", "name": "X", "code": "11"}) is None

    def test_routes_each_level_to_the_right_lookup(self):
        from services.seloger_geocode import _resolve_uncached
        with patch("services.seloger_geocode._find_city_place_id", return_value="POCO1") as city:
            with patch("services.seloger_geocode._find_wide_area_place_id", return_value="AD06X") as wide:
                assert _resolve_uncached(PARIS_15) == "POCO1"
                assert _resolve_uncached({"kind": "department", "name": "Gironde", "code": "33"}) == "AD06X"
        city.assert_called_once_with("75015")
        wide.assert_called_once_with("Gironde", "department")


class TestResolvePlaceId:
    def test_returns_cached_place_id_without_crawling(self):
        repo = MagicMock()
        repo.get_cached.return_value = {"place_id": "AD08FR31096", "resolved_at": None}
        with patch("services.seloger_geocode._resolve_uncached") as mock_crawl:
            place_id = resolve_place_id(PARIS_15, repo=repo)
        assert place_id == "AD08FR31096"
        mock_crawl.assert_not_called()

    def test_crawls_and_caches_on_first_lookup(self):
        repo = MagicMock()
        repo.get_cached.return_value = None
        with patch("services.seloger_geocode._resolve_uncached", return_value="AD08FR31096"):
            place_id = resolve_place_id(PARIS_15, repo=repo)
        assert place_id == "AD08FR31096"
        repo.set_cached.assert_called_once_with("75115", "AD08FR31096")

    def test_caches_a_failed_crawl_as_none(self):
        repo = MagicMock()
        repo.get_cached.return_value = None
        with patch("services.seloger_geocode._resolve_uncached", return_value=None):
            place_id = resolve_place_id(NOWHERE, repo=repo)
        assert place_id is None
        repo.set_cached.assert_called_once_with("99999", None)

    def test_recent_failure_is_not_retried(self):
        import datetime
        repo = MagicMock()
        repo.get_cached.return_value = {
            "place_id": None, "resolved_at": datetime.datetime.utcnow(),
        }
        with patch("services.seloger_geocode._resolve_uncached") as mock_crawl:
            place_id = resolve_place_id(NOWHERE, repo=repo)
        assert place_id is None
        mock_crawl.assert_not_called()

    def test_old_failure_is_retried(self):
        import datetime
        repo = MagicMock()
        repo.get_cached.return_value = {
            "place_id": None,
            "resolved_at": datetime.datetime.utcnow() - datetime.timedelta(days=30),
        }
        with patch("services.seloger_geocode._resolve_uncached", return_value="AD08FR31096") as mock_crawl:
            place_id = resolve_place_id(PARIS_15, repo=repo)
        assert place_id == "AD08FR31096"
        mock_crawl.assert_called_once()


class TestRememberManualPlaceId:
    def test_banks_single_location_single_place_id(self):
        repo = MagicMock()
        repo.get_cached.return_value = None
        criteria = {
            "sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096"]}},
            "locations": [{"city": "Paris", "postalCode": "75015", "inseeCode": "75115"}],
        }
        remember_manual_place_id(criteria, repo=repo)
        repo.set_cached.assert_called_once_with("75115", "AD08FR31096")

    def test_skips_when_already_cached(self):
        repo = MagicMock()
        repo.get_cached.return_value = {"place_id": "AD08FR31096", "resolved_at": None}
        criteria = {
            "sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096"]}},
            "locations": [{"city": "Paris", "postalCode": "75015", "inseeCode": "75115"}],
        }
        remember_manual_place_id(criteria, repo=repo)
        repo.set_cached.assert_not_called()

    def test_skips_ambiguous_multi_location_entry(self):
        repo = MagicMock()
        criteria = {
            "sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096"]}},
            "locations": [
                {"city": "Paris", "postalCode": "75015", "inseeCode": "75115"},
                {"city": "Lyon", "postalCode": "69007", "inseeCode": "69387"},
            ],
        }
        remember_manual_place_id(criteria, repo=repo)
        repo.set_cached.assert_not_called()

    def test_skips_multiple_place_ids(self):
        repo = MagicMock()
        criteria = {
            "sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096", "AD08FR12345"]}},
            "locations": [{"city": "Paris", "postalCode": "75015", "inseeCode": "75115"}],
        }
        remember_manual_place_id(criteria, repo=repo)
        repo.set_cached.assert_not_called()

    def test_skips_location_without_insee_code(self):
        """A manually-typed location (no autocomplete) has no inseeCode —
        nothing to key the cache on, so this must be a safe no-op."""
        repo = MagicMock()
        criteria = {
            "sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096"]}},
            "locations": [{"city": "Paris", "postalCode": "75015"}],
        }
        remember_manual_place_id(criteria, repo=repo)
        repo.set_cached.assert_not_called()

    def test_no_placeids_is_a_noop(self):
        repo = MagicMock()
        remember_manual_place_id({"locations": []}, repo=repo)
        repo.get_cached.assert_not_called()
        repo.set_cached.assert_not_called()
