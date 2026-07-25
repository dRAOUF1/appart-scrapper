"""Tests for parsers/seloger.py — traduction du canonique + résolution du placeId."""
from unittest.mock import MagicMock, patch

import pytest

from parsers.seloger import SeLogerParser

PARIS_15 = {"city": "Paris", "postalCode": "75015", "inseeCode": "75115"}
LYON_7 = {"city": "Lyon", "postalCode": "69007", "inseeCode": "69387"}
NOWHERE = {"city": "Nawak", "postalCode": "99999", "inseeCode": "99999"}


def manual(place_ids):
    """Des critères portant un placeId saisi à la main."""
    return {"sourceOverrides": {"seloger": {"placeIds": place_ids}}}


def resolving_parser():
    """Un parser doté d'un storage : la résolution automatique du placeId
    s'appuie sur le cache en base, donc sur le storage injecté (jamais sur
    flask.current_app — voir TestNoFlaskDependency)."""
    return SeLogerParser(storage=MagicMock())


class TestHasValidCriteria:
    def test_location_with_insee_code_is_tentatively_valid(self):
        """Resolution is attempted at scrape time, not at validation time —
        a location from the unified autocomplete (inseeCode present) must
        not block search creation on a live crawl."""
        parser = SeLogerParser()
        assert parser.has_valid_criteria({"locations": [PARIS_15]}) is True

    def test_location_without_insee_code_is_invalid(self):
        """A manually-typed location (never went through the autocomplete)
        has no inseeCode to resolve from — still needs the manual fallback."""
        parser = SeLogerParser()
        criteria = {"locations": [{"city": "Paris", "postalCode": "75015"}]}
        assert parser.has_valid_criteria(criteria) is False

    def test_manual_place_id_alone_is_enough(self):
        parser = SeLogerParser()
        assert parser.has_valid_criteria(manual(["AD08FR31096"])) is True

    def test_no_criteria_at_all(self):
        assert SeLogerParser().has_valid_criteria({}) is False


class TestCannotSearchReason:
    def test_none_when_usable(self):
        parser = SeLogerParser()
        assert parser.cannot_search_reason({"locations": [PARIS_15]}) is None

    def test_explains_the_missing_insee_code(self):
        """Le message doit orienter vers la solution (choisir dans les
        suggestions) plutôt que dire seulement « invalide »."""
        parser = SeLogerParser()
        criteria = {"locations": [{"city": "Paris", "postalCode": "75015"}]}
        reason = parser.cannot_search_reason(criteria)
        assert reason and "INSEE" in reason

    def test_seloger_supports_every_property_type(self):
        parser = SeLogerParser()
        criteria = {"locations": [PARIS_15], "propertyTypes": ["parking", "land"]}
        assert parser.cannot_search_reason(criteria) is None


class TestToNative:
    def test_manual_place_ids_are_never_overridden(self):
        parser = SeLogerParser()
        criteria = {**manual(["AD08FR12345"]), "locations": [PARIS_15]}
        with patch("services.seloger_geocode.resolve_place_id") as mock_resolve:
            native = parser.to_native(criteria)
        assert native["placeIds"] == ["AD08FR12345"]
        mock_resolve.assert_not_called()

    def test_resolves_place_ids_from_locations(self):
        parser = resolving_parser()
        criteria = {"locations": [PARIS_15, LYON_7]}
        with patch(
            "services.seloger_geocode.resolve_place_id",
            side_effect=["AD08FR31096", "AD08FR99999"],
        ) as mock_resolve:
            native = parser.to_native(criteria)
        assert native["placeIds"] == ["AD08FR31096", "AD08FR99999"]
        assert mock_resolve.call_count == 2

    def test_locations_without_insee_code_are_skipped(self):
        """Une ville tapée à la main n'a pas de code INSEE : rien ne permet
        d'identifier le périmètre, donc aucun placeId — et surtout aucun appel
        réseau à SeLoger pour rien."""
        parser = SeLogerParser(storage=MagicMock(**{"seloger_geo.get_cached.return_value": None}))
        criteria = {"locations": [{"city": "Paris", "postalCode": "75015"}]}
        with patch("services.seloger_geocode._resolve_uncached") as mock_lookup:
            native = parser.to_native(criteria)
        assert "placeIds" not in native
        mock_lookup.assert_not_called()

    def test_unresolvable_location_yields_no_place_id(self):
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value=None):
            native = parser.to_native({"locations": [NOWHERE]})
        assert "placeIds" not in native

    def test_partial_resolution_keeps_only_the_resolved_ones(self):
        parser = resolving_parser()
        with patch(
            "services.seloger_geocode.resolve_place_id",
            side_effect=["AD08FR31096", None],
        ):
            native = parser.to_native({"locations": [PARIS_15, NOWHERE]})
        assert native["placeIds"] == ["AD08FR31096"]

    def test_duplicate_place_ids_are_collapsed(self):
        """Deux arrondissements d'une même ville peuvent résoudre vers le même
        placeId — le répéter dans l'URL ne sert à rien."""
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value="AD08FR31096"):
            native = parser.to_native({"locations": [PARIS_15, LYON_7]})
        assert native["placeIds"] == ["AD08FR31096"]

    def test_translates_the_canonical_vocabulary(self):
        parser = SeLogerParser()
        criteria = {
            **manual(["AD08FR31096"]),
            "transaction": "buy",
            "propertyTypes": ["house", "land"],
            "priceMin": 100000,
            "priceMax": 500000,
            "surfaceMin": 40,
            "surfaceMax": 120,
            "rooms": [2, 3],
            "bedrooms": [1],
        }
        native = parser.to_native(criteria)
        assert native["distributionTypes"] == ["Sale"]
        assert native["estateTypes"] == ["House", "Land"]
        assert native["priceMin"] == 100000
        assert native["priceMax"] == 500000
        # SeLoger nomme la surface "space", et attend des chaînes pour les pièces.
        assert native["spaceMin"] == 40
        assert native["spaceMax"] == 120
        assert native["rooms"] == ["2", "3"]
        assert native["bedrooms"] == ["1"]
        assert "surfaceMin" not in native

    def test_rent_is_translated(self):
        parser = SeLogerParser()
        native = parser.to_native({**manual(["X"]), "transaction": "rent"})
        assert native["distributionTypes"] == ["Rent"]

    def test_other_manual_overrides_are_forwarded(self):
        """Une URL SeLoger collée peut porter d'autres paramètres propres au
        site (ex. locationsInBuildingExcluded) — ils doivent survivre."""
        parser = SeLogerParser()
        criteria = {"sourceOverrides": {"seloger": {
            "placeIds": ["AD08FR31096"],
            "locationsInBuildingExcluded": ["Ground"],
        }}}
        native = parser.to_native(criteria)
        assert native["locationsInBuildingExcluded"] == ["Ground"]

    def test_does_not_mutate_the_input(self):
        """Les critères sont partagés entre les sources d'une recherche."""
        parser = resolving_parser()
        criteria = {"locations": [PARIS_15], "transaction": "rent"}
        snapshot = {"locations": [dict(PARIS_15)], "transaction": "rent"}
        with patch("services.seloger_geocode.resolve_place_id", return_value="AD08FR31096"):
            parser.to_native(criteria)
        assert criteria == snapshot


class TestWideAreaSearches:
    """SeLoger a un identifiant par niveau de périmètre, et un seul suffit à le
    couvrir entièrement — vérifié en live : AD04FR5 (Île-de-France) rend des
    annonces réparties sur les 8 départements de la région."""

    GIRONDE = {"kind": "department", "name": "Gironde", "code": "33"}
    IDF = {"kind": "region", "name": "Île-de-France", "code": "11",
           "departments": ["75", "77", "78", "91", "92", "93", "94", "95"]}
    PARIS_WHOLE = {"kind": "whole_city", "city": "Paris", "inseeCode": "75056",
                   "postalCodes": ["75001", "75015"]}

    def test_one_place_id_covers_a_whole_department(self):
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value="AD06FR34") as mock:
            native = parser.to_native({"locations": [self.GIRONDE]})
        assert native["placeIds"] == ["AD06FR34"]
        assert mock.call_count == 1

    def test_one_place_id_covers_a_whole_region(self):
        """Pas de développement en 1266 communes : un seul identifiant."""
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value="AD04FR5") as mock:
            native = parser.to_native({"locations": [self.IDF]})
        assert native["placeIds"] == ["AD04FR5"]
        assert mock.call_count == 1

    def test_whole_city_is_one_place_id_too(self):
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value="AD08FR31096"):
            native = parser.to_native({"locations": [self.PARIS_WHOLE]})
        assert native["placeIds"] == ["AD08FR31096"]

    def test_levels_can_be_mixed_in_one_search(self):
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id",
                   side_effect=["AD06FR34", "POCOFR4809"]):
            native = parser.to_native({"locations": [self.GIRONDE, PARIS_15]})
        assert native["placeIds"] == ["AD06FR34", "POCOFR4809"]

    def test_a_department_is_valid_without_any_city(self):
        """Une recherche départementale n'a ni ville ni code postal, elle doit
        rester valide."""
        parser = SeLogerParser()
        criteria = {"locations": [self.GIRONDE]}
        assert parser.has_valid_criteria(criteria) is True
        assert parser.cannot_search_reason(criteria) is None

    def test_a_wide_area_without_a_code_is_not_valid(self):
        parser = SeLogerParser()
        criteria = {"locations": [{"kind": "department", "name": "Gironde"}]}
        assert parser.has_valid_criteria(criteria) is False


class TestBuildSearchUrl:
    """Regression coverage: build_search_url() used to forward stored
    criteria straight to scraper.seloger.build_search_url() without
    resolving placeIds first — since resolution isn't persisted back to the
    search, a search created via the unified location autocomplete (only
    `locations`, no placeId yet) silently produced a URL with no
    `locations=` param at all: an unscoped, nationwide SeLoger link shown
    to the user as "the" search URL."""

    def test_resolves_place_ids_before_building_the_url(self):
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value="AD08FR31096"):
            url = parser.build_search_url({"locations": [PARIS_15]})
        assert url is not None
        assert "locations=AD08FR31096" in url

    def test_manual_place_ids_still_work_without_any_resolution_call(self):
        parser = SeLogerParser()
        with patch("services.seloger_geocode.resolve_place_id") as mock_resolve:
            url = parser.build_search_url(manual(["AD08FR31096"]))
        assert "locations=AD08FR31096" in url
        mock_resolve.assert_not_called()

    def test_returns_none_instead_of_an_unscoped_url_when_unresolved(self):
        """The regression: must NOT return a URL missing `locations=` —
        return None so the UI shows "URL non disponible" instead of a
        nationwide search link that looks legitimate."""
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value=None):
            assert parser.build_search_url({"locations": [NOWHERE]}) is None

    def test_returns_none_with_no_criteria_at_all(self):
        assert SeLogerParser().build_search_url({}) is None

    def test_build_search_urls_is_empty_when_unresolved(self):
        """base.BaseParser.build_search_urls() default wraps
        build_search_url() — must come back empty, not [None]."""
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value=None):
            assert parser.build_search_urls({"locations": [NOWHERE]}) == []


class TestScrape:
    def test_raises_when_nothing_resolves(self):
        """Must fail loudly instead of silently building an unscoped,
        nationwide SeLoger search when neither a manual placeId nor
        automatic resolution produced anything."""
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value=None):
            with pytest.raises(ValueError):
                parser.scrape({"locations": [NOWHERE]})

    def test_raises_with_no_criteria_at_all(self):
        with pytest.raises(ValueError):
            SeLogerParser().scrape({})

    def test_scrapes_with_auto_resolved_place_id(self):
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value="AD08FR31096"):
            with patch("scraper.seloger.scrape", return_value=[]) as mock_scrape:
                parser.scrape({"locations": [PARIS_15]})
        called_criteria = mock_scrape.call_args[0][0]
        assert called_criteria["placeIds"] == ["AD08FR31096"]

    def test_scrape_receives_native_criteria_only(self):
        """Le scraper ne doit jamais voir le vocabulaire canonique."""
        parser = SeLogerParser()
        criteria = {**manual(["AD08FR31096"]), "transaction": "rent", "surfaceMin": 30}
        with patch("scraper.seloger.scrape", return_value=[]) as mock_scrape:
            parser.scrape(criteria)
        native = mock_scrape.call_args[0][0]
        assert native["distributionTypes"] == ["Rent"]
        assert native["spaceMin"] == 30
        assert "transaction" not in native
        assert "surfaceMin" not in native


class TestNoFlaskDependency:
    """Régression : le parser lisait son cache d'identifiants de lieu via
    `flask.current_app`. Or le scraping tourne sur un thread de fond, sans
    contexte d'application (voir core.scrape_control : ScrapeService est
    soumis à un ThreadPoolExecutor, pas exécuté dans une requête) — tous les
    scrapes automatiques de SeLoger échouaient donc sur « Working outside of
    application context », y compris ceux du scheduler.

    Ces tests tournent volontairement hors de toute app Flask : ils échouent
    si une dépendance à current_app est réintroduite.
    """

    def _storage_with_cache(self, place_id):
        storage = MagicMock()
        storage.seloger_geo.get_cached.return_value = {
            "place_id": place_id, "resolved_at": None,
        }
        return storage

    def test_scrape_works_without_an_app_context(self):
        parser = SeLogerParser(storage=self._storage_with_cache("AD08FR31096"))
        with patch("scraper.seloger.scrape", return_value=[]) as mock_scrape:
            parser.scrape({"locations": [PARIS_15]})
        assert mock_scrape.call_args[0][0]["placeIds"] == ["AD08FR31096"]

    def test_resolution_is_read_from_the_injected_storage(self):
        storage = self._storage_with_cache("AD08FR31096")
        parser = SeLogerParser(storage=storage)
        assert parser.to_native({"locations": [PARIS_15]})["placeIds"] == ["AD08FR31096"]
        storage.seloger_geo.get_cached.assert_called_once_with("75115")

    def test_without_storage_no_place_id_is_invented(self):
        """Sans storage (donc sans cache), la source doit le dire clairement au
        lieu de partir sur une recherche non localisée."""
        parser = SeLogerParser()
        with pytest.raises(ValueError):
            parser.scrape({"locations": [PARIS_15]})

    def test_manual_place_id_needs_no_storage_at_all(self):
        """Le repli manuel doit rester utilisable même sans base."""
        parser = SeLogerParser()
        with patch("scraper.seloger.scrape", return_value=[]) as mock_scrape:
            parser.scrape(manual(["AD08FR31096"]))
        assert mock_scrape.call_args[0][0]["placeIds"] == ["AD08FR31096"]


class TestManualOverrideParsing:
    def test_parses_a_pasted_search_url(self):
        parser = SeLogerParser()
        override = parser.parse_manual_override(
            "https://www.seloger.com/classified-search?locations=AD08FR31096&priceMax=2000"
        )
        assert override == {"placeIds": ["AD08FR31096"]}

    def test_splits_comma_joined_place_ids_from_a_url(self):
        """Une vraie URL SeLoger joint les placeIds par des virgules."""
        parser = SeLogerParser()
        override = parser.parse_manual_override(
            "https://www.seloger.com/classified-search?locations=AD08FR31096,AD08FR36603"
        )
        assert override == {"placeIds": ["AD08FR31096", "AD08FR36603"]}

    def test_parses_raw_place_ids(self):
        parser = SeLogerParser()
        assert parser.parse_manual_override("AD08FR31096, AD08FR36603") == {
            "placeIds": ["AD08FR31096", "AD08FR36603"]
        }

    def test_empty_or_useless_input(self):
        parser = SeLogerParser()
        assert parser.parse_manual_override("") == {}
        assert parser.parse_manual_override("   ") == {}
        assert parser.parse_manual_override("https://www.seloger.com/") == {}
