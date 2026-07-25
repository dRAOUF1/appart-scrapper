"""Tests for scraper/seloger.py's URL building/parsing (no real network calls)."""
from scraper.seloger import build_search_url, parse_search_url


class TestParseSearchUrl:
    def test_single_location(self):
        url = "https://www.seloger.com/classified-search?locations=AD08FR31096"
        criteria = parse_search_url(url)
        assert criteria["placeIds"] == ["AD08FR31096"]

    def test_comma_joined_locations_are_split_into_separate_place_ids(self):
        """Regression: a real SeLoger URL can comma-join several placeIds
        into a single `locations=` occurrence — unlike this app's own
        build_search_url(), which repeats the param instead (see
        test_multiple_locations_are_repeated_params_not_comma_joined).
        Splitting must match distributionTypes/estateTypes/rooms/bedrooms,
        which already use _split_csv_values. Without this, a pasted
        multi-location URL silently becomes ONE bogus opaque placeId
        string (verified against a real broken search: "AD08FR31096,
        AD08FR36603,AD08FR36621,AD08FR36616" ended up as a single-element
        list instead of four)."""
        url = (
            "https://www.seloger.com/classified-search"
            "?locations=AD08FR31096,AD08FR36603,AD08FR36621,AD08FR36616"
        )
        criteria = parse_search_url(url)
        assert criteria["placeIds"] == [
            "AD08FR31096", "AD08FR36603", "AD08FR36621", "AD08FR36616",
        ]

    def test_repeated_locations_param_also_works(self):
        """This app's own build_search_url() repeats the param instead of
        comma-joining — both real-world shapes must parse correctly."""
        url = (
            "https://www.seloger.com/classified-search"
            "?locations=AD08FR31096&locations=AD08FR36603"
        )
        criteria = parse_search_url(url)
        assert criteria["placeIds"] == ["AD08FR31096", "AD08FR36603"]

    def test_no_locations_param_means_no_place_ids_key(self):
        url = "https://www.seloger.com/classified-search?priceMax=1000"
        criteria = parse_search_url(url)
        assert "placeIds" not in criteria

    def test_price_and_space_fields(self):
        url = "https://www.seloger.com/classified-search?priceMin=600&priceMax=900&spaceMin=20"
        criteria = parse_search_url(url)
        assert criteria["priceMin"] == 600
        assert criteria["priceMax"] == 900
        assert criteria["spaceMin"] == 20


class TestBuildSearchUrl:
    def test_multiple_locations_are_repeated_params_not_comma_joined(self):
        url = build_search_url({"placeIds": ["AD08FR31096", "AD08FR36603"]})
        assert "locations=AD08FR31096" in url
        assert "locations=AD08FR36603" in url
        assert "%2C" not in url  # never comma-joined

    def test_round_trips_through_parse_search_url(self):
        """build then parse must reproduce the same placeIds list — the
        two real-world URL shapes (repeated param vs comma-joined) both
        need to survive this, since a user might paste either one."""
        original = ["AD08FR31096", "AD08FR36603", "AD08FR36621"]
        url = build_search_url({"placeIds": original})
        reparsed = parse_search_url(url)
        assert reparsed["placeIds"] == original
