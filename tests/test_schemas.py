"""Tests for core/schemas.py payload validation."""
import pytest

from core.schemas import validate_criteria, validate_scrape_interval


class TestValidateCriteria:
    def test_none_returns_empty_dict(self):
        assert validate_criteria(None) == {}

    def test_canonical_criteria_pass_through(self):
        criteria = {
            "locations": [{"city": "Paris", "postalCode": "75015"}],
            "transaction": "rent",
            "propertyTypes": ["apartment"],
            "priceMax": 1500,
        }
        result = validate_criteria(criteria)
        assert result["transaction"] == "rent"
        assert result["propertyTypes"] == ["apartment"]
        assert result["priceMax"] == 1500

    def test_legacy_criteria_are_converted(self):
        """Un client (ou une intégration) qui envoie encore l'ancien
        vocabulaire SeLoger reste accepté — c'est stocké en canonique."""
        result = validate_criteria({
            "distributionTypes": ["Sale"],
            "estateTypes": ["House"],
            "spaceMin": 40,
            "city": "Poitiers", "postalCode": "86000",
        })
        assert result["transaction"] == "buy"
        assert result["propertyTypes"] == ["house"]
        assert result["surfaceMin"] == 40
        assert result["locations"] == [{"city": "Poitiers", "postalCode": "86000"}]

    def test_source_specific_keys_land_in_source_overrides(self):
        """placeIds et locationsInBuildingExcluded sont propres à SeLoger :
        conservés, mais rangés comme surcharge de source, pas comme critères."""
        criteria = {"placeIds": ["x"], "locationsInBuildingExcluded": ["y"]}
        result = validate_criteria(criteria)
        assert result["sourceOverrides"]["seloger"] == {
            "placeIds": ["x"],
            "locationsInBuildingExcluded": ["y"],
        }

    def test_non_dict_raises(self):
        with pytest.raises(ValueError):
            validate_criteria("not-a-dict")

    def test_wrong_type_for_known_field_raises(self):
        with pytest.raises(ValueError):
            validate_criteria({"priceMax": "not-a-number"})

    def test_placeids_must_be_list(self):
        with pytest.raises(ValueError):
            validate_criteria({"placeIds": "AD08FR12345"})


class TestValidateScrapeInterval:
    def test_valid_int(self):
        assert validate_scrape_interval(10) == 10

    def test_valid_string(self):
        assert validate_scrape_interval("10") == 10

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError):
            validate_scrape_interval("abc")

    def test_below_minimum_raises(self):
        with pytest.raises(ValueError):
            validate_scrape_interval(0)

    def test_above_maximum_raises(self):
        with pytest.raises(ValueError):
            validate_scrape_interval(999999)
