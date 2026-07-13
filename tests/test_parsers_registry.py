"""Tests for parsers/base.py (BaseParser contract + ParserRegistry)."""
import pytest

from models.listing import Listing
from parsers.base import BaseParser, ParserRegistry, get_locations


@pytest.fixture(autouse=True)
def _restore_registry():
    """Snapshot/restore the registry so test-only parsers don't leak
    into other tests (ParserRegistry._parsers is a shared class dict)."""
    original = dict(ParserRegistry._parsers)
    yield
    ParserRegistry._parsers.clear()
    ParserRegistry._parsers.update(original)


class TestAutoRegistration:
    def test_subclass_with_source_id_is_auto_registered(self):
        class FakeParser(BaseParser):
            SOURCE_ID = "fake_source"
            SOURCE_NAME = "Fake"
            SOURCE_DESCRIPTION = "A fake source for tests"

            def scrape(self, criteria, use_bff=True):
                return []

        assert ParserRegistry._parsers["fake_source"] is FakeParser

    def test_subclass_without_source_id_is_not_registered(self):
        before = dict(ParserRegistry._parsers)

        class NoSourceIdParser(BaseParser):
            def scrape(self, criteria, use_bff=True):
                return []

        assert ParserRegistry._parsers == before


class TestGet:
    def test_get_returns_instance_of_registered_parser(self):
        class FakeParser(BaseParser):
            SOURCE_ID = "fake_get"
            SOURCE_NAME = "Fake"

            def scrape(self, criteria, use_bff=True):
                return []

        instance = ParserRegistry.get("fake_get")
        assert isinstance(instance, FakeParser)

    def test_get_unknown_source_raises_value_error_listing_available(self):
        class FakeParser(BaseParser):
            SOURCE_ID = "known_source"
            SOURCE_NAME = "Known"

            def scrape(self, criteria, use_bff=True):
                return []

        with pytest.raises(ValueError) as exc_info:
            ParserRegistry.get("totally_unknown_source")

        assert "known_source" in str(exc_info.value)


class TestListSources:
    def test_returns_metadata_for_all_registered_parsers(self):
        class FakeParser(BaseParser):
            SOURCE_ID = "listed_source"
            SOURCE_NAME = "Listed"
            SOURCE_DESCRIPTION = "Description here"

            def scrape(self, criteria, use_bff=True):
                return []

        sources = ParserRegistry.list_sources()
        matching = [s for s in sources if s["id"] == "listed_source"]
        assert matching == [{
            "id": "listed_source", "name": "Listed", "description": "Description here",
            "requires_extra_location": False, "extra_location_label": "", "extra_location_help": "",
            "url_note": "",
        }]

    def test_seloger_is_registered_by_default(self):
        sources = ParserRegistry.list_sources()
        assert any(s["id"] == "seloger" for s in sources)

    def test_laforet_is_registered_by_default(self):
        sources = ParserRegistry.list_sources()
        assert any(s["id"] == "laforet" for s in sources)


class TestBaseParserDefaults:
    def test_parse_raises_not_implemented_by_default(self):
        class FakeParser(BaseParser):
            SOURCE_ID = "no_parse"

            def scrape(self, criteria, use_bff=True):
                return []

        with pytest.raises(NotImplementedError):
            FakeParser().parse("<html></html>")

    def test_build_search_url_returns_none_by_default(self):
        class FakeParser(BaseParser):
            SOURCE_ID = "no_url"

            def scrape(self, criteria, use_bff=True):
                return []

        assert FakeParser().build_search_url({}) is None

    def test_scrape_is_abstract_and_required(self):
        with pytest.raises(TypeError):
            class IncompleteParser(BaseParser):
                SOURCE_ID = "incomplete"

            IncompleteParser()

    def test_has_valid_criteria_default_requires_city_and_postal_code(self):
        """City + postal code is the universal location contract: any new
        source that doesn't override has_valid_criteria gets this for free."""
        class FakeParser(BaseParser):
            SOURCE_ID = "default_validity"

            def scrape(self, criteria, use_bff=True):
                return []

        parser = FakeParser()
        assert parser.has_valid_criteria({"city": "Paris", "postalCode": "75018"}) is True
        assert parser.has_valid_criteria({"city": "Paris"}) is False
        assert parser.has_valid_criteria({"anything": 1}) is False
        assert parser.has_valid_criteria({}) is False

    def test_extra_location_metadata_defaults_to_none_required(self):
        class FakeParser(BaseParser):
            SOURCE_ID = "no_extra_location"

            def scrape(self, criteria, use_bff=True):
                return []

        parser = FakeParser()
        assert parser.REQUIRES_EXTRA_LOCATION is False
        assert parser.EXTRA_LOCATION_LABEL == ""
        assert parser.EXTRA_LOCATION_HELP == ""

    def test_has_valid_criteria_default_accepts_multiple_locations(self):
        """A search can cover several cities/postal codes at once — the
        universal default must accept that too, not just a single pair."""
        class FakeParser(BaseParser):
            SOURCE_ID = "multi_location_validity"

            def scrape(self, criteria, use_bff=True):
                return []

        parser = FakeParser()
        assert parser.has_valid_criteria({
            "locations": [
                {"city": "Paris", "postalCode": "75014"},
                {"city": "Lyon", "postalCode": "69007"},
            ]
        }) is True
        assert parser.has_valid_criteria({"locations": []}) is False
        assert parser.has_valid_criteria({"locations": [{"city": "Paris"}]}) is False

    def test_build_search_urls_default_wraps_single_url(self):
        class FakeParser(BaseParser):
            SOURCE_ID = "single_url_wrap"

            def scrape(self, criteria, use_bff=True):
                return []

            def build_search_url(self, criteria):
                return "https://example.com/search"

        parser = FakeParser()
        assert parser.build_search_urls({}) == ["https://example.com/search"]

    def test_build_search_urls_default_empty_when_no_url(self):
        class FakeParser(BaseParser):
            SOURCE_ID = "no_url_wrap"

            def scrape(self, criteria, use_bff=True):
                return []

        assert FakeParser().build_search_urls({}) == []


class TestGetLocations:
    """get_locations() is the single place every location-based parser
    reads city+postalCode through — supports both a single legacy pair and
    a plural list (a search spanning several cities/postal codes)."""

    def test_legacy_flat_city_and_postal_code(self):
        assert get_locations({"city": "Paris", "postalCode": "75014"}) == [
            {"city": "Paris", "postalCode": "75014"}
        ]

    def test_plural_locations_list(self):
        criteria = {
            "locations": [
                {"city": "Paris", "postalCode": "75014"},
                {"city": "Lyon", "postalCode": "69007"},
            ]
        }
        assert get_locations(criteria) == criteria["locations"]

    def test_plural_locations_takes_precedence_over_flat_keys(self):
        criteria = {
            "city": "Paris", "postalCode": "75014",
            "locations": [{"city": "Lyon", "postalCode": "69007"}],
        }
        assert get_locations(criteria) == [{"city": "Lyon", "postalCode": "69007"}]

    def test_incomplete_entries_in_locations_list_are_dropped(self):
        criteria = {"locations": [
            {"city": "Paris", "postalCode": "75014"},
            {"city": "Lyon"},
            {"postalCode": "13001"},
        ]}
        assert get_locations(criteria) == [{"city": "Paris", "postalCode": "75014"}]

    def test_no_location_at_all(self):
        assert get_locations({}) == []
        assert get_locations({"city": "Paris"}) == []


class TestPerSourceHasValidCriteria:
    """Each source encodes location differently, so validity is delegated
    per-parser instead of a single hardcoded key (e.g. placeIds)."""

    def test_seloger_requires_place_ids(self):
        from parsers.seloger import SeLogerParser
        parser = SeLogerParser()
        assert parser.has_valid_criteria({"placeIds": ["750113"]}) is True
        assert parser.has_valid_criteria({"priceMax": 1500}) is False
        assert parser.has_valid_criteria({}) is False

    def test_laforet_requires_city_and_postal_code(self):
        from parsers.laforet import LaforetParser
        parser = LaforetParser()
        assert parser.has_valid_criteria({"city": "Paris", "postalCode": "75018"}) is True
        assert parser.has_valid_criteria({"city": "Paris"}) is False
        assert parser.has_valid_criteria({"postalCode": "75018"}) is False
        assert parser.has_valid_criteria({}) is False

    def test_laforet_does_not_override_has_valid_criteria(self):
        """Laforet's requirement is exactly BaseParser's default (city +
        postalCode) — it needs zero source-specific validity logic, which is
        the whole point of the universal location contract."""
        from parsers.laforet import LaforetParser
        assert "has_valid_criteria" not in LaforetParser.__dict__


class TestExtraLocationMetadata:
    """SeLoger can't derive its opaque placeId from city+postalCode (no
    public geocoding API), so it declares an extra manual field via this
    metadata — the UI renders it generically from here."""

    def test_seloger_requires_extra_location_field(self):
        from parsers.seloger import SeLogerParser
        assert SeLogerParser.REQUIRES_EXTRA_LOCATION is True
        assert SeLogerParser.EXTRA_LOCATION_LABEL
        assert SeLogerParser.EXTRA_LOCATION_HELP

    def test_laforet_does_not_require_extra_location_field(self):
        from parsers.laforet import LaforetParser
        assert LaforetParser.REQUIRES_EXTRA_LOCATION is False

    def test_list_sources_includes_extra_location_metadata(self):
        sources = ParserRegistry.list_sources()
        seloger = next(s for s in sources if s["id"] == "seloger")
        laforet = next(s for s in sources if s["id"] == "laforet")
        assert seloger["requires_extra_location"] is True
        assert seloger["extra_location_label"]
        assert laforet["requires_extra_location"] is False

    def test_laforet_url_note_explains_missing_filters(self):
        """Laforet's build_search_url() deliberately omits price/surface/rooms
        (they break the site's own city scoping — see parsers/laforet.py) —
        URL_NOTE must explain that in the UI instead of it looking broken."""
        from parsers.laforet import LaforetParser
        assert LaforetParser.URL_NOTE

    def test_list_sources_includes_url_note(self):
        sources = ParserRegistry.list_sources()
        seloger = next(s for s in sources if s["id"] == "seloger")
        laforet = next(s for s in sources if s["id"] == "laforet")
        assert laforet["url_note"]
        assert seloger["url_note"] == ""
