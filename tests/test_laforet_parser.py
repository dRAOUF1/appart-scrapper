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


class TestHasValidCriteria:
    def test_requires_city_and_postal_code(self):
        parser = LaforetParser()
        assert parser.has_valid_criteria({"city": "Paris", "postalCode": "75018"})
        assert not parser.has_valid_criteria({"city": "Paris"})
        assert not parser.has_valid_criteria({})


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


class TestScrape:
    def test_raises_on_invalid_location(self):
        parser = LaforetParser()
        with pytest.raises(ValueError):
            parser.scrape({})

    def test_scrape_paginates_and_dedupes(self):
        parser = LaforetParser()
        page1_resp = MagicMock(status_code=200, text=SAMPLE_PAGE_HTML)
        page1_resp.raise_for_status.return_value = None
        page2_resp = MagicMock(status_code=200, text="<html><body>no more cards</body></html>")
        page2_resp.raise_for_status.return_value = None

        with patch("parsers.laforet._parse_total_pages", return_value=1):
            with patch.object(parser, "build_search_url", return_value="https://www.laforet.com/ville/x"):
                with patch("requests.Session") as mock_session_cls:
                    mock_session = MagicMock()
                    mock_session.get.return_value = page1_resp
                    mock_session_cls.return_value = mock_session
                    listings = parser.scrape({"city": "Paris", "postalCode": "75018"})

        assert len(listings) == 2
        assert {l.listing_id for l in listings} == {"lf_52811904", "lf_52805433"}

    def test_scrape_raises_clear_error_on_404(self):
        parser = LaforetParser()
        resp = MagicMock(status_code=404)
        with patch.object(parser, "build_search_url", return_value="https://www.laforet.com/ville/x"):
            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.get.return_value = resp
                mock_session_cls.return_value = mock_session
                with pytest.raises(ValueError):
                    parser.scrape({"city": "Nawak", "postalCode": "99999"})
