"""Tests unitaires de `services/bienici_geocode.py`.

Ce module traduit un périmètre canonique (voir core/criteria.py) en zoneIds
bienici, en interrogeant l'autocomplete public du site et en mémorisant le
résultat — même rôle que services/seloger_geocode.py, dont ce fichier de
test reprend la structure (voir tests/unit/test_seloger_geocode.py).

Deux différences de fond avec SeLoger, qui ont chacune leur propre section
de tests :

  * bienici résout un DÉPARTEMENT par son code INSEE directement (`q=33`),
    pas par son nom — vérifié en direct, plus simple et sans ambiguïté que
    la recherche par nom de SeLoger ;
  * bienici n'a AUCUNE entité de niveau région dans son autocomplete : une
    région se résout en UNION des zoneIds de chacun de ses départements
    (core.geocode.region_departments), d'où une résolution qui renvoie une
    LISTE de zoneIds plutôt qu'un identifiant singulier.

Aucun appel réseau : le socle bloque le transport HTTP (tests/conftest.py),
`requests_mock` sert d'adaptateur pour les tests qui exercent la requête.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time
from loguru import logger

from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY
from repositories.bienici_geo_repo import BienIciGeoRepository
from services import bienici_geocode as geo
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)

FROZEN = "2026-07-26 12:00:00"

# Réponses de suggest.json, telles qu'observées en direct le 26/07/2026.
PARIS_WHOLE_CITY = {
    "name": "Paris", "type": "city", "insee_code": "75056",
    "postalCodes": ["75000"], "zoneIds": ["-7444"],
}
PARIS_15E = {
    "name": "Paris 15e", "type": "arrondissement", "insee_code": "75115",
    "postalCodes": ["75015"], "zoneIds": ["-9520"],
}
NANTES = {
    "name": "Nantes", "type": "city", "insee_code": "44109",
    "postalCodes": ["44000", "44100", "44200", "44300"], "zoneIds": ["-8931"],
}
GIRONDE_DEPT = {
    "name": "Gironde", "type": "department", "insee_code": "33",
    "postalCodes": None, "zoneIds": ["-7405"],
}
GIRONDE_COMMUNE = {
    "name": "Gironde-sur-Dropt", "type": "city", "insee_code": "33187",
    "postalCodes": ["33190"], "zoneIds": ["-74563"],
}
RHONE_ALIAS = {
    "name": "Rhône et Grand Lyon", "type": "alias-department",
    "insee_code": None, "zoneIds": ["-4850450", "-4850451"],
}


@pytest.fixture
def log_messages():
    messages: list[str] = []
    sink_id = logger.add(lambda msg: messages.append(msg.record["message"]), level="DEBUG")
    yield messages
    logger.remove(sink_id)


@pytest.fixture
def repo():
    double = MagicMock(spec=BienIciGeoRepository)
    double.get_cached.return_value = None
    return double


@pytest.fixture
def suggest(monkeypatch):
    """Remplace `_query_suggest` et enregistre les textes demandés."""

    def install(results, side_effect=None):
        asked: list[str] = []

        def fake_query(text):
            asked.append(text)
            if side_effect is not None:
                raise side_effect
            return results(text) if callable(results) else results

        monkeypatch.setattr(geo, "_query_suggest", fake_query)
        return asked

    return install


# ---------------------------------------------------------------------------
# _query_suggest
# ---------------------------------------------------------------------------

class TestQuerySuggest:
    def test_the_request_matches_the_site_s_own_search_bar(self, requests_mock):
        mock = requests_mock.get(geo.SUGGEST_URL, json=[PARIS_15E])

        assert geo._query_suggest("75015") == [PARIS_15E]

        request = mock.last_request
        assert request.qs["q"] == ["75015"]
        assert request.headers["Referer"] == "https://www.bienici.com/"
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        assert "Chrome" in request.headers["User-Agent"]

    @pytest.mark.parametrize("text", ["", "p", None], ids=["empty", "one_letter", "none"])
    def test_a_text_shorter_than_two_characters_never_reaches_the_network(self, text, requests_mock):
        mock = requests_mock.get(geo.SUGGEST_URL, json=[PARIS_15E])

        assert geo._query_suggest(text) == []

        assert mock.call_count == 0

    @pytest.mark.parametrize("status", [400, 429, 500, 503])
    def test_an_http_error_is_raised_not_swallowed(self, status, requests_mock):
        requests_mock.get(geo.SUGGEST_URL, status_code=status, json={})

        with pytest.raises(Exception, match=str(status)):
            geo._query_suggest("75015")

    def test_a_non_list_response_is_treated_as_no_result(self, requests_mock):
        requests_mock.get(geo.SUGGEST_URL, json={"error": "oops"})

        assert geo._query_suggest("75015") == []


# ---------------------------------------------------------------------------
# _pick_city_match
# ---------------------------------------------------------------------------

class TestPickCityMatch:
    @pytest.mark.parametrize(
        "results", [[PARIS_WHOLE_CITY, PARIS_15E], [PARIS_15E, PARIS_WHOLE_CITY]],
        ids=["broad_first", "exact_first"],
    )
    def test_an_exact_postal_code_match_wins_whatever_the_api_order(self, results):
        """« Paris » entier contient AUSSI le 75015 dans une lecture naïve :
        sans la préférence stricte, une recherche sur le 15e ratisserait
        les 20 arrondissements."""
        assert geo._pick_city_match(results, "75015") == PARIS_15E

    def test_a_broader_entry_containing_the_code_is_the_second_choice(self):
        assert geo._pick_city_match([NANTES], "44100") == NANTES

    def test_no_match_returns_none(self):
        """Contrairement à SeLoger, pas de repli sur le premier résultat :
        une correspondance de type/code postal explicite est exigée, sinon
        None plutôt qu'un périmètre au hasard."""
        assert geo._pick_city_match([NANTES], "99999") is None

    def test_a_department_or_region_entry_never_matches_a_postal_code_query(self):
        assert geo._pick_city_match([GIRONDE_DEPT], "33000") is None


# ---------------------------------------------------------------------------
# _find_city_zone_ids / _find_whole_city_zone_ids / _find_department_zone_ids
# ---------------------------------------------------------------------------

class TestFindCityZoneIds:
    def test_the_query_uses_the_postal_code_and_nothing_else(self, suggest):
        asked = suggest([PARIS_WHOLE_CITY, PARIS_15E])

        assert geo._find_city_zone_ids("75015") == ["-9520"]
        assert asked == ["75015"]

    def test_no_match_means_no_zone_ids(self, suggest):
        suggest([])

        assert geo._find_city_zone_ids("44000") is None


class TestFindWholeCityZoneIds:
    def test_found_by_name_filtered_to_the_city_type(self, suggest):
        asked = suggest([PARIS_15E, PARIS_WHOLE_CITY])

        assert geo._find_whole_city_zone_ids("Paris", "75056") == ["-7444"]
        assert asked == ["Paris"]

    def test_an_insee_mismatch_returns_none_directly(self, suggest):
        """Deux communes homonymes ne doivent jamais se substituer l'une à
        l'autre — même garde-fou que SeLoger sur le type_key attendu."""
        suggest([{"name": "Paris", "type": "city", "insee_code": "99999", "zoneIds": ["-1"]}])

        assert geo._find_whole_city_zone_ids("Paris", "75056") is None

    def test_a_city_type_match_with_no_zone_ids_is_skipped_in_favour_of_the_next(self, suggest):
        """Une entrée du bon type et du bon insee_code mais sans zoneIds
        exploitables (réponse tronquée) ne doit pas arrêter la recherche
        prématurément sur un `None` alors qu'une entrée plus loin convient."""
        suggest([
            {"name": "Paris", "type": "city", "insee_code": "75056", "zoneIds": []},
            {"name": "Paris", "type": "city", "insee_code": "75056", "zoneIds": ["-7444"]},
        ])

        assert geo._find_whole_city_zone_ids("Paris", "75056") == ["-7444"]

    def test_no_insee_code_known_accepts_the_first_city_type_match(self, suggest):
        suggest([PARIS_15E, PARIS_WHOLE_CITY])

        assert geo._find_whole_city_zone_ids("Paris", None) == ["-7444"]


class TestFindDepartmentZoneIds:
    def test_resolved_directly_by_its_insee_code_not_by_name(self, suggest):
        """Vérifié en direct : `q=33` renvoie le département en tête, sans
        ambiguïté — contrairement à SeLoger qui doit chercher par nom."""
        asked = suggest([GIRONDE_DEPT])

        assert geo._find_department_zone_ids("33") == ["-7405"]
        assert asked == ["33"]

    def test_a_homonymous_commune_is_never_picked(self, suggest):
        """« 33 » peut aussi faire remonter une commune : seul le type
        "department" au bon insee_code compte."""
        suggest([GIRONDE_COMMUNE, GIRONDE_DEPT])

        assert geo._find_department_zone_ids("33") == ["-7405"]

    def test_no_department_match_returns_none(self, suggest):
        suggest([GIRONDE_COMMUNE])

        assert geo._find_department_zone_ids("33") is None

    def test_an_alias_department_can_carry_several_zone_ids(self, suggest):
        suggest([RHONE_ALIAS, {"name": "Rhône", "type": "department", "insee_code": "69D", "zoneIds": ["-4850451"]}])

        assert geo._find_department_zone_ids("69D") == ["-4850451"]


# ---------------------------------------------------------------------------
# _find_region_zone_ids — union des départements
# ---------------------------------------------------------------------------

class TestFindRegionZoneIds:
    def test_it_unions_the_zone_ids_of_every_department_in_the_region(self, monkeypatch):
        monkeypatch.setattr(geo, "region_departments", lambda code: ["33", "40"], raising=False)
        monkeypatch.setattr("core.geocode.region_departments", lambda code: ["33", "40"])
        calls = []

        def fake_find_dept(code):
            calls.append(code)
            return {"33": ["-7405"], "40": ["-7440", "-7441"]}[code]

        monkeypatch.setattr(geo, "_find_department_zone_ids", fake_find_dept)

        result = geo._find_region_zone_ids({"kind": REGION, "code": "75"})

        assert calls == ["33", "40"]
        assert result == ["-7405", "-7440", "-7441"]

    def test_duplicate_zone_ids_across_departments_are_not_repeated(self, monkeypatch):
        monkeypatch.setattr("core.geocode.region_departments", lambda code: ["33", "40"])
        monkeypatch.setattr(geo, "_find_department_zone_ids", lambda code: ["-1"])

        assert geo._find_region_zone_ids({"kind": REGION, "code": "75"}) == ["-1"]

    def test_a_department_that_fails_to_resolve_is_simply_skipped(self, monkeypatch):
        monkeypatch.setattr("core.geocode.region_departments", lambda code: ["33", "40"])
        monkeypatch.setattr(geo, "_find_department_zone_ids", lambda code: None if code == "40" else ["-7405"])

        assert geo._find_region_zone_ids({"kind": REGION, "code": "75"}) == ["-7405"]

    def test_no_code_means_no_lookup_at_all(self, monkeypatch):
        calls = []
        monkeypatch.setattr("core.geocode.region_departments", lambda code: calls.append(code) or [])

        assert geo._find_region_zone_ids({"kind": REGION}) is None
        assert calls == []

    def test_no_department_resolves_to_anything_returns_none(self, monkeypatch):
        monkeypatch.setattr("core.geocode.region_departments", lambda code: ["33"])
        monkeypatch.setattr(geo, "_find_department_zone_ids", lambda code: None)

        assert geo._find_region_zone_ids({"kind": REGION, "code": "75"}) is None


# ---------------------------------------------------------------------------
# area_cache_key — même convention que SeLoger
# ---------------------------------------------------------------------------

class TestAreaCacheKey:
    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            (make_city_location(insee="75115"), "75115"),
            (make_whole_city_location(insee="33063"), "city:33063"),
            (make_department_location(code="33"), "dept:33"),
            (make_region_location(code="75"), "region:75"),
        ],
        ids=["city", "whole_city", "department", "region"],
    )
    def test_each_level_has_its_own_namespace(self, location, expected):
        assert geo.area_cache_key(location) == expected

    def test_the_same_code_at_two_levels_never_collides(self):
        keys = {
            geo.area_cache_key(make_department_location(code="75")),
            geo.area_cache_key(make_region_location(code="75")),
            geo.area_cache_key(make_city_location(insee="75")),
        }
        assert len(keys) == 3, f"collision de clés : {keys}"

    @pytest.mark.parametrize(
        ("location", "case"),
        [
            ({"kind": CITY, "inseeCode": ""}, "code INSEE vide, pas de code postal non plus"),
            ({"kind": WHOLE_CITY}, "ville entière sans code INSEE ni nom de ville"),
            ({"kind": REGION, "name": "Nulle part"}, "région sans code"),
            ({"kind": DEPARTMENT, "code": ""}, "département à code vide"),
        ],
        ids=["empty_insee_and_postal", "whole_city_no_insee_no_city", "region_no_code", "dept_empty_code"],
    )
    def test_an_unidentifiable_area_has_no_key(self, location, case):
        assert geo.area_cache_key(location) is None, case

    def test_a_city_without_insee_falls_back_to_its_postal_code(self):
        """# BUG corrigé : une commune tapée à la main (sans code INSEE) a
        quand même une clé, dérivée du code postal — `_find_city_zone_ids`
        résout déjà par ce seul code postal, la clé n'a pas besoin de plus."""
        assert geo.area_cache_key({"kind": CITY, "city": "Paris", "postalCode": "75015"}) == "postal:75015"

    def test_a_whole_city_without_insee_falls_back_to_its_name(self):
        assert geo.area_cache_key({"kind": WHOLE_CITY, "city": "Poitiers"}) == "city_name:poitiers"


# ---------------------------------------------------------------------------
# _resolve_uncached — routage par niveau
# ---------------------------------------------------------------------------

class TestResolveUncached:
    def test_city_is_routed_to_find_city_zone_ids(self, monkeypatch):
        monkeypatch.setattr(geo, "_find_city_zone_ids", lambda pc: ["-1"] if pc == "75015" else None)

        assert geo._resolve_uncached(make_city_location(postal_code="75015")) == ["-1"]

    def test_whole_city_is_routed_by_city_name_and_insee(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            geo, "_find_whole_city_zone_ids",
            lambda city, insee: calls.append((city, insee)) or ["-2"],
        )

        assert geo._resolve_uncached(make_whole_city_location(city="Poitiers", insee="86194")) == ["-2"]
        assert calls == [("Poitiers", "86194")]

    def test_department_is_routed_by_code(self, monkeypatch):
        calls = []
        monkeypatch.setattr(geo, "_find_department_zone_ids", lambda code: calls.append(code) or ["-3"])

        assert geo._resolve_uncached(make_department_location(code="33")) == ["-3"]
        assert calls == ["33"]

    def test_region_is_routed_to_the_union_lookup(self, monkeypatch):
        location = make_region_location(code="75")
        calls = []
        monkeypatch.setattr(geo, "_find_region_zone_ids", lambda loc: calls.append(loc) or ["-4"])

        assert geo._resolve_uncached(location) == ["-4"]
        assert calls == [location]

    def test_an_unknown_kind_resolves_to_none(self):
        assert geo._resolve_uncached({"kind": "canton", "name": "Gironde"}) is None

    @pytest.mark.parametrize(
        ("error", "case"),
        [
            (ConnectionError("réseau coupé"), "panne réseau"),
            (ValueError("réponse illisible"), "JSON inattendu"),
            (KeyError("id"), "champ manquant"),
            (TimeoutError("délai dépassé"), "timeout"),
        ],
        ids=["network", "bad_json", "missing_field", "timeout"],
    )
    def test_it_never_raises_whatever_goes_wrong(self, suggest, log_messages, error, case):
        suggest([], side_effect=error)

        assert geo._resolve_uncached(make_city_location()) is None, case
        assert geo._resolve_uncached(make_department_location()) is None, case
        assert any("Résolution échouée" in m for m in log_messages)

    def test_a_city_without_postal_code_is_swallowed(self, suggest):
        suggest([])

        assert geo._resolve_uncached({"kind": CITY, "city": "Paris"}) is None


# ---------------------------------------------------------------------------
# _seconds_since
# ---------------------------------------------------------------------------

class TestSecondsSince:
    @freeze_time(FROZEN)
    def test_none_has_no_age(self):
        assert geo._seconds_since(None) is None

    @freeze_time(FROZEN)
    def test_a_naive_datetime_is_compared_against_utc_now(self):
        naive = datetime.datetime(2026, 7, 26, 10, 0, 0)

        assert geo._seconds_since(naive) == 2 * 3600

    @pytest.mark.parametrize(
        "tzinfo",
        [datetime.UTC, datetime.timezone(datetime.timedelta(hours=2)), datetime.timezone(datetime.timedelta(hours=-5))],
        ids=["utc", "plus_two", "minus_five"],
    )
    @freeze_time(FROZEN)
    def test_an_aware_datetime_is_compared_in_its_own_timezone(self, tzinfo):
        instant = datetime.datetime(2026, 7, 26, 10, 0, 0, tzinfo=datetime.UTC)

        assert geo._seconds_since(instant.astimezone(tzinfo)) == 2 * 3600


# ---------------------------------------------------------------------------
# resolve_zone_ids — la table de vérité du cache (même forme que SeLoger)
# ---------------------------------------------------------------------------

class TestResolveZoneIdsCacheTruthTable:
    @staticmethod
    def spy_resolution(monkeypatch, result=("-7444",)):
        result = list(result) if result else result
        calls: list[dict] = []
        monkeypatch.setattr(geo, "_resolve_uncached", lambda location: calls.append(location) or result)
        return calls

    @freeze_time(FROZEN)
    def test_an_unidentifiable_area_warns_and_never_touches_the_cache(self, repo, monkeypatch, log_messages):
        """Une commune avec un code postal (même farfelu, "99999") a une clé
        de repli désormais (voir area_cache_key) : ce n'est plus le cas
        « non identifiable ». Il ne reste que l'absence totale d'information
        exploitable (ni ville, ni code postal, ni code INSEE)."""
        calls = self.spy_resolution(monkeypatch)
        location = {"kind": CITY}

        assert geo.resolve_zone_ids(location, repo=repo) is None

        assert calls == []
        repo.get_cached.assert_not_called()
        repo.set_cached.assert_not_called()
        assert any("Périmètre non identifiable" in m for m in log_messages)

    @freeze_time(FROZEN)
    def test_a_hand_typed_postal_code_now_resolves_through_its_fallback_key(self, repo, monkeypatch):
        """# BUG corrigé : une localisation tapée à la main (sans code INSEE)
        échouait systématiquement ici faute de clé de cache, alors que
        `_find_city_zone_ids` résout déjà par le seul code postal."""
        calls = self.spy_resolution(monkeypatch, result=["-7444"])
        repo.get_cached.return_value = None
        location = {"kind": CITY, "city": "Saisie manuelle", "postalCode": "99999"}

        assert geo.resolve_zone_ids(location, repo=repo) == ["-7444"]

        assert calls == [location]
        repo.get_cached.assert_called_once_with("postal:99999")
        repo.set_cached.assert_called_once_with("postal:99999", ["-7444"])

    @freeze_time(FROZEN)
    def test_cached_zone_ids_are_returned_immediately(self, repo, monkeypatch):
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = {"zone_ids": ["-9520"], "resolved_at": None}

        assert geo.resolve_zone_ids(make_city_location(insee="75115"), repo=repo) == ["-9520"]

        repo.get_cached.assert_called_once_with("75115")
        assert calls == []
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        "age_seconds", [0, 3600, 7 * 24 * 3600 - 1], ids=["just_now", "one_hour", "one_second_before_expiry"],
    )
    @freeze_time(FROZEN)
    def test_a_recent_failure_is_not_retried(self, repo, monkeypatch, age_seconds):
        calls = self.spy_resolution(monkeypatch)
        resolved_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=age_seconds)
        repo.get_cached.return_value = {"zone_ids": None, "resolved_at": resolved_at}

        assert geo.resolve_zone_ids(make_city_location(), repo=repo) is None

        assert calls == []
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        "age_seconds", [7 * 24 * 3600, 30 * 24 * 3600], ids=["exactly_seven_days", "one_month"],
    )
    @freeze_time(FROZEN)
    def test_an_expired_failure_is_retried_and_rewritten(self, repo, monkeypatch, age_seconds):
        calls = self.spy_resolution(monkeypatch)
        resolved_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=age_seconds)
        repo.get_cached.return_value = {"zone_ids": None, "resolved_at": resolved_at}

        assert geo.resolve_zone_ids(make_city_location(insee="75115"), repo=repo) == ["-7444"]

        assert len(calls) == 1
        repo.set_cached.assert_called_once_with("75115", ["-7444"])

    @freeze_time(FROZEN)
    def test_a_failure_without_a_resolution_date_is_retried(self, repo, monkeypatch):
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = {"zone_ids": None, "resolved_at": None}

        assert geo.resolve_zone_ids(make_city_location(), repo=repo) == ["-7444"]
        assert len(calls) == 1

    @freeze_time(FROZEN)
    def test_a_first_lookup_resolves_and_caches(self, repo, monkeypatch, log_messages):
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = None
        location = make_city_location(city="Paris", postal_code="75015", insee="75115")

        assert geo.resolve_zone_ids(location, repo=repo) == ["-7444"]

        assert calls == [location]
        repo.set_cached.assert_called_once_with("75115", ["-7444"])
        assert any("Paris (75015) -> ['-7444']" in m for m in log_messages)

    @freeze_time(FROZEN)
    def test_a_failed_resolution_is_cached_as_none_with_a_warning(self, repo, monkeypatch, log_messages):
        self.spy_resolution(monkeypatch, result=None)
        repo.get_cached.return_value = None

        assert geo.resolve_zone_ids(make_city_location(insee="75115"), repo=repo) is None

        repo.set_cached.assert_called_once_with("75115", None)
        assert any("Aucun zoneId trouvé" in m for m in log_messages)

    @pytest.mark.parametrize(
        ("location", "expected_key"),
        [
            (make_city_location(insee="75115"), "75115"),
            (make_whole_city_location(insee="86194"), "city:86194"),
            (make_department_location(code="33"), "dept:33"),
            (make_region_location(code="75"), "region:75"),
        ],
        ids=["city", "whole_city", "department", "region"],
    )
    @freeze_time(FROZEN)
    def test_the_cache_is_read_and_written_with_the_level_qualified_key(
        self, repo, monkeypatch, location, expected_key
    ):
        self.spy_resolution(monkeypatch)

        geo.resolve_zone_ids(location, repo=repo)

        repo.get_cached.assert_called_once_with(expected_key)
        repo.set_cached.assert_called_once_with(expected_key, ["-7444"])


# ---------------------------------------------------------------------------
# remember_manual_zone_ids
# ---------------------------------------------------------------------------

class TestRememberManualZoneIds:
    @staticmethod
    def criteria_with(zone_ids, locations):
        from tests.helpers.factories import make_criteria

        return make_criteria(locations=locations, sourceOverrides={"bienici": {"zoneIds": zone_ids}})

    def test_zone_ids_and_one_location_are_banked_together(self, repo):
        criteria = self.criteria_with(["-9520"], [make_city_location(insee="75115")])

        geo.remember_manual_zone_ids(criteria, repo=repo)

        repo.set_cached.assert_called_once_with("75115", ["-9520"])

    def test_an_existing_cache_entry_is_never_overwritten(self, repo):
        repo.get_cached.return_value = {"zone_ids": ["-9520"], "resolved_at": None}
        criteria = self.criteria_with(["-1"], [make_city_location(insee="75115")])

        geo.remember_manual_zone_ids(criteria, repo=repo)

        repo.get_cached.assert_called_once_with("75115")
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        ("zone_ids", "locations", "case"),
        [
            (["-1"], [], "aucune localisation"),
            (
                ["-1"],
                [
                    make_city_location(insee="75115"),
                    make_city_location(city="Lyon", postal_code="69007", insee="69387"),
                ],
                "deux localisations",
            ),
            ([], [make_city_location(insee="75115")], "aucun zoneId"),
        ],
        ids=["no_location", "two_locations", "no_zone_id"],
    )
    def test_nothing_to_remember_is_a_silent_no_op(self, repo, zone_ids, locations, case):
        criteria = self.criteria_with(zone_ids, locations)

        geo.remember_manual_zone_ids(criteria, repo=repo)

        repo.set_cached.assert_not_called(), case

    def test_a_location_without_an_insee_code_is_still_banked_under_its_postal_key(self, repo):
        """# BUG corrigé : un zoneId collé à la main pour une localisation
        sans code INSEE (tapée à la main) n'était jamais mémorisé faute de
        clé, alors que `area_cache_key` en calcule désormais une de repli
        (`postal:<code>`) — voir services.bienici_geocode.area_cache_key."""
        repo.get_cached.return_value = None
        criteria = self.criteria_with(["-1"], [{"kind": CITY, "city": "Paris", "postalCode": "75015"}])

        geo.remember_manual_zone_ids(criteria, repo=repo)

        repo.set_cached.assert_called_once_with("postal:75015", ["-1"])

    def test_a_location_with_no_key_at_all_has_nothing_to_key_on(self, repo):
        criteria = self.criteria_with(["-1"], [{"kind": CITY}])

        geo.remember_manual_zone_ids(criteria, repo=repo)

        repo.get_cached.assert_not_called()
        repo.set_cached.assert_not_called()
