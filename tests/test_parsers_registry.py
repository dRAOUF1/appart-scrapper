"""Tests for parsers/base.py (BaseParser contract + ParserRegistry)."""
import pytest

from models.listing import Listing
from parsers.base import BaseParser, ParserRegistry


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
        }]

    def test_seloger_is_registered_by_default(self):
        sources = ParserRegistry.list_sources()
        assert any(s["id"] == "seloger" for s in sources)


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
