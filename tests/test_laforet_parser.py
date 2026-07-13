"""Tests for parsers/laforet.py."""
from unittest.mock import MagicMock, patch

import pytest

from models.listing import Listing
from parsers.laforet import (
    LaforetParser,
    _dict_to_listing,
    _parse_cards,
    _parse_total_pages,
    _passes_filters,
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


class TestSlugify:
    def test_lowercases_and_strips_accents(self):
        assert _slugify("Paris") == "paris"
        assert _slugify("Le Kremlin-Bicêtre") == "le-kremlin-bicetre"
        assert _slugify("Île-de-France") == "ile-de-france"

    def test_collapses_non_alnum_to_single_hyphen(self):
        assert _slugify("Charenton-le-Pont") == "charenton-le-pont"
        assert _slugify("  Saint  Mandé  ") == "saint-mande"


class TestBuildSearchUrl:
    def test_returns_none_without_city_or_postal_code(self):
        parser = LaforetParser()
        assert parser.build_search_url({}) is None
        assert parser.build_search_url({"city": "Paris"}) is None

    def test_rent_apartment_url(self):
        parser = LaforetParser()
        url = parser.build_search_url({"city": "Paris", "postalCode": "75018"})
        assert url == "https://www.laforet.com/ville/location-appartement-paris-75018"

    def test_sale_house_url(self):
        parser = LaforetParser()
        url = parser.build_search_url({
            "city": "Lyon", "postalCode": "69000",
            "distributionTypes": ["Sale"], "estateTypes": ["House"],
        })
        assert url == "https://www.laforet.com/ville/achat-maison-lyon-69000"

    def test_unsupported_estate_type_raises(self):
        parser = LaforetParser()
        with pytest.raises(ValueError):
            parser.build_search_url({
                "city": "Paris", "postalCode": "75018", "estateTypes": ["Parking"],
            })

    def test_never_appends_filter_query_params(self):
        """Verified live: filter[min]/filter[max]/filter[surface] query
        params silently break Laforet's city scoping (results become a
        nationwide feed instead of staying scoped to the requested city) —
        so they must never be sent. Filtering is enforced client-side by
        _passes_filters() instead."""
        parser = LaforetParser()
        url = parser.build_search_url({
            "city": "Paris", "postalCode": "75018",
            "priceMin": 600, "priceMax": 850, "spaceMin": 20, "rooms": ["2"],
        })
        assert url == "https://www.laforet.com/ville/location-appartement-paris-75018"
        assert "filter" not in url

    def test_build_search_url_returns_first_of_several_locations(self):
        parser = LaforetParser()
        url = parser.build_search_url({"locations": [
            {"city": "Paris", "postalCode": "75014"},
            {"city": "Lyon", "postalCode": "69007"},
        ]})
        assert url == "https://www.laforet.com/ville/location-appartement-paris-75014"


class TestBuildSearchUrls:
    """A search can span several cities/postal codes — one URL per location."""

    def test_one_url_per_location(self):
        parser = LaforetParser()
        urls = parser.build_search_urls({"locations": [
            {"city": "Paris", "postalCode": "75014"},
            {"city": "Lyon", "postalCode": "69007"},
        ]})
        assert urls == [
            "https://www.laforet.com/ville/location-appartement-paris-75014",
            "https://www.laforet.com/ville/location-appartement-lyon-69007",
        ]

    def test_single_legacy_location_still_works(self):
        parser = LaforetParser()
        assert parser.build_search_urls({"city": "Paris", "postalCode": "75018"}) == [
            "https://www.laforet.com/ville/location-appartement-paris-75018"
        ]

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
            surface="50", rooms="2",
        )
        base.update(overrides)
        return Listing(**base)

    def test_price_range(self):
        listing = self._listing(price_value=1000.0)
        assert _passes_filters(listing, {"priceMin": 900, "priceMax": 1100})
        assert not _passes_filters(listing, {"priceMax": 900})
        assert not _passes_filters(listing, {"priceMin": 1100})

    def test_surface_range(self):
        listing = self._listing(surface="50")
        assert _passes_filters(listing, {"spaceMin": 40, "spaceMax": 60})
        assert not _passes_filters(listing, {"spaceMin": 60})

    def test_rooms_exact_match(self):
        listing = self._listing(rooms="2")
        assert _passes_filters(listing, {"rooms": ["2", "3"]})
        assert not _passes_filters(listing, {"rooms": ["3", "4"]})

    def test_rooms_five_plus(self):
        listing = self._listing(rooms="6")
        assert _passes_filters(listing, {"rooms": ["5"]})

    def test_missing_data_does_not_exclude(self):
        listing = self._listing(price_value=None, surface="", rooms="")
        assert _passes_filters(listing, {"priceMin": 900, "spaceMin": 40, "rooms": ["2"]})

    def test_postal_code_exact_match_required(self):
        """Laforet's /ville/{postalCode} page isn't scoped to that exact
        postal code — it backfills from neighboring areas (verified live:
        for Paris 75014, only 1 of 41 returned listings was actually in
        75014). A search for one postal code must exclude every other one."""
        listing = self._listing(zip_code="75014")
        assert _passes_filters(listing, {"postalCode": "75014"})
        assert not _passes_filters(listing, {"postalCode": "75015"})
        assert not _passes_filters(listing, {"postalCode": "94230"})

    def test_no_postal_code_filter_when_not_requested(self):
        listing = self._listing(zip_code="94230")
        assert _passes_filters(listing, {})

    def test_missing_zip_code_excludes_when_postal_code_requested(self):
        """Fail closed, not open: unlike price/surface/rooms, location
        correctness can't be waived just because a card's postal code
        couldn't be parsed — verified live this is exactly how a fake
        agency-office "listing" (no zip_code at all) slipped through."""
        listing = self._listing(zip_code="")
        assert not _passes_filters(listing, {"postalCode": "75014"})


class TestScrape:
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
        with patch("requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.return_value = resp
            mock_session_cls.return_value = mock_session
            with pytest.raises(ValueError):
                parser.scrape({"city": "Nawak", "postalCode": "99999"})

    def test_scrape_aggregates_across_multiple_locations(self):
        """A search covering several cities/postal codes must return
        listings from all of them, each filtered against its own postal
        code (not whichever location happens to be first)."""
        parser = LaforetParser()

        paris_resp = MagicMock(status_code=200, text=SAMPLE_PAGE_HTML)
        paris_resp.raise_for_status.return_value = None
        lyon_html = SAMPLE_PAGE_HTML.replace("75018", "69007").replace("52811904", "99999999")
        lyon_resp = MagicMock(status_code=200, text=lyon_html)
        lyon_resp.raise_for_status.return_value = None

        def fake_get(url, timeout=15):
            return lyon_resp if "lyon" in url else paris_resp

        with patch("parsers.laforet._parse_total_pages", return_value=1):
            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.get.side_effect = fake_get
                mock_session_cls.return_value = mock_session
                listings = parser.scrape({"locations": [
                    {"city": "Paris", "postalCode": "75018"},
                    {"city": "Lyon", "postalCode": "69007"},
                ]})

        zip_codes = {l.zip_code for l in listings}
        assert zip_codes == {"75018", "69007"}
        assert len(listings) == 2

    def test_rejected_backfill_card_is_not_blacklisted_from_its_own_location(self):
        """Real bug found live: Laforet backfills a location's page with
        listings from neighboring postal codes. Those get correctly
        rejected there (wrong postal code) — but they must not be marked
        "seen" from that rejection, or the SAME listing silently vanishes
        when its own location is scraped later in the same multi-location
        search (verified live: Paris 75015/75013 listings backfilled onto
        the 75014 page were blacklisted before their own page was scraped,
        dropping 12 of 13 real results down to 1)."""
        parser = LaforetParser()

        # 75014's own page: both cards (75018, 75015) are backfill noise —
        # neither matches 75014, both get rejected, including reference
        # 52805433 which is the *real* 75015 listing scraped further down.
        loc_a_resp = MagicMock(status_code=200, text=SAMPLE_PAGE_HTML)
        loc_a_resp.raise_for_status.return_value = None

        # 75015's own page: the same reference (52805433) reappears, this
        # time as its legitimate match.
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
        with patch("requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.return_value = resp
            mock_session_cls.return_value = mock_session
            with pytest.raises(ValueError):
                parser.scrape({"locations": [
                    {"city": "Nawak", "postalCode": "99999"},
                    {"city": "Bidule", "postalCode": "88888"},
                ]})
