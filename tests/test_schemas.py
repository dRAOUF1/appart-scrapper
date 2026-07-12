"""Tests for schemas.py payload validation."""
import pytest

from schemas import validate_criteria, validate_scrape_interval


class TestValidateCriteria:
    def test_none_returns_empty_dict(self):
        assert validate_criteria(None) == {}

    def test_valid_criteria_passes_through(self):
        criteria = {"placeIds": ["AD08FR12345"], "priceMax": 1500}
        result = validate_criteria(criteria)
        assert result["placeIds"] == ["AD08FR12345"]
        assert result["priceMax"] == 1500

    def test_unknown_extra_fields_are_preserved(self):
        criteria = {"placeIds": ["x"], "locationsInBuildingExcluded": ["y"]}
        result = validate_criteria(criteria)
        assert result["locationsInBuildingExcluded"] == ["y"]

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
