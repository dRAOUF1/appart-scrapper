"""Tests unitaires de `scraper/bienici.py`.

Contrairement à SeLoger (JSON embarqué, anti-bot DataDome), bienici expose une
API JSON dédiée — l'essentiel à couvrir ici est donc la pagination
`from`/`size` et le plafond de sécurité, pas le décodage d'un blob fragile.
"""

from __future__ import annotations

import json

import pytest
import requests

from scraper.bienici import ADS_URL, MAX_PAGES, PAGE_SIZE, _fetch_page, _headers, scrape


def _page(total: int, ads: list[dict]) -> dict:
    return {"total": total, "realEstateAds": ads}


def _ad(ad_id: str) -> dict:
    return {"id": ad_id}


# ---------------------------------------------------------------------------
# _headers
# ---------------------------------------------------------------------------

class TestHeaders:
    def test_headers_look_like_a_real_browser_request(self):
        """Recommandation reprise du blog lobstr.io pour éviter tout blocage —
        pas de warm-up de cookie/token, confirmé inutile en test direct."""
        headers = _headers()

        assert "Chrome" in headers["User-Agent"]
        assert headers["Referer"] == "https://www.bienici.com/"
        assert headers["X-Requested-With"] == "XMLHttpRequest"


# ---------------------------------------------------------------------------
# _fetch_page
# ---------------------------------------------------------------------------

class TestFetchPage:
    def test_the_filters_dict_is_json_encoded_in_the_query_string(self, requests_mock):
        mock = requests_mock.get(ADS_URL, json=_page(0, []))

        _fetch_page({"filterType": "rent", "size": 24, "from": 0})

        request = mock.last_request
        assert json.loads(request.qs["filters"][0]) == {"filtertype": "rent", "size": 24, "from": 0}

    def test_a_successful_response_is_returned_as_is(self, requests_mock):
        requests_mock.get(ADS_URL, json=_page(1, [_ad("a1")]))

        assert _fetch_page({}) == _page(1, [_ad("a1")])

    def test_a_network_error_is_retried_with_backoff(self, requests_mock, slept):
        requests_mock.get(
            ADS_URL,
            [
                {"exc": requests.exceptions.ConnectionError},
                {"exc": requests.exceptions.ConnectionError},
                {"json": _page(0, [])},
            ],
        )

        result = _fetch_page({}, max_retries=3)

        assert result == _page(0, [])
        assert len(slept) == 2, "une attente avant chaque nouvelle tentative, pas avant la première"

    def test_exhausting_all_retries_raises_a_clear_error(self, requests_mock, slept):
        requests_mock.get(ADS_URL, exc=requests.exceptions.ConnectionError)

        with pytest.raises(ValueError, match="tentatives"):
            _fetch_page({}, max_retries=3)

    def test_an_http_error_status_is_also_retried_then_raised(self, requests_mock, slept):
        requests_mock.get(ADS_URL, status_code=500)

        with pytest.raises(ValueError, match="tentatives"):
            _fetch_page({}, max_retries=2)


# ---------------------------------------------------------------------------
# scrape — pagination
# ---------------------------------------------------------------------------

class TestScrapePagination:
    def test_a_single_page_below_the_page_size_stops_immediately(self, requests_mock):
        requests_mock.get(ADS_URL, json=_page(2, [_ad("a1"), _ad("a2")]))

        result = scrape({"filterType": "rent"})

        assert [ad["id"] for ad in result] == ["a1", "a2"]
        assert requests_mock.call_count == 1

    def test_several_full_pages_are_all_collected_in_order(self, requests_mock):
        page1 = _page(PAGE_SIZE + 1, [_ad(f"p1-{i}") for i in range(PAGE_SIZE)])
        page2 = _page(PAGE_SIZE + 1, [_ad("p2-0")])
        requests_mock.get(ADS_URL, [{"json": page1}, {"json": page2}])

        result = scrape({})

        assert [ad["id"] for ad in result] == [f"p1-{i}" for i in range(PAGE_SIZE)] + ["p2-0"]
        assert requests_mock.call_count == 2

    def test_the_from_offset_advances_by_the_number_of_ads_actually_received(self, requests_mock):
        """Pas par PAGE_SIZE fixe : une dernière page partielle ne doit pas
        faire sauter `from` au-delà de `total`."""
        page1 = _page(30, [_ad(f"a{i}") for i in range(PAGE_SIZE)])
        page2 = _page(30, [_ad(f"b{i}") for i in range(30 - PAGE_SIZE)])
        mock = requests_mock.get(ADS_URL, [{"json": page1}, {"json": page2}])

        scrape({})

        second_call_filters = json.loads(mock.request_history[1].qs["filters"][0])
        assert second_call_filters["from"] == PAGE_SIZE

    def test_an_empty_page_before_reaching_the_announced_total_is_an_error(self, requests_mock):
        """Filet de sécurité : si l'API renvoie 0 annonce alors que `total`
        laisse penser qu'il en reste, on n'insiste pas indéfiniment."""
        requests_mock.get(ADS_URL, json=_page(1000, []))

        with pytest.raises(ValueError, match="page vide inattendue"):
            scrape({})
        assert requests_mock.call_count == 1

    @pytest.mark.parametrize(
        "payload",
        [[], {}, {"total": 0}, {"realEstateAds": []}, {"total": "0", "realEstateAds": []}],
    )
    def test_an_invalid_json_schema_is_retried_then_reported(self, requests_mock, payload):
        requests_mock.get(ADS_URL, json=payload)

        with pytest.raises(ValueError, match="schéma JSON inattendu"):
            _fetch_page({}, max_retries=1)

    def test_the_max_pages_safety_cap_stops_a_pathologically_large_search(self, requests_mock):
        """Limite structurelle documentée de l'API (~2500 annonces / ~100
        pages) : matérialisée ici plutôt que de boucler indéfiniment dessus."""
        full_page = _page(10**9, [_ad(f"x{i}") for i in range(PAGE_SIZE)])
        requests_mock.get(ADS_URL, json=full_page)

        result = scrape({})

        assert requests_mock.call_count == MAX_PAGES
        assert len(result) == MAX_PAGES * PAGE_SIZE

    def test_native_criteria_are_forwarded_verbatim_alongside_pagination(self, requests_mock):
        """`native` (filterType, zoneIdsByTypes, ...) doit atteindre l'API tel
        quel — c'est BienIciParser.to_native() qui en est responsable, ce
        module ne fait qu'ajouter size/from par-dessus."""
        mock = requests_mock.get(ADS_URL, json=_page(0, []))
        native = {"filterType": "buy", "zoneIdsByTypes": {"zoneIds": ["-7444"]}}

        scrape(native)

        sent = json.loads(mock.last_request.qs["filters"][0])
        assert sent["filtertype"] == "buy"
        assert sent["zoneidsbytypes"] == {"zoneids": ["-7444"]}
