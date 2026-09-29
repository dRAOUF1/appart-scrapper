"""Non-régressions du tri, de la pagination et du rendu Citya."""

from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup

import parsers.citya as citya
from parsers.citya import CityaParser, _native_filters, _search_url
from tests.helpers.factories import (
    make_city_location,
    make_criteria,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)

TOULOUSE = make_city_location(city="Toulouse", postal_code="31000", insee="31555")


def _criteria(**overrides):
    values = make_criteria(
        locations=[TOULOUSE],
        transaction="rent",
        propertyTypes=["apartment"],
        sourceOverrides={"citya": {"slugs": ["toulouse-31555"]}},
    )
    values.update(overrides)
    return values


def test_native_filters_force_newest_first_after_user_filters():
    assert _native_filters({"priceMax": 900, "surfaceMin": 20, "rooms": [2]}) == [
        ("prixMax", "900"),
        ("surfaceMin", "20"),
        ("nbrePiecesMin", "2"),
        ("sort", "b.dateCreation"),
        ("direction", "desc"),
    ]


def test_every_page_url_keeps_the_descending_date_sort():
    url = _search_url("toulouse-31555", "appartement", {}, page=2)

    assert "page=2" in url
    assert "sort=b.dateCreation" in url
    assert "direction=desc" in url


def test_an_explicit_zero_result_page_is_legitimately_empty(monkeypatch):
    parser = CityaParser()
    monkeypatch.setattr(
        parser,
        "_fetch_page",
        lambda session, url: BeautifulSoup("<main>0 résultat</main>", "lxml"),
    )

    assert parser.scrape(_criteria()) == []


def test_an_unexplained_empty_first_page_is_an_error(monkeypatch):
    parser = CityaParser()
    monkeypatch.setattr(parser, "_fetch_page", lambda session, url: BeautifulSoup("<main></main>", "lxml"))

    with pytest.raises(ValueError, match="HTTP 200 sans carte"):
        parser.scrape(_criteria())


def test_the_safety_cap_is_thirty_pages():
    assert citya.MAX_PAGES == 30


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1 453,5", 1453.5), (12, 12.0), (None, None), ("abc", None)],
)
def test_float_and_price_formatting(value, expected):
    assert citya._as_float(value) == expected
    if expected is not None:
        assert citya._format_price(expected).endswith(" €")
    else:
        assert citya._format_price(expected) == ""


def test_transaction_types_rooms_and_native_filters_edge_cases():
    assert citya._transaction({}) == "rent"
    assert citya._transaction({"transaction": "buy"}) == "rent"
    assert citya._property_types({}) == ["apartment"]
    assert citya._property_types({"propertyTypes": ["house", "castle"]}) == ["house"]
    assert citya._property_types({"propertyTypes": ["castle"]}) == []
    assert citya._allowed_rooms(["2", 5, 8, 0, "x"]) == ({2}, True)
    assert citya._rooms_min([3, 5]) == 3
    assert citya._rooms_min([5]) == 5
    assert citya._rooms_min([]) is None
    assert citya._native_filters({"rooms": [1]})[-2:] == [("sort", "b.dateCreation"), ("direction", "desc")]


def _card_html(**overrides):
    values = {
        "attrs": (
            'data-itemid="GES-1" data-itemname="Appartement 3 pièces 65,5 m²" '
            'data-category="Appartement" data-price="1250"'
        ),
        "link": '<a href="/annonces/GES-1">Voir</a>',
        "body": 'Appartement Toulouse (31000) Meublé <img src="/media/images/bien.webp">',
    }
    values.update(overrides)
    return f'<div class="property-card" {values["attrs"]}>{values["link"]}{values["body"]}</div>'


def test_parse_place_and_full_card():
    assert citya._parse_place("Titre 75013 puis Paris (75013)") == ("Titre 75013 puis Paris", "75013")
    assert citya._parse_place("aucun lieu") == ("", "")
    card = BeautifulSoup(_card_html(), "lxml").select_one(".property-card")

    listing = citya._parse_card(card)

    assert listing.listing_id == "citya_GES-1"
    assert listing.url.endswith("/annonces/GES-1")
    assert listing.price_value == 1250
    assert listing.surface == "65.5"
    assert listing.rooms == "3"
    assert listing.zip_code == "31000"
    assert listing.headline == "Meublé"
    assert listing.image_url.endswith("/media/images/bien.webp")
    assert listing.creation_date == citya.DATE_INCONNUE


def test_parse_card_rejects_missing_identity_or_link_and_tolerates_sparse_fields():
    no_id = BeautifulSoup('<div class="property-card"><a href="/x">x</a></div>', "lxml").div
    no_link = BeautifulSoup('<div class="property-card" data-itemid="x"></div>', "lxml").div
    assert citya._parse_card(no_id) is None
    assert citya._parse_card(no_link) is None
    sparse = BeautifulSoup(
        _card_html(attrs='data-itemid="x" data-itemname="Studio" data-price="abc"', body="Sans ville"),
        "lxml",
    ).div
    listing = citya._parse_card(sparse)
    assert listing.price == ""
    assert listing.surface == ""
    assert listing.rooms == ""
    assert listing.photos == "[]"


@pytest.mark.parametrize(
    ("criteria", "expected"),
    [
        ({}, True),
        ({"priceMin": 1300}, False),
        ({"priceMax": 1200}, False),
        ({"surfaceMin": 70}, False),
        ({"surfaceMax": 60}, False),
        ({"rooms": [2]}, False),
        ({"rooms": [3]}, True),
        ({"rooms": [5]}, False),
    ],
)
def test_local_filters(criteria, expected):
    card = BeautifulSoup(_card_html(), "lxml").div
    assert citya._passes_filters(citya._parse_card(card), criteria, [TOULOUSE]) is expected


def test_local_filters_reject_out_of_scope_and_keep_unreadable_numbers():
    listing = citya._parse_card(BeautifulSoup(_card_html(), "lxml").div)
    assert not citya._passes_filters(listing, {}, [make_city_location(postal_code="44000")])
    listing.surface = "abc"
    listing.rooms = "abc"
    assert citya._passes_filters(listing, {"surfaceMin": 100, "rooms": [2]}, [TOULOUSE])


def test_covered_by_uses_hierarchical_postal_prefixes():
    assert citya._covered_by(["75013"], [["75"]])
    assert not citya._covered_by([], [["75"]])
    assert not citya._covered_by(["75013"], [["69"]])


def test_manual_resolution_and_parser_metadata():
    parser = CityaParser()
    locations = [TOULOUSE, make_department_location(code="31")]
    criteria = {"locations": locations, "sourceOverrides": {"citya": {"slugs": ["toulouse", "dept-31"]}}}
    assert parser._resolved_locations(criteria, locations) == list(
        zip(locations, ["toulouse", "dept-31"], strict=True)
    )
    assert parser.parse_manual_override(" a, b ,, ") == {"slugs": ["a", "b"]}
    assert parser.parse_manual_override(" ") == {}
    assert parser.to_native(criteria) is criteria
    assert parser.has_valid_criteria(criteria)
    assert not parser.has_valid_criteria({})


def test_automatic_resolution_deduplicates_slugs_and_skips_failures(monkeypatch):
    repo = MagicMock()
    parser = CityaParser(storage=MagicMock(citya_geo=repo))
    locations = [TOULOUSE, make_city_location(city="Autre", postal_code="44000", insee="44109"), TOULOUSE]
    answers = iter(["slug-commun", None, "slug-commun"])
    monkeypatch.setattr("services.citya_geocode.resolve_slug_id", lambda location, repo: next(answers))
    assert parser._resolved_locations({"locations": locations}, locations) == [(TOULOUSE, "slug-commun")]


def test_resolution_without_storage_and_manual_memory_fail_closed(monkeypatch):
    parser = CityaParser()
    assert parser._resolved_locations({"locations": [TOULOUSE]}, [TOULOUSE]) == []
    parser.remember_manual_override(_criteria())

    repo = MagicMock()
    parser = CityaParser(storage=MagicMock(citya_geo=repo))
    remember = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr("services.citya_geocode.remember_manual_slugs", remember)
    parser.remember_manual_override(_criteria())
    remember.assert_called_once()


def test_query_targets_composes_regular_scopes_and_keeps_uncovered_whole_city_solo():
    parser = CityaParser()
    whole = make_whole_city_location(city="Lyon", postal_codes=("69001", "69002"), insee="69123")
    resolved = [
        (TOULOUSE, "toulouse"),
        (make_department_location(code="31"), "dept-31"),
        (whole, "lyon-entier"),
    ]
    assert parser._query_targets(resolved) == ["toulouse,dept-31", "lyon-entier"]


def test_query_targets_skips_covered_or_duplicate_solos():
    parser = CityaParser()
    region = make_region_location(code="11", name="Île-de-France", departments=["75"])
    paris = make_whole_city_location(city="Paris", postal_codes=("75001", "75013"), insee="75056")
    assert parser._query_targets([(region, "idf"), (paris, "paris-75")]) == ["idf"]
    assert parser._query_targets([(TOULOUSE, "meme"), (paris, "meme")]) == ["meme"]


def test_build_urls_handles_missing_locations_types_and_multiple_types():
    parser = CityaParser()
    assert parser.build_search_urls({}) == []
    assert parser.build_search_urls({"locations": [TOULOUSE], "propertyTypes": ["castle"]}) == []
    criteria = _criteria(propertyTypes=["apartment", "house"])
    urls = parser.build_search_urls(criteria)
    assert len(urls) == 2
    assert "/appartement/" in urls[0] and "/maison/" in urls[1]
    assert parser.build_search_url(criteria) == urls[0]


def test_fetch_page_success_and_http_error(requests_mock):
    parser = CityaParser()
    url = "https://www.citya.com/test"
    requests_mock.get(url, [{"text": "<main>ok</main>"}, {"status_code": 503}])
    session = citya.requests.Session()
    assert parser._fetch_page(session, url).main.text == "ok"
    with pytest.raises(ValueError, match="Requête Citya échouée"):
        parser._fetch_page(session, url)


@pytest.mark.parametrize(
    ("criteria", "message"),
    [
        ({"transaction": "buy"}, "location"),
        ({}, "au moins une localisation"),
        ({"locations": [TOULOUSE], "propertyTypes": ["castle"]}, "aucun des types"),
    ],
)
def test_scrape_rejects_unusable_criteria(criteria, message):
    with pytest.raises(ValueError, match=message):
        CityaParser().scrape(criteria)


def test_scrape_parses_deduplicates_and_stops_on_empty_next_page(monkeypatch):
    parser = CityaParser()
    pages = iter([
        BeautifulSoup(_card_html(), "lxml"),
        BeautifulSoup(_card_html(), "lxml"),
        BeautifulSoup("<main></main>", "lxml"),
    ])
    monkeypatch.setattr(parser, "_fetch_page", lambda session, url: next(pages))
    criteria = _criteria(propertyTypes=["apartment"])

    listings = parser.scrape(criteria)

    assert [item.listing_id for item in listings] == ["citya_GES-1"]
