"""Tests for core/criteria.py — le vocabulaire canonique et sa normalisation."""

from core.criteria import (
    APARTMENT,
    matches_locations,
    BUY,
    HOUSE,
    LAND,
    PARKING,
    RENT,
    normalize_criteria,
    source_overrides,
    with_source_override,
)


class TestCanonicalPassthrough:
    def test_empty_and_invalid_inputs(self):
        assert normalize_criteria(None) == {}
        assert normalize_criteria({}) == {}
        assert normalize_criteria("not-a-dict") == {}

    def test_already_canonical_is_unchanged(self):
        criteria = {
            "locations": [{"kind": "city", "city": "Paris", "postalCode": "75015", "inseeCode": "75115"}],
            "transaction": RENT,
            "propertyTypes": [APARTMENT],
            "priceMin": 500,
            "priceMax": 2000,
            "surfaceMin": 20,
            "surfaceMax": 80,
            "rooms": [1, 2],
            "bedrooms": [1],
        }
        assert normalize_criteria(criteria) == criteria

    def test_normalization_is_idempotent(self):
        legacy = {
            "city": "Poitiers", "postalCode": "86000",
            "distributionTypes": ["Sale"], "estateTypes": ["House"],
            "spaceMin": 40, "rooms": ["2", "3"],
        }
        once = normalize_criteria(legacy)
        assert normalize_criteria(once) == once


class TestLegacyVocabulary:
    def test_distribution_types_become_transaction(self):
        assert normalize_criteria({"distributionTypes": ["Rent"]})["transaction"] == RENT
        assert normalize_criteria({"distributionTypes": ["Sale"]})["transaction"] == BUY

    def test_estate_types_become_property_types(self):
        result = normalize_criteria({"estateTypes": ["Apartment", "House", "Parking", "Land"]})
        assert result["propertyTypes"] == [APARTMENT, HOUSE, PARKING, LAND]

    def test_space_becomes_surface(self):
        result = normalize_criteria({"spaceMin": 30, "spaceMax": 90})
        assert result["surfaceMin"] == 30
        assert result["surfaceMax"] == 90
        assert "spaceMin" not in result

    def test_flat_city_postal_code_becomes_a_location(self):
        result = normalize_criteria({"city": "Poitiers", "postalCode": "86000"})
        assert result["locations"] == [{"kind": "city", "city": "Poitiers", "postalCode": "86000"}]

    def test_locations_win_over_the_flat_pair(self):
        """Le couple à plat n'était qu'un miroir de la première localisation
        (voir routes/web.py) — il ne doit pas en ajouter une seconde."""
        result = normalize_criteria({
            "locations": [
                {"city": "Paris", "postalCode": "75015"},
                {"city": "Paris", "postalCode": "75014"},
            ],
            "city": "Paris", "postalCode": "75015",
        })
        assert len(result["locations"]) == 2

    def test_counts_become_ints(self):
        result = normalize_criteria({"rooms": ["2", "1"], "bedrooms": ["3"]})
        assert result["rooms"] == [1, 2]
        assert result["bedrooms"] == [3]

    def test_seloger_own_keys_move_to_source_overrides(self):
        """placeIds n'a rien à faire au premier niveau du canonique : c'est
        une valeur propre à SeLoger, pas un critère de recherche."""
        result = normalize_criteria({
            "placeIds": ["AD08FR31096"],
            "locationsInBuildingExcluded": ["Ground"],
        })
        assert "placeIds" not in result
        assert result["sourceOverrides"]["seloger"] == {
            "placeIds": ["AD08FR31096"],
            "locationsInBuildingExcluded": ["Ground"],
        }

    def test_order_is_dropped(self):
        """`order: DateDesc` était un détail SeLoger ; un tracker veut
        toujours les annonces les plus récentes, la source le fixe elle-même."""
        assert "order" not in normalize_criteria({"order": "DateDesc", "priceMax": 900})


class TestRobustness:
    def test_unknown_values_are_dropped_not_guessed(self):
        result = normalize_criteria({
            "transaction": "barter",
            "propertyTypes": ["castle", "Apartment"],
        })
        assert "transaction" not in result
        assert result["propertyTypes"] == [APARTMENT]

    def test_unparsable_numbers_are_ignored(self):
        result = normalize_criteria({"priceMax": "beaucoup", "priceMin": "500"})
        assert "priceMax" not in result
        assert result["priceMin"] == 500

    def test_incomplete_locations_are_dropped(self):
        result = normalize_criteria({"locations": [
            {"city": "Paris"},
            {"postalCode": "75015"},
            "not-a-dict",
            {"city": "  ", "postalCode": "75015"},
            {"city": "Lyon", "postalCode": "69007"},
        ]})
        assert result["locations"] == [{"kind": "city", "city": "Lyon", "postalCode": "69007"}]

    def test_optional_location_fields_are_kept_when_present(self):
        result = normalize_criteria({"locations": [
            {"city": "Paris", "postalCode": "75015", "inseeCode": "75115", "lat": 48.8, "lon": 2.3},
        ]})
        assert result["locations"][0]["inseeCode"] == "75115"
        assert result["locations"][0]["lat"] == 48.8

    def test_case_and_whitespace_are_tolerated(self):
        result = normalize_criteria({
            "transaction": " RENT ",
            "propertyTypes": ["APARTMENT"],
            "locations": [{"city": " Paris ", "postalCode": " 75015 "}],
        })
        assert result["transaction"] == RENT
        assert result["propertyTypes"] == [APARTMENT]
        assert result["locations"] == [{"kind": "city", "city": "Paris", "postalCode": "75015"}]

    def test_numeric_postal_code_is_stringified(self):
        result = normalize_criteria({"locations": [{"city": "Paris", "postalCode": 75015}]})
        assert result["locations"][0]["postalCode"] == "75015"

    def test_duplicate_property_types_are_collapsed(self):
        result = normalize_criteria({"propertyTypes": ["apartment", "Apartment"]})
        assert result["propertyTypes"] == [APARTMENT]


class TestWideAreaLocations:
    """Les périmètres plus larges qu'une commune : région, département, ville
    entière. Chaque source les traduit vers son propre identifiant."""

    def test_region_keeps_its_code_and_departments(self):
        result = normalize_criteria({"locations": [{
            "kind": "region", "name": "Île-de-France", "code": "11",
            "departments": ["75", "77", "78", "91", "92", "93", "94", "95"],
        }]})
        assert result["locations"] == [{
            "kind": "region", "code": "11", "name": "Île-de-France",
            "departments": ["75", "77", "78", "91", "92", "93", "94", "95"],
        }]

    def test_department_keeps_its_code(self):
        result = normalize_criteria({"locations": [
            {"kind": "department", "name": "Gironde", "code": "33"},
        ]})
        assert result["locations"] == [{"kind": "department", "code": "33", "name": "Gironde"}]

    def test_whole_city_keeps_all_its_postal_codes(self):
        result = normalize_criteria({"locations": [{
            "kind": "whole_city", "city": "Bordeaux", "inseeCode": "33063",
            "postalCodes": ["33800", "33000", "33000"],
        }]})
        assert result["locations"] == [{
            "kind": "whole_city", "city": "Bordeaux",
            "postalCodes": ["33000", "33800"], "inseeCode": "33063",
        }]

    def test_a_wide_area_without_a_code_is_dropped(self):
        """Sans code, le périmètre est inexploitable — mieux vaut l'écarter que
        de lancer une recherche sur un lieu indéterminé."""
        assert normalize_criteria({"locations": [{"kind": "region", "name": "Nulle part"}]}) == {}
        assert normalize_criteria({"locations": [{"kind": "department", "name": "X"}]}) == {}

    def test_a_whole_city_without_postal_codes_is_dropped(self):
        assert normalize_criteria({"locations": [
            {"kind": "whole_city", "city": "Bordeaux", "inseeCode": "33063"},
        ]}) == {}

    def test_unknown_kind_is_dropped_not_guessed(self):
        assert normalize_criteria({"locations": [
            {"kind": "planet", "city": "Mars", "postalCode": "00000"},
        ]}) == {}

    def test_mixed_levels_coexist(self):
        result = normalize_criteria({"locations": [
            {"kind": "region", "name": "Île-de-France", "code": "11", "departments": ["75"]},
            {"kind": "department", "name": "Gironde", "code": "33"},
            {"city": "Poitiers", "postalCode": "86000"},
        ]})
        assert [loc["kind"] for loc in result["locations"]] == ["region", "department", "city"]


class TestPostalCodeMatching:
    """Le contrôle local qui garantit qu'une annonce est bien dans le périmètre
    demandé — les sources élargissent parfois d'elles-mêmes (Laforet inclut la
    métropole autour d'une commune)."""

    def test_city_requires_an_exact_postal_code(self):
        locations = [{"kind": "city", "city": "Bordeaux", "postalCode": "33000"}]
        assert matches_locations("33000", locations) is True
        assert matches_locations("33800", locations) is False

    def test_whole_city_accepts_all_its_postal_codes(self):
        locations = [{"kind": "whole_city", "city": "Bordeaux",
                      "postalCodes": ["33000", "33800"]}]
        assert matches_locations("33800", locations) is True
        assert matches_locations("33300", locations) is False

    def test_department_accepts_the_whole_range(self):
        locations = [{"kind": "department", "name": "Gironde", "code": "33"}]
        assert matches_locations("33000", locations) is True
        assert matches_locations("33640", locations) is True
        assert matches_locations("75015", locations) is False

    def test_region_accepts_every_department(self):
        locations = [{"kind": "region", "name": "Île-de-France", "code": "11",
                      "departments": ["75", "92", "93"]}]
        assert matches_locations("75015", locations) is True
        assert matches_locations("93200", locations) is True
        assert matches_locations("33000", locations) is False

    def test_corsica_departments_use_the_20_prefix(self):
        """2A/2B n'apparaissent jamais dans un code postal : la Corse est en
        20xxx. Sans ce traitement, une recherche corse ne ramènerait rien."""
        locations = [{"kind": "department", "name": "Corse-du-Sud", "code": "2A"}]
        assert matches_locations("20000", locations) is True

    def test_a_listing_without_a_postal_code_never_matches(self):
        """On n'accorde jamais le bénéfice du doute sur la localisation."""
        assert matches_locations("", [{"kind": "department", "code": "33"}]) is False

    def test_no_location_matches_nothing(self):
        assert matches_locations("33000", []) is False


class TestSourceOverrides:
    def test_reads_only_the_asked_source(self):
        criteria = {"sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096"]}}}
        assert source_overrides(criteria, "seloger") == {"placeIds": ["AD08FR31096"]}
        assert source_overrides(criteria, "laforet") == {}

    def test_missing_or_malformed_overrides(self):
        assert source_overrides({}, "seloger") == {}
        assert source_overrides({"sourceOverrides": "nope"}, "seloger") == {}
        assert source_overrides({"sourceOverrides": {"seloger": "nope"}}, "seloger") == {}

    def test_with_source_override_does_not_mutate_the_original(self):
        """Les critères sont partagés entre toutes les sources d'une même
        recherche pendant un scrape : la traduction de l'une ne doit jamais
        fuiter dans celle d'une autre."""
        criteria = {"sourceOverrides": {"laforet": {"foo": "bar"}}}
        updated = with_source_override(criteria, "seloger", {"placeIds": ["X"]})
        assert criteria == {"sourceOverrides": {"laforet": {"foo": "bar"}}}
        assert updated["sourceOverrides"]["seloger"] == {"placeIds": ["X"]}
        assert updated["sourceOverrides"]["laforet"] == {"foo": "bar"}

    def test_with_empty_values_is_a_noop(self):
        assert with_source_override({"priceMax": 900}, "seloger", {}) == {"priceMax": 900}
