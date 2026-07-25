"""Tests for services/seloger_geocode.py (no real network calls)."""
from unittest.mock import MagicMock, patch

import pytest

from services.seloger_geocode import (
    _find_place_id,
    _pick_best_match,
    _query_autocomplete,
    remember_manual_place_id,
    resolve_place_id,
)

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


class TestFindPlaceId:
    def test_queries_by_postal_code_only(self):
        """No city-name fallback: a plain city-name query caps at 10
        results, which for a 20-arrondissement city can push the specific
        match we need out of the window entirely — the postal-code query
        reliably returns it directly, so there's no fallback to fall
        through to a wrong, broader match with."""
        with patch("services.seloger_geocode._query_autocomplete", return_value=[PARIS_15E]) as mock_query:
            place_id = _find_place_id("Paris", "75015")
        assert place_id == "AD09FR40"
        mock_query.assert_called_once_with("75015")

    def test_empty_postal_code_query_returns_none_not_a_broader_guess(self):
        with patch("services.seloger_geocode._query_autocomplete", return_value=[]) as mock_query:
            place_id = _find_place_id("Nantes", "44000")
        assert place_id is None
        mock_query.assert_called_once_with("44000")

    def test_returns_none_when_nothing_found(self):
        with patch("services.seloger_geocode._query_autocomplete", return_value=[]):
            assert _find_place_id("Nawak", "99999") is None

    def test_returns_none_on_network_error(self):
        with patch("services.seloger_geocode._query_autocomplete", side_effect=Exception("boom")):
            assert _find_place_id("Paris", "75015") is None


class TestResolvePlaceId:
    def test_returns_cached_place_id_without_crawling(self):
        repo = MagicMock()
        repo.get_cached.return_value = {"place_id": "AD08FR31096", "resolved_at": None}
        with patch("services.seloger_geocode._find_place_id") as mock_crawl:
            place_id = resolve_place_id("75115", "Paris", "75015", repo=repo)
        assert place_id == "AD08FR31096"
        mock_crawl.assert_not_called()

    def test_crawls_and_caches_on_first_lookup(self):
        repo = MagicMock()
        repo.get_cached.return_value = None
        with patch("services.seloger_geocode._find_place_id", return_value="AD08FR31096"):
            place_id = resolve_place_id("75115", "Paris", "75015", repo=repo)
        assert place_id == "AD08FR31096"
        repo.set_cached.assert_called_once_with("75115", "AD08FR31096")

    def test_caches_a_failed_crawl_as_none(self):
        repo = MagicMock()
        repo.get_cached.return_value = None
        with patch("services.seloger_geocode._find_place_id", return_value=None):
            place_id = resolve_place_id("99999", "Nawak", "00000", repo=repo)
        assert place_id is None
        repo.set_cached.assert_called_once_with("99999", None)

    def test_recent_failure_is_not_retried(self):
        import datetime
        repo = MagicMock()
        repo.get_cached.return_value = {
            "place_id": None, "resolved_at": datetime.datetime.utcnow(),
        }
        with patch("services.seloger_geocode._find_place_id") as mock_crawl:
            place_id = resolve_place_id("99999", "Nawak", "00000", repo=repo)
        assert place_id is None
        mock_crawl.assert_not_called()

    def test_old_failure_is_retried(self):
        import datetime
        repo = MagicMock()
        repo.get_cached.return_value = {
            "place_id": None,
            "resolved_at": datetime.datetime.utcnow() - datetime.timedelta(days=30),
        }
        with patch("services.seloger_geocode._find_place_id", return_value="AD08FR31096") as mock_crawl:
            place_id = resolve_place_id("75115", "Paris", "75015", repo=repo)
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
