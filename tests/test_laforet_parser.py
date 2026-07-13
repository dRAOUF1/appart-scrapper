"""Tests for parsers/laforet.py."""
from unittest.mock import MagicMock, patch

import pytest

from models.listing import Listing
from parsers.laforet import (
    LaforetParser,
    _arrondissement_insee_code,
    _dict_to_listing,
    _extract_genuine_section,
    _parse_cards,
    _parse_total_pages,
    _passes_filters,
    _resolve_insee_code,
    _slugify,
)

SAMPLE_PAGE_HTML = """
<html><body>
<script type="application/ld+json">
[{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[]},
 {"@context":"https://schema.org","@type":"ItemList","itemListElement":[
    {"@type":"ListItem","position":1,"url":"/ville/location-appartement-paris-75000"},
    {"@type":"ListItem","position":2,"url":"/ville/location-appartement-paris-75000?page=2"},
    {"@type":"ListItem","position":3,"url":"/ville/location-appartement-paris-75000?page=3"}
 ]}]
</script>
<article>
  <a href="https://www.laforet.com/agence-immobiliere/paris18marxdormoy/louer/paris-18/appartement-1-piece-52811904" target="_blank">photo</a>
  <h3>Appartement <span>1 021 &#8364;/mois</span> <span>PARIS (75018)</span></h3>
  <div>30 m²&nbsp;&bull;&nbsp;1 pi&egrave;ce</div>
</article>
<article>
  <a href="https://www.laforet.com/agence-immobiliere/paris15lourmel/louer/paris-15/appartement-2-pieces-52805433" target="_blank">photo</a>
  <h3>Appartement <span>1 513 &#8364;/mois</span> <span>PARIS (75015)</span></h3>
  <div>50 m²&nbsp;&bull;&nbsp;2 pi&egrave;ces</div>
</article>
</body></html>
"""

# A merged multi-city response (as produced by filter[cities][]) has both
# cities' genuine cards in the same section — unlike SAMPLE_PAGE_HTML, which
# models a single-city page where the 75015 card is backfill noise.
MERGED_PAGE_HTML = """
<html><body>
<article>
  <a href="https://www.laforet.com/agence-immobiliere/paris18marxdormoy/louer/paris-18/appartement-1-piece-52811904" target="_blank">photo</a>
  <h3>Appartement <span>1 021 &#8364;/mois</span> <span>PARIS (75018)</span></h3>
  <div>30 m²&nbsp;&bull;&nbsp;1 pi&egrave;ce</div>
</article>
<article>
  <a href="https://www.laforet.com/agence-immobiliere/lyon7agence/louer/lyon-07/appartement-2-pieces-99999999" target="_blank">photo</a>
  <h3>Appartement <span>900 &#8364;/mois</span> <span>LYON (69007)</span></h3>
  <div>40 m²&nbsp;&bull;&nbsp;2 pi&egrave;ces</div>
</article>
</body></html>
"""

# Reproduces a real bug found live: when a city has thin inventory, Laforet
# backfills the page with "nearby agency office" cards. Their link
# (/agence-immobiliere/lyon-7) has no listing content but still ends in
# "-<digit>" (the arrondissement number in the agency's own slug), which a
# looser regex misidentified as a listing detail URL.
PAGE_WITH_AGENCY_OFFICE_CARD = """
<html><body>
<article>
  <a href="https://www.laforet.com/agence-immobiliere/lyon-7" target="_blank">Agence Laforêt LYON 7</a>
  <div>Fermé — 55 avenue Jean Jaurès, 69007 LYON</div>
</article>
</body></html>
"""


@pytest.fixture(autouse=True)
def _clear_insee_cache():
    from parsers.laforet import _INSEE_CACHE
    _INSEE_CACHE.clear()
    yield
    _INSEE_CACHE.clear()


class TestSlugify:
    def test_lowercases_and_strips_accents(self):
        assert _slugify("Paris") == "paris"
        assert _slugify("Le Kremlin-Bicêtre") == "le-kremlin-bicetre"
        assert _slugify("Île-de-France") == "ile-de-france"

    def test_collapses_non_alnum_to_single_hyphen(self):
        assert _slugify("Charenton-le-Pont") == "charenton-le-pont"
        assert _slugify("  Saint  Mandé  ") == "saint-mande"


class TestBuildSearchUrl:
    """build_search_url()/build_search_urls() mirror scrape()'s own merge
    strategy (see TestBuildSearchUrls) — every location resolvable to an
    INSEE code produces one combined URL, exactly what's actually fetched,
    not a plain per-location link. Paris/Lyon/Marseille resolve via the
    pure arrondissement formula (no network call needed in these tests)."""

    def test_returns_none_without_city_or_postal_code(self):
        parser = LaforetParser()
        assert parser.build_search_url({}) is None
        assert parser.build_search_url({"city": "Paris"}) is None

    def test_rent_apartment_url_includes_merge_filters(self):
        parser = LaforetParser()
        url = parser.build_search_url({"city": "Paris", "postalCode": "75018"})
        assert url == (
            "https://www.laforet.com/ville/location-appartement-paris-75018"
            "?filter%5Btypes%5D%5B%5D=apartment&filter%5Bcities%5D%5B%5D=75118"
        )

    def test_sale_house_url(self):
        parser = LaforetParser()
        url = parser.build_search_url({
            "city": "Lyon", "postalCode": "69007",
            "distributionTypes": ["Sale"], "estateTypes": ["House"],
        })
        assert url == (
            "https://www.laforet.com/ville/achat-maison-lyon-69007"
            "?filter%5Btypes%5D%5B%5D=house&filter%5Bcities%5D%5B%5D=69387"
        )

    def test_unsupported_estate_type_raises(self):
        parser = LaforetParser()
        with pytest.raises(ValueError):
            parser.build_search_url({
                "city": "Paris", "postalCode": "75018", "estateTypes": ["Parking"],
            })

    def test_unresolvable_location_falls_back_to_a_plain_url(self):
        """A postal code that can't be resolved to an INSEE code (unknown
        geo API, foreign postal code) gets a plain link instead — matching
        scrape()'s own per-location fallback, not a broken merged link."""
        parser = LaforetParser()
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            url = parser.build_search_url({"city": "Paris", "postalCode": "75018"})
        assert url == "https://www.laforet.com/ville/location-appartement-paris-75018"
        assert "filter" not in url

    def test_build_search_url_returns_the_merged_url_for_several_locations(self):
        parser = LaforetParser()
        url = parser.build_search_url({"locations": [
            {"city": "Paris", "postalCode": "75014"},
            {"city": "Lyon", "postalCode": "69007"},
        ]})
        assert url == (
            "https://www.laforet.com/ville/location-appartement-paris-75014"
            "?filter%5Btypes%5D%5B%5D=apartment&filter%5Bcities%5D%5B%5D=75114&filter%5Bcities%5D%5B%5D=69387"
        )


class TestBuildSearchUrls:
    """A search can span several cities/postal codes — reflects the real
    scraping strategy: resolvable locations become one merged URL (what
    "Voir l'URL" should show, since that's what's actually fetched), only
    an unresolvable one gets its own separate plain URL."""

    def test_resolvable_locations_produce_one_merged_url(self):
        parser = LaforetParser()
        urls = parser.build_search_urls({"locations": [
            {"city": "Paris", "postalCode": "75014"},
            {"city": "Lyon", "postalCode": "69007"},
        ]})
        assert len(urls) == 1
        assert urls[0].startswith("https://www.laforet.com/ville/location-appartement-paris-75014?")
        assert "filter%5Bcities%5D%5B%5D=75114" in urls[0]
        assert "filter%5Bcities%5D%5B%5D=69387" in urls[0]

    def test_single_legacy_location_still_works(self):
        parser = LaforetParser()
        urls = parser.build_search_urls({"city": "Paris", "postalCode": "75018"})
        assert len(urls) == 1
        assert urls[0].startswith("https://www.laforet.com/ville/location-appartement-paris-75018?")

    def test_unresolvable_location_gets_its_own_plain_url(self):
        parser = LaforetParser()
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            urls = parser.build_search_urls({"city": "Paris", "postalCode": "75018"})
        assert urls == ["https://www.laforet.com/ville/location-appartement-paris-75018"]

    def test_mix_of_resolvable_and_unresolvable_locations(self):
        parser = LaforetParser()
        with patch("parsers.laforet._resolve_insee_code", side_effect=_arrondissement_insee_code):
            urls = parser.build_search_urls({"locations": [
                {"city": "Paris", "postalCode": "75014"},   # resolves
                {"city": "Nawak", "postalCode": "99999"},   # doesn't
            ]})
        assert len(urls) == 2
        assert "filter%5Bcities%5D%5B%5D=75114" in urls[0]
        assert urls[1] == "https://www.laforet.com/ville/location-appartement-nawak-99999"

    def test_empty_without_any_location(self):
        assert LaforetParser().build_search_urls({}) == []


class TestHasValidCriteria:
    def test_requires_city_and_postal_code(self):
        parser = LaforetParser()
        assert parser.has_valid_criteria({"city": "Paris", "postalCode": "75018"})
        assert not parser.has_valid_criteria({"city": "Paris"})
        assert not parser.has_valid_criteria({})

    def test_multiple_locations(self):
        parser = LaforetParser()
        assert parser.has_valid_criteria({"locations": [
            {"city": "Paris", "postalCode": "75014"},
            {"city": "Lyon", "postalCode": "69007"},
        ]})


class TestArrondissementInseeCode:
    """Paris/Lyon/Marseille arrondissements need a special code Laforet's
    filter[cities][] expects — formulas verified against Laforet's own
    embedded page state (see parsers/laforet.py module docstring), not
    guessed: 75014->75114, 69007->69387, 13001->13201, etc."""

    def test_paris(self):
        assert _arrondissement_insee_code("75014") == "75114"
        assert _arrondissement_insee_code("75001") == "75101"
        assert _arrondissement_insee_code("75020") == "75120"

    def test_lyon(self):
        assert _arrondissement_insee_code("69001") == "69381"
        assert _arrondissement_insee_code("69007") == "69387"
        assert _arrondissement_insee_code("69009") == "69389"

    def test_marseille(self):
        assert _arrondissement_insee_code("13001") == "13201"
        assert _arrondissement_insee_code("13008") == "13208"
        assert _arrondissement_insee_code("13016") == "13216"

    def test_non_special_cased_postal_code_returns_none(self):
        assert _arrondissement_insee_code("86000") is None
        assert _arrondissement_insee_code("44000") is None

    def test_out_of_range_or_malformed_returns_none(self):
        assert _arrondissement_insee_code("75000") is None
        assert _arrondissement_insee_code("7500") is None
        assert _arrondissement_insee_code("abcde") is None


class TestResolveInseeCode:
    def test_special_case_never_hits_the_network(self):
        with patch("parsers.laforet.requests.get") as mock_get:
            code = _resolve_insee_code("75014")
        assert code == "75114"
        mock_get.assert_not_called()

    def test_general_case_uses_the_public_geo_api(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{"code": "86194"}]
        resp.raise_for_status.return_value = None
        with patch("parsers.laforet.requests.get", return_value=resp) as mock_get:
            code = _resolve_insee_code("86000")
        assert code == "86194"
        mock_get.assert_called_once()

    def test_result_is_cached(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{"code": "86194"}]
        resp.raise_for_status.return_value = None
        with patch("parsers.laforet.requests.get", return_value=resp) as mock_get:
            _resolve_insee_code("86000")
            _resolve_insee_code("86000")
        mock_get.assert_called_once()

    def test_returns_none_on_network_error(self):
        with patch("parsers.laforet.requests.get", side_effect=Exception("boom")):
            assert _resolve_insee_code("99999") is None

    def test_returns_none_when_no_commune_matches(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = []
        resp.raise_for_status.return_value = None
        with patch("parsers.laforet.requests.get", return_value=resp):
            assert _resolve_insee_code("99999") is None


class TestExtractGenuineSection:
    """Laforet always appends a second "Appartements à proximité de {ville}"
    section with backfill noise after the real results — must never be
    parsed as part of the results (verified live: this is what let listings
    from unrelated cities contaminate a search)."""

    def test_truncates_before_the_nearby_marker(self):
        html = "GENUINEproximité de Paris 14</div>NOISE"
        assert _extract_genuine_section(html) == "GENUINE"

    def test_returns_whole_html_when_marker_absent(self):
        html = "<html>no marker here, plenty of native inventory</html>"
        assert _extract_genuine_section(html) == html


class TestParseTotalPages:
    def test_reads_last_position_from_itemlist(self):
        assert _parse_total_pages(SAMPLE_PAGE_HTML) == 3

    def test_defaults_to_one_page_without_itemlist(self):
        assert _parse_total_pages("<html><body>no ld+json here</body></html>") == 1


class TestParseCards:
    def test_extracts_expected_fields(self):
        cards = _parse_cards(SAMPLE_PAGE_HTML)
        assert len(cards) == 2
        assert cards[0]["reference"] == "52811904"
        assert cards[0]["city"] == "PARIS"
        assert cards[0]["zip_code"] == "75018"
        assert cards[0]["surface"] == "30"
        assert cards[0]["rooms"] == "1"
        assert cards[0]["price_value"] == 1021.0
        assert cards[0]["agency"] == "paris18marxdormoy"

    def test_second_card_handles_plural_pieces(self):
        cards = _parse_cards(SAMPLE_PAGE_HTML)
        assert cards[1]["rooms"] == "2"
        assert cards[1]["price_value"] == 1513.0

    def test_ignores_nearby_agency_office_cards(self):
        """A nearby-agency-office filler card must never be mistaken for a
        listing, even though its own link ends in "-<digit>" too (verified
        live against Lyon: this produced a fake "listing" that was just a
        link to the agency's own page, with no price/surface/location)."""
        cards = _parse_cards(PAGE_WITH_AGENCY_OFFICE_CARD)
        assert cards == []


class TestDictToListing:
    def test_maps_to_common_listing_schema(self):
        cards = _parse_cards(SAMPLE_PAGE_HTML)
        listing = _dict_to_listing(cards[0], "Appartement")
        assert listing.listing_id == "lf_52811904"
        assert listing.source == "laforet"
        assert listing.city == "PARIS"
        assert listing.zip_code == "75018"
        assert listing.price_value == 1021.0


class TestPassesFilters:
    def _listing(self, **overrides):
        base = dict(
            listing_id="lf_1", url="https://x", price_value=1000.0,
            surface="50", rooms="2", zip_code="75014",
        )
        base.update(overrides)
        return Listing(**base)

    def test_price_range(self):
        listing = self._listing(price_value=1000.0)
        allowed = {"75014"}
        assert _passes_filters(listing, {"priceMin": 900, "priceMax": 1100}, allowed)
        assert not _passes_filters(listing, {"priceMax": 900}, allowed)
        assert not _passes_filters(listing, {"priceMin": 1100}, allowed)

    def test_surface_range(self):
        listing = self._listing(surface="50")
        allowed = {"75014"}
        assert _passes_filters(listing, {"spaceMin": 40, "spaceMax": 60}, allowed)
        assert not _passes_filters(listing, {"spaceMin": 60}, allowed)

    def test_rooms_exact_match(self):
        listing = self._listing(rooms="2")
        allowed = {"75014"}
        assert _passes_filters(listing, {"rooms": ["2", "3"]}, allowed)
        assert not _passes_filters(listing, {"rooms": ["3", "4"]}, allowed)

    def test_rooms_five_plus(self):
        listing = self._listing(rooms="6")
        assert _passes_filters(listing, {"rooms": ["5"]}, {"75014"})

    def test_missing_data_does_not_exclude(self):
        listing = self._listing(price_value=None, surface="", rooms="")
        assert _passes_filters(listing, {"priceMin": 900, "spaceMin": 40, "rooms": ["2"]}, {"75014"})

    def test_postal_code_must_be_in_allowed_set(self):
        listing = self._listing(zip_code="75014")
        assert _passes_filters(listing, {}, {"75014"})
        assert not _passes_filters(listing, {}, {"75015"})
        assert not _passes_filters(listing, {}, {"94230"})

    def test_multiple_allowed_postal_codes(self):
        """A merged multi-location search allows any of several postal codes."""
        allowed = {"75014", "92120"}
        assert _passes_filters(self._listing(zip_code="75014"), {}, allowed)
        assert _passes_filters(self._listing(zip_code="92120"), {}, allowed)
        assert not _passes_filters(self._listing(zip_code="75015"), {}, allowed)

    def test_missing_zip_code_excludes(self):
        """Fail closed, not open: unlike price/surface/rooms, location
        correctness can't be waived just because a card's postal code
        couldn't be parsed — verified live this is exactly how a fake
        agency-office "listing" (no zip_code at all) slipped through."""
        listing = self._listing(zip_code="")
        assert not _passes_filters(listing, {}, {"75014"})


class TestScrape:
    """_resolve_insee_code is patched to the pure arrondissement formula in
    most of these (no network, deterministic) — postal codes outside
    Paris/Lyon/Marseille resolve to None, taking the per-location fallback
    path, exactly like a real geo-API miss would."""

    def test_raises_on_invalid_location(self):
        parser = LaforetParser()
        with pytest.raises(ValueError):
            parser.scrape({})

    def test_scrape_paginates_and_dedupes(self):
        parser = LaforetParser()
        page1_resp = MagicMock(status_code=200, text=SAMPLE_PAGE_HTML)
        page1_resp.raise_for_status.return_value = None

        with patch("parsers.laforet._parse_total_pages", return_value=1):
            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.get.return_value = page1_resp
                mock_session_cls.return_value = mock_session
                listings = parser.scrape({"city": "Paris", "postalCode": "75018"})

        # SAMPLE_PAGE_HTML has one card in 75018 and one in 75015 — the
        # strict postal-code filter must keep only the requested one.
        assert len(listings) == 1
        assert listings[0].listing_id == "lf_52811904"

    def test_scrape_raises_clear_error_on_404(self):
        parser = LaforetParser()
        resp = MagicMock(status_code=404)
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.get.return_value = resp
                mock_session_cls.return_value = mock_session
                with pytest.raises(ValueError):
                    parser.scrape({"city": "Nawak", "postalCode": "99999"})

    def test_scrape_merges_multiple_locations_into_one_request(self):
        """Verified live: filter[cities][] genuinely merges several
        cities/postal codes into one correctly-scoped request+pagination —
        this must result in exactly one HTTP call, not one per location."""
        parser = LaforetParser()
        resp = MagicMock(status_code=200, text=MERGED_PAGE_HTML)
        resp.raise_for_status.return_value = None

        with patch("parsers.laforet._parse_total_pages", return_value=1):
            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.get.return_value = resp
                mock_session_cls.return_value = mock_session
                listings = parser.scrape({"locations": [
                    {"city": "Paris", "postalCode": "75018"},
                    {"city": "Lyon", "postalCode": "69007"},
                ]})

        zip_codes = {l.zip_code for l in listings}
        assert zip_codes == {"75018", "69007"}
        assert len(listings) == 2
        assert mock_session.get.call_count == 1

    def test_merged_request_sends_insee_codes_and_type_filter(self):
        parser = LaforetParser()
        resp = MagicMock(status_code=200, text=MERGED_PAGE_HTML)
        resp.raise_for_status.return_value = None

        with patch("parsers.laforet._parse_total_pages", return_value=1):
            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.get.return_value = resp
                mock_session_cls.return_value = mock_session
                parser.scrape({"locations": [
                    {"city": "Paris", "postalCode": "75018"},
                    {"city": "Lyon", "postalCode": "69007"},
                ]})

        args, kwargs = mock_session.get.call_args
        assert args[0] == "https://www.laforet.com/ville/location-appartement-paris-75018"
        query_pairs = kwargs["params"]
        assert ("filter[types][]", "apartment") in query_pairs
        assert ("filter[cities][]", "75118") in query_pairs
        assert ("filter[cities][]", "69387") in query_pairs

    def test_falls_back_to_per_location_when_insee_resolution_fails(self):
        """A location whose postal code can't be resolved to an INSEE code
        (unknown geo API, foreign postal code) must not be silently
        dropped — it gets its own separate request instead."""
        parser = LaforetParser()
        page1_resp = MagicMock(status_code=200, text=SAMPLE_PAGE_HTML)
        page1_resp.raise_for_status.return_value = None

        with patch("parsers.laforet._resolve_insee_code", side_effect=_arrondissement_insee_code):
            with patch("parsers.laforet._parse_total_pages", return_value=1):
                with patch("requests.Session") as mock_session_cls:
                    mock_session = MagicMock()
                    mock_session.get.return_value = page1_resp
                    mock_session_cls.return_value = mock_session
                    listings = parser.scrape({"locations": [
                        {"city": "Poitiers", "postalCode": "86000"},  # not special-cased -> None here
                    ]})

        assert len(listings) == 0  # SAMPLE_PAGE_HTML has no 86000 card
        # falls back to _scrape_location (single-URL string, no params=)
        args, kwargs = mock_session.get.call_args
        assert "params" not in kwargs
        assert "poitiers-86000" in args[0]

    def test_rejected_card_in_fallback_path_is_not_blacklisted_from_another_location(self):
        """Real bug found live: a listing rejected as backfill noise while
        scraping one (fallback) location's own page must not be
        permanently blacklisted from being counted on a different
        location's page later in the same scrape."""
        parser = LaforetParser()

        loc_a_resp = MagicMock(status_code=200, text=SAMPLE_PAGE_HTML)
        loc_a_resp.raise_for_status.return_value = None
        loc_b_html = """
        <html><body>
        <article>
          <a href="https://www.laforet.com/agence-immobiliere/paris15lourmel/louer/paris-15/appartement-2-pieces-52805433" target="_blank">photo</a>
          <h3>Appartement <span>1 513 &#8364;/mois</span> <span>PARIS (75015)</span></h3>
          <div>50 m²&nbsp;&bull;&nbsp;2 pi&egrave;ces</div>
        </article>
        </body></html>
        """
        loc_b_resp = MagicMock(status_code=200, text=loc_b_html)
        loc_b_resp.raise_for_status.return_value = None

        def fake_get(url, timeout=15):
            return loc_a_resp if "75014" in url else loc_b_resp

        # Force both locations through the per-location fallback path (as if
        # neither resolved to an INSEE code) so this exercises _scrape_location.
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            with patch("parsers.laforet._parse_total_pages", return_value=1):
                with patch("requests.Session") as mock_session_cls:
                    mock_session = MagicMock()
                    mock_session.get.side_effect = fake_get
                    mock_session_cls.return_value = mock_session
                    listings = parser.scrape({"locations": [
                        {"city": "Paris", "postalCode": "75014"},
                        {"city": "Paris", "postalCode": "75015"},
                    ]})

        assert len(listings) == 1
        assert listings[0].listing_id == "lf_52805433"
        assert listings[0].zip_code == "75015"

    def test_scrape_one_bad_location_does_not_lose_the_others(self):
        """A 404 on one city/postal code must not discard results already
        found for the others in the same multi-location search."""
        parser = LaforetParser()

        paris_resp = MagicMock(status_code=200, text=SAMPLE_PAGE_HTML)
        paris_resp.raise_for_status.return_value = None
        not_found_resp = MagicMock(status_code=404)

        def fake_get(url, timeout=15):
            return not_found_resp if "nawak" in url else paris_resp

        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            with patch("parsers.laforet._parse_total_pages", return_value=1):
                with patch("requests.Session") as mock_session_cls:
                    mock_session = MagicMock()
                    mock_session.get.side_effect = fake_get
                    mock_session_cls.return_value = mock_session
                    listings = parser.scrape({"locations": [
                        {"city": "Paris", "postalCode": "75018"},
                        {"city": "Nawak", "postalCode": "99999"},
                    ]})

        assert len(listings) == 1
        assert listings[0].zip_code == "75018"

    def test_scrape_raises_only_when_every_location_fails(self):
        parser = LaforetParser()
        resp = MagicMock(status_code=404)
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.get.return_value = resp
                mock_session_cls.return_value = mock_session
                with pytest.raises(ValueError):
                    parser.scrape({"locations": [
                        {"city": "Nawak", "postalCode": "99999"},
                        {"city": "Bidule", "postalCode": "88888"},
                    ]})

    def test_merged_request_failure_falls_back_to_per_location(self):
        """If the combined multi-location request itself fails (network
        error, unexpected response), every location that was part of it
        must still get its own separate attempt rather than being lost."""
        parser = LaforetParser()

        def side_effect(*args, **kwargs):
            if "params" in kwargs:
                raise ConnectionError("merged request boom")
            resp = MagicMock(status_code=200, text=SAMPLE_PAGE_HTML)
            resp.raise_for_status.return_value = None
            return resp

        with patch("parsers.laforet._parse_total_pages", return_value=1):
            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.get.side_effect = side_effect
                mock_session_cls.return_value = mock_session
                listings = parser.scrape({"city": "Paris", "postalCode": "75018"})

        # Merged attempt (with params=) raised, fell back to per-location
        # (no params=) which succeeds via SAMPLE_PAGE_HTML.
        assert len(listings) == 1
        assert listings[0].zip_code == "75018"
