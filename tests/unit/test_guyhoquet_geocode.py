"""Tests unitaires de `services/guyhoquet_geocode.py`.

Ce module traduit un périmètre canonique (voir core/criteria.py) en slug
d'URL Guy Hoquet (`toulouse-31000_c3`, `lyon-69123_c4`, `31_c2`, `76_c1`),
en interrogeant l'autocomplete public du site et en mémorisant le résultat —
même rôle que services/century21_geocode.py et services/seloger_geocode.py,
dont ce fichier de test reprend la structure.

Deux différences de fond avec Century 21, chacune avec sa section de tests :

  * départements et régions se DÉRIVENT PUREMENT du code canonique
    (`31` -> `31_c2`, Corse minuscule comprise `2a_c2`) : zéro réseau, zéro
    cache pour ces deux niveaux — seules les villes consultent l'autocomplete.
    Les régions DROM (01/02/03/04/06) n'existent pas côté site -> None ;
  * le slug `_c4` (ville entière) embarque le code INSEE RÉEL fourni par le
    site (`lyon-69123_c4`, `ajaccio-2A004_c4`) : c'est la seule source où la
    correspondance avec l'`inseeCode` canonique est contrôlable exactement.

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
from repositories.guyhoquet_geo_repo import GuyHoquetGeoRepository
from services import guyhoquet_geocode as geo
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)

FROZEN = "2026-08-23 12:00:00"

# Réponses de l'autocomplete, telles que documentées d'après les captures du
# 23/08/2026 : `location_type` 1=région 2=département 3=commune+CP 4=ville
# entière (INSEE réel embarqué dans le slug).
TOULOUSE_C3 = {"slug": "toulouse-31000_c3", "name": "Toulouse", "zip": "31000", "location_type": 3}
LYON_C3 = {"slug": "lyon-69003_c3", "name": "Lyon", "zip": "69003", "location_type": 3}
VILLEURBANNE_C3 = {"slug": "villeurbanne-69100_c3", "name": "Villeurbanne", "zip": "69100", "location_type": 3}
IVRY_C3 = {"slug": "ivry-sur-seine-94200_c3", "name": "Ivry-sur-Seine", "zip": "94200", "location_type": 3}
LYON_C4 = {"slug": "lyon-69123_c4", "name": "Lyon", "zip": "69003", "location_type": 4}
VILLEURBANNE_C4 = {"slug": "villeurbanne-69100_c4", "name": "Villeurbanne", "zip": "69100", "location_type": 4}
AJACCIO_C4 = {"slug": "ajaccio-2A004_c4", "name": "Ajaccio", "zip": "20090", "location_type": 4}
# Bruit non français : l'autocomplete mélange des entrées canadiennes préfixées
# `ca-` (vérifié en direct), à écarter quel que soit leur location_type.
CANADIAN_C2 = {"slug": "ca-qc-11_c2", "name": "Montréal", "zip": "", "location_type": 2}
CANADIAN_C3 = {"slug": "ca-qc-h2x_c3", "name": "Montréal", "zip": "", "location_type": 3}


@pytest.fixture
def log_messages():
    messages: list[str] = []
    sink_id = logger.add(lambda msg: messages.append(msg.record["message"]), level="DEBUG")
    yield messages
    logger.remove(sink_id)


@pytest.fixture
def repo():
    """Le cache persistant, doublé. `spec=` interdit d'appeler une méthode que
    le vrai repository n'a pas."""
    double = MagicMock(spec=GuyHoquetGeoRepository)
    double.get_cached.return_value = None
    return double


@pytest.fixture
def autocomplete(monkeypatch):
    """Remplace `_query_autocomplete` et enregistre les textes demandés."""

    def install(results, side_effect=None):
        asked: list[str] = []

        def fake_query(text):
            asked.append(text)
            if side_effect is not None:
                raise side_effect
            return results(text) if callable(results) else results

        monkeypatch.setattr(geo, "_query_autocomplete", fake_query)
        return asked

    return install


# ---------------------------------------------------------------------------
# _query_autocomplete
# ---------------------------------------------------------------------------

class TestQueryAutocomplete:
    def test_the_request_matches_the_site_s_own_search_bar(self, requests_mock):
        """Le contrat exact avec l'endpoint : un GET sur `/biens/search-
        localization` avec `q`. L'endpoint est public : un User-Agent desktop
        et l'Accept JSON suffisent, aucun header de session (curl nu vérifié
        en direct)."""
        mock = requests_mock.get(
            geo.SUGGEST_URL, json={"success": True, "cities": [TOULOUSE_C3]}
        )

        assert geo._query_autocomplete("31000") == [TOULOUSE_C3]

        request = mock.last_request
        assert request.qs["q"] == ["31000"]
        assert "Chrome" in request.headers["User-Agent"]
        assert request.headers["Accept"] == "application/json"

    @pytest.mark.parametrize("text", ["", "p", None], ids=["empty", "one_letter", "none"])
    def test_a_text_shorter_than_two_characters_never_reaches_the_network(self, text, requests_mock):
        mock = requests_mock.get(geo.SUGGEST_URL, json={"success": True, "cities": []})

        assert geo._query_autocomplete(text) == []

        assert mock.call_count == 0

    @pytest.mark.parametrize("status", [400, 429, 500, 503])
    def test_an_http_error_is_raised_not_swallowed(self, status, requests_mock):
        requests_mock.get(geo.SUGGEST_URL, status_code=status, json={})

        with pytest.raises(Exception, match=str(status)):
            geo._query_autocomplete("31000")

    @pytest.mark.parametrize(
        ("body", "case"),
        [
            ({"success": False, "cities": []}, "success faux"),
            ({"error": "oops"}, "success absent"),
            ({"success": True}, "cities absent"),
            ({"success": True, "cities": "nawak"}, "cities pas une liste"),
        ],
        ids=["echec_site", "pas_de_success", "pas_de_cities", "cities_invalide"],
    )
    def test_an_unexpected_response_is_treated_as_no_result(self, body, case, requests_mock):
        """Une réponse inattendue vaut liste vide : l'échec sera mémorisé comme
        tel par l'appelant plutôt que de lever en pleine résolution."""
        requests_mock.get(geo.SUGGEST_URL, json=body)

        assert geo._query_autocomplete("31000") == [], case

    def test_non_dict_entries_are_filtered_out(self, requests_mock):
        body = {"success": True, "cities": [TOULOUSE_C3, "nawak", None]}

        requests_mock.get(geo.SUGGEST_URL, json=body)

        assert geo._query_autocomplete("31000") == [TOULOUSE_C3]


# ---------------------------------------------------------------------------
# _usable_entries — le tri par location_type et le bruit non français
# ---------------------------------------------------------------------------

class TestUsableEntries:
    def test_only_the_requested_location_type_survives(self):
        results = [{"slug": "76_c1", "location_type": 1}, LYON_C3, LYON_C4]

        assert geo._usable_entries(results, 4) == [LYON_C4]
        assert geo._usable_entries(results, 3) == [LYON_C3]

    def test_canadian_slugs_are_dropped_whatever_their_type(self):
        """🔒 Le site mélange des adresses canadiennes reconnaissables à leur
        slug préfixé `ca-` : elles ne correspondent jamais à un périmètre
        français canonique et pollueraient sinon les replis « premier du CP »."""
        results = [CANADIAN_C2, CANADIAN_C3, TOULOUSE_C3, LYON_C4]

        assert geo._usable_entries(results, 2) == []
        assert geo._usable_entries(results, 3) == [TOULOUSE_C3]
        assert geo._usable_entries(results, 4) == [LYON_C4]


# ---------------------------------------------------------------------------
# _normalize_name / _names_match
# ---------------------------------------------------------------------------

class TestNormalizeName:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Toulouse", "TOULOUSE"),
            ("Ivry-sur-Seine", "IVRY SUR SEINE"),
            ("IVRY SUR SEINE", "IVRY SUR SEINE"),
            # Accents et apostrophe : NFKD puis ASCII seul.
            ("Saint-Étienne", "SAINT ETIENNE"),
            ("L'Haÿ-les-Roses", "L HAY LES ROSES"),
        ],
    )
    def test_names_are_upper_ascii_and_compact(self, text, expected):
        assert geo._normalize_name(text) == expected


class TestNamesMatch:
    @pytest.mark.parametrize(
        ("entry", "city", "expected"),
        [
            ("Toulouse", "Toulouse", True),
            ("Ivry-sur-Seine", "Ivry-sur-Seine", True),
            # Casse, accents et séparateurs convergent.
            ("IVRY SUR SEINE", "Ivry-sur-Seine", True),
            # Même zone, ville différente : ne doit PAS matcher.
            ("Lyon", "Villeurbanne", False),
        ],
    )
    def test_the_name_carries_the_disambiguation(self, entry, city, expected):
        assert geo._names_match(entry, city) is expected

    @pytest.mark.parametrize(
        ("entry", "city"),
        [("", "Toulouse"), (None, "Toulouse"), ("Lyon", ""), ("Lyon", None)],
        ids=["entry_vide", "entry_none", "city_vide", "city_none"],
    )
    def test_a_missing_name_never_matches(self, entry, city):
        assert geo._names_match(entry, city) is False


# ---------------------------------------------------------------------------
# _dashed_slug / _query_with_dash_retry — les noms multi-mots exigent des tirets
# ---------------------------------------------------------------------------

class TestDashedSlug:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Île-de-France", "ile-de-france"),
            ("Le Mans", "le-mans"),
            ("Toulouse", "toulouse"),
        ],
    )
    def test_the_form_the_autocomplete_expects(self, text, expected):
        assert geo._dashed_slug(text) == expected


class TestQueryWithDashRetry:
    def test_a_direct_hit_is_not_retried(self, autocomplete):
        asked = autocomplete([TOULOUSE_C3])

        results = geo._query_with_dash_retry("Saint-Étienne")

        assert results == [TOULOUSE_C3]
        assert asked == ["Saint-Étienne"]

    def test_an_empty_answer_is_retried_with_dashes(self, autocomplete):
        """« Le Mans » brut ne trouve rien alors que « le-mans » trouve
        (vérifié en direct) : une seule retente slugifiée, pas de martèlement."""
        asked = autocomplete(lambda text: [TOULOUSE_C3] if text == "le-mans" else [])

        results = geo._query_with_dash_retry("Le Mans")

        assert results == [TOULOUSE_C3]
        assert asked == ["Le Mans", "le-mans"]

    def test_an_already_sluggified_name_is_not_retried(self, autocomplete):
        asked = autocomplete([])

        assert geo._query_with_dash_retry("ile-de-france") == []

        assert asked == ["ile-de-france"]


# ---------------------------------------------------------------------------
# _insee_from_whole_city_slug — l'INSEE réel embarqué par le site
# ---------------------------------------------------------------------------

class TestInseeFromWholeCitySlug:
    @pytest.mark.parametrize(
        ("slug", "expected"),
        [
            ("lyon-69123_c4", "69123"),
            # Corse : l'INSEE porte une lettre, rendue en MAJUSCULE.
            ("ajaccio-2A004_c4", "2A004"),
            ("ajaccio-2a004_c4", "2A004"),
        ],
        ids=["metropole", "corse_majuscule", "corse_minuscule"],
    )
    def test_the_embedded_code_is_extracted_case_insensitively(self, slug, expected):
        assert geo._insee_from_whole_city_slug(slug) == expected

    @pytest.mark.parametrize(
        "slug",
        ["toulouse-31000_c3", "lyon_c4", "", "nawak"],
        ids=["mauvais_suffixe", "pas_de_code", "vide", "sans_chiffres"],
    )
    def test_a_slug_of_another_shape_has_no_insee(self, slug):
        """Un slug `_c3` ne passe PAS pour un `_c4` : le contrôle par INSEE ne
        doit jamais valider le mauvais niveau."""
        assert geo._insee_from_whole_city_slug(slug) is None


# ---------------------------------------------------------------------------
# _pick_city_slug — commune + code postal
# ---------------------------------------------------------------------------

class TestPickCitySlug:
    def test_the_entry_matching_both_zip_and_name_wins(self):
        results = [VILLEURBANNE_C3, LYON_C3]

        assert geo._pick_city_slug(results, "69003", "Lyon") == "lyon-69003_c3"
        assert geo._pick_city_slug(results, "69100", "Villeurbanne") == "villeurbanne-69100_c3"

    @pytest.mark.parametrize(
        "results",
        [[LYON_C3, VILLEURBANNE_C3], [VILLEURBANNE_C3, LYON_C3]],
        ids=["lyon_first", "villeurbanne_first"],
    )
    def test_the_name_match_wins_whatever_the_api_order(self, results):
        assert geo._pick_city_slug(results, "69003", "Lyon") == "lyon-69003_c3"

    def test_fallback_to_the_first_entry_of_the_postal_code(self):
        """Les codes postaux partagés sont LE cas ambigu (69003 = Lyon 3e ET
        Villeurbanne) ; quand le nom affiché ne se rapproche pas, la première
        entrée du même CP reste le bon périmètre — le contrôle
        matches_locations en aval garde le résultat honnête."""
        abridged = {"slug": "st-etienne-42000_c3", "name": "ST ETIENNE", "zip": "42000", "location_type": 3}

        assert geo._pick_city_slug([abridged], "42000", "Saint-Étienne") == "st-etienne-42000_c3"

    def test_an_entry_of_another_postal_code_is_never_a_candidate(self):
        assert geo._pick_city_slug([LYON_C3], "75001", "Lyon") is None

    def test_a_canadian_entry_of_the_same_shape_is_ignored(self):
        results = [CANADIAN_C3, {"slug": "ca-qc-h2y_c3", "name": "Montréal", "zip": "42000", "location_type": 3}]

        assert geo._pick_city_slug(results, "42000", "Montréal") is None

    def test_no_candidate_returns_none(self):
        assert geo._pick_city_slug([], "31000", "Toulouse") is None


# ---------------------------------------------------------------------------
# _pick_whole_city_slug — ville entière, contrôle par INSEE embarqué
# ---------------------------------------------------------------------------

class TestPickWholeCitySlug:
    def test_the_slug_whose_embedded_insee_matches_wins(self):
        """Cas nominal : l'inseeCode vient de l'autocomplete du front et ne
        doit jamais être perdu — ici il permet de choisir EXACTEMENT la bonne
        ville entière parmi plusieurs candidats."""
        results = [VILLEURBANNE_C4, LYON_C4]

        assert geo._pick_whole_city_slug(results, "Lyon", "69123") == "lyon-69123_c4"

    def test_the_insee_comparison_is_uppercased_on_both_sides(self):
        assert geo._pick_whole_city_slug([AJACCIO_C4], "Ajaccio", "2a004") == "ajaccio-2A004_c4"

    def test_without_an_insee_the_name_decides_among_the_c4(self, ):
        assert geo._pick_whole_city_slug([AJACCIO_C4, LYON_C4], "Lyon", None) == "lyon-69123_c4"

    def test_small_communes_have_no_c4_and_fall_back_to_their_c3(self):
        """🔒 REPLI vérifié en direct (23/08/2026) : les petites communes n'ont
        PAS de slug `_c4` (« ivry » ne renvoie que des `_c3`). La commune
        mono-CP y supplée par son `_c3` au nom correspondant : ce slug couvre
        déjà toute la commune."""
        results = [IVRY_C3]

        assert geo._pick_whole_city_slug(results, "Ivry-sur-Seine", "94400", ["94200"]) == (
            "ivry-sur-seine-94200_c3"
        )

    def test_the_c3_fallback_respects_the_covered_postal_codes(self):
        """Le repli `_c3` exige que son code postal soit couvert par la
        localisation : un même nom peut porter plusieurs communes."""
        other_zip = {"slug": "ivry-sur-seine-94000_c3", "name": "Ivry-sur-Seine", "zip": "94000", "location_type": 3}

        assert geo._pick_whole_city_slug([other_zip], "Ivry-sur-Seine", "94400", ["94200"]) is None
        assert geo._pick_whole_city_slug([other_zip], "Ivry-sur-Seine", None, []) == "ivry-sur-seine-94000_c3"

    def test_the_c3_fallback_still_requires_a_name_match(self):
        mismatched = {"slug": "villeurbanne-69100_c3", "name": "Villeurbanne", "zip": "94200", "location_type": 3}

        assert geo._pick_whole_city_slug([mismatched], "Ivry-sur-Seine", None, ["94200"]) is None

    def test_a_canadian_c4_is_never_a_candidate(self):
        canadian_c4 = {"slug": "ca-qc-11_c4", "name": "Lyon", "zip": "", "location_type": 4}

        assert geo._pick_whole_city_slug([canadian_c4, LYON_C4], "Lyon", None) == "lyon-69123_c4"

    def test_nothing_matches_returns_none(self):
        assert geo._pick_whole_city_slug([], "Lyon", "69123") is None


# ---------------------------------------------------------------------------
# Dérivation statique région / département — sans réseau ni cache
# ---------------------------------------------------------------------------

class TestStaticSlugs:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [("76", "76_c1"), ("11", "11_c1")],
        ids=["normandie", "ile_de_france"],
    )
    def test_a_metropolitan_region_derives_its_c1(self, code, expected):
        assert geo._region_slug(code) == expected

    @pytest.mark.parametrize(
        "code",
        ["01", "02", "03", "04", "06"],
        ids=["guadeloupe", "martinique", "guyane", "reunion", "mayotte"],
    )
    def test_an_overseas_region_does_not_exist_for_the_site(self, code):
        """Les régions DROM ont des codes propres (différents de ceux des
        départements) et sont absentes de l'autocomplete (vérifié en direct)
        : aucune dérivation possible."""
        assert geo._region_slug(code) is None

    def test_a_region_without_a_code_derives_nothing(self):
        assert geo._region_slug(None) is None

    @pytest.mark.parametrize(
        ("code", "expected"),
        [("33", "33_c2"), ("2A", "2a_c2"), ("971", "971_c2")],
        ids=["gironde", "corse_minuscule", "drom_departement"],
    )
    def test_a_department_derives_its_lowercase_c2(self, code, expected):
        """Contrairement aux régions DROM, les départements ultramarins
        971..976 passent (vérifié en direct)."""
        assert geo._department_slug(code) == expected

    def test_a_department_without_a_code_derives_nothing(self):
        assert geo._department_slug(None) is None


# ---------------------------------------------------------------------------
# area_cache_key — même convention que SeLoger / bienici / Century 21
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

    def test_a_city_without_insee_falls_back_to_its_postal_code(self):
        assert geo.area_cache_key({"kind": CITY, "city": "Paris", "postalCode": "75015"}) == "postal:75015"

    def test_a_whole_city_without_insee_falls_back_to_its_name(self):
        assert geo.area_cache_key({"kind": WHOLE_CITY, "city": "Poitiers"}) == "city_name:poitiers"

    @pytest.mark.parametrize(
        ("location", "case"),
        [
            ({"kind": CITY}, "commune sans code INSEE ni code postal"),
            ({"kind": WHOLE_CITY}, "ville entière sans code INSEE ni nom"),
            ({"kind": DEPARTMENT}, "département sans code"),
            ({"kind": REGION}, "région sans code"),
        ],
        ids=["city_sans_rien", "whole_city_sans_rien", "dept_sans_code", "region_sans_code"],
    )
    def test_an_unidentifiable_area_has_no_key(self, location, case):
        assert geo.area_cache_key(location) is None, case


# ---------------------------------------------------------------------------
# is_statically_resolvable — ce qui se résout sans aucun réseau
# ---------------------------------------------------------------------------

class TestIsStaticallyResolvable:
    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            (make_region_location(code="76"), True),
            (make_department_location(code="33"), True),
            (make_department_location(code="2A"), True),
            # Régions DROM : rien à dériver, le site ne les référence pas.
            (make_region_location(code="01"), False),
            (make_city_location(), False),
            (make_whole_city_location(), False),
            ({}, False),
        ],
        ids=["region", "departement", "corse", "region_drom", "city", "whole_city", "vide"],
    )
    def test_only_regions_and_departments_are_static(self, location, expected):
        assert geo.is_statically_resolvable(location) is expected


# ---------------------------------------------------------------------------
# _resolve_uncached — routage par niveau (villes uniquement)
# ---------------------------------------------------------------------------

class TestResolveUncached:
    def test_a_city_queries_by_postal_code_and_picks_by_name(self, autocomplete):
        asked = autocomplete([TOULOUSE_C3])

        slug = geo._resolve_uncached(make_city_location("Toulouse", "31000", "31555"))

        assert slug == "toulouse-31000_c3"
        assert asked == ["31000"]

    def test_a_whole_city_queries_by_name_and_controls_the_insee(self, autocomplete):
        asked = autocomplete([LYON_C3, LYON_C4])

        slug = geo._resolve_uncached(make_whole_city_location("Lyon", ("69003",), "69123"))

        assert slug == "lyon-69123_c4"
        assert asked == ["Lyon"]

    @pytest.mark.parametrize(
        ("location", "case"),
        [(make_department_location(code="33"), "département"),
         (make_region_location(code="76"), "région")],
        ids=["departement", "region"],
    )
    def test_static_levels_never_query_the_autocomplete(self, autocomplete, location, case):
        """Départements et régions se dérivent purement : aucune requête, la
        résolution réseau n'est réservée qu'aux villes."""
        asked = autocomplete([TOULOUSE_C3])

        assert geo._resolve_uncached(location) is None, case

        assert asked == []

    def test_an_unknown_kind_resolves_to_none(self, autocomplete):
        asked = autocomplete([TOULOUSE_C3])

        assert geo._resolve_uncached({"kind": "canton", "name": "Toulouse"}) is None

        assert asked == []

    def test_a_city_without_postal_code_queries_nothing_usable(self, autocomplete):
        asked = autocomplete([])

        assert geo._resolve_uncached({"kind": CITY, "city": "Toulouse"}) is None

        assert asked == [""]

    @pytest.mark.parametrize(
        ("error", "case"),
        [
            (ConnectionError("réseau coupé"), "panne réseau"),
            (ValueError("réponse illisible"), "JSON inattendu"),
            (KeyError("slug"), "champ manquant"),
            (TimeoutError("délai dépassé"), "timeout"),
        ],
        ids=["network", "bad_json", "missing_field", "timeout"],
    )
    def test_it_never_raises_whatever_goes_wrong(self, autocomplete, log_messages, error, case):
        autocomplete([], side_effect=error)

        assert geo._resolve_uncached(make_city_location()) is None, case
        assert any("Résolution échouée" in m for m in log_messages)


# ---------------------------------------------------------------------------
# _seconds_since
# ---------------------------------------------------------------------------

class TestSecondsSince:
    @freeze_time(FROZEN)
    def test_none_has_no_age(self):
        assert geo._seconds_since(None) is None

    @freeze_time(FROZEN)
    def test_a_naive_datetime_is_compared_against_utc_now(self):
        naive = datetime.datetime(2026, 8, 23, 10, 0, 0)

        assert geo._seconds_since(naive) == 2 * 3600

    @pytest.mark.parametrize(
        "tzinfo",
        [datetime.UTC, datetime.timezone(datetime.timedelta(hours=2)), datetime.timezone(datetime.timedelta(hours=-5))],
        ids=["utc", "plus_two", "minus_five"],
    )
    @freeze_time(FROZEN)
    def test_an_aware_datetime_is_compared_in_its_own_timezone(self, tzinfo):
        instant = datetime.datetime(2026, 8, 23, 10, 0, 0, tzinfo=datetime.UTC)

        assert geo._seconds_since(instant.astimezone(tzinfo)) == 2 * 3600


# ---------------------------------------------------------------------------
# resolve_slug — la table de vérité du cache
# ---------------------------------------------------------------------------

class TestResolveSlugCacheTruthTable:
    @staticmethod
    def spy_resolution(monkeypatch, result="toulouse-31000_c3"):
        calls: list[dict] = []
        monkeypatch.setattr(
            geo, "_resolve_uncached", lambda location: calls.append(location) or result
        )
        return calls

    @freeze_time(FROZEN)
    def test_an_unidentifiable_area_warns_and_never_touches_the_cache(self, repo, monkeypatch, log_messages):
        calls = self.spy_resolution(monkeypatch)
        location = {"kind": CITY}

        assert geo.resolve_slug(location, repo) is None

        assert calls == []
        repo.get_cached.assert_not_called()
        repo.set_cached.assert_not_called()
        assert any("Périmètre non identifiable" in m for m in log_messages)

    # --- niveaux statiques : JAMAIS de cache ni de réseau ------------------

    @freeze_time(FROZEN)
    def test_a_region_derives_its_slug_purely(self, repo, monkeypatch):
        """Région et département se dérivent du code : le repo n'est ni lu ni
        écrit, aucune résolution réseau n'est tentée."""
        calls = self.spy_resolution(monkeypatch)

        assert geo.resolve_slug(make_region_location(code="76"), repo) == "76_c1"

        repo.get_cached.assert_not_called()
        repo.set_cached.assert_not_called()
        assert calls == []

    @freeze_time(FROZEN)
    def test_a_department_derives_its_slug_purely(self, repo, monkeypatch):
        self.spy_resolution(monkeypatch)

        assert geo.resolve_slug(make_department_location(code="2A"), repo) == "2a_c2"

        repo.get_cached.assert_not_called()
        repo.set_cached.assert_not_called()

    @freeze_time(FROZEN)
    def test_an_overseas_region_warns_and_returns_none(self, repo, monkeypatch, log_messages):
        self.spy_resolution(monkeypatch)

        assert geo.resolve_slug(make_region_location(code="01", name="Guadeloupe"), repo) is None

        repo.set_cached.assert_not_called()
        assert any("DROM" in m and "ignorée" in m for m in log_messages)

    # --- villes : le repo devient obligatoire ------------------------------

    @freeze_time(FROZEN)
    def test_a_city_without_a_repo_warns_instead_of_reading_flask(self, monkeypatch, log_messages):
        """🔒 Le scraping tourne sur un thread de fond, hors contexte Flask :
        sans storage injecté, la résolution renonce proprement au lieu de lire
        flask.current_app (régression historique de cette équipe)."""
        calls = self.spy_resolution(monkeypatch)

        assert geo.resolve_slug(make_city_location(), None) is None

        assert calls == []
        assert any("Aucun storage fourni" in m for m in log_messages)

    @freeze_time(FROZEN)
    def test_a_cached_slug_is_returned_immediately(self, repo, monkeypatch):
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = {"slug_id": "toulouse-31000_c3", "resolved_at": None}

        assert geo.resolve_slug(make_city_location(insee="31555"), repo) == "toulouse-31000_c3"

        repo.get_cached.assert_called_once_with("31555")
        assert calls == []
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        "age_seconds", [0, 3600, 7 * 24 * 3600 - 1], ids=["just_now", "one_hour", "one_second_before_expiry"]
    )
    @freeze_time(FROZEN)
    def test_a_recent_failure_is_not_retried(self, repo, monkeypatch, age_seconds):
        """🔒 L'échec mémorisé (slug_id NULL) coupe toute nouvelle tentative
        avant 7 jours : sans ce délai, un périmètre introuvable martèlerait
        l'autocomplete à chaque cycle de scrape (toutes les 30 s)."""
        calls = self.spy_resolution(monkeypatch)
        resolved_at = datetime.datetime(2026, 8, 23, 12, 0, 0) - datetime.timedelta(seconds=age_seconds)
        repo.get_cached.return_value = {"slug_id": None, "resolved_at": resolved_at}

        assert geo.resolve_slug(make_city_location(), repo) is None

        assert calls == []
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        "age_seconds", [7 * 24 * 3600, 30 * 24 * 3600], ids=["exactly_seven_days", "one_month"]
    )
    @freeze_time(FROZEN)
    def test_an_expired_failure_is_retried_and_rewritten(self, repo, monkeypatch, age_seconds):
        calls = self.spy_resolution(monkeypatch)
        resolved_at = datetime.datetime(2026, 8, 23, 12, 0, 0) - datetime.timedelta(seconds=age_seconds)
        repo.get_cached.return_value = {"slug_id": None, "resolved_at": resolved_at}

        assert geo.resolve_slug(make_city_location(insee="31555"), repo) == "toulouse-31000_c3"

        assert len(calls) == 1
        repo.set_cached.assert_called_once_with("31555", "toulouse-31000_c3")

    @freeze_time(FROZEN)
    def test_a_failure_without_a_resolution_date_is_retried(self, repo, monkeypatch):
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = {"slug_id": None, "resolved_at": None}

        assert geo.resolve_slug(make_city_location(), repo) == "toulouse-31000_c3"
        assert len(calls) == 1

    @freeze_time(FROZEN)
    def test_a_first_lookup_resolves_and_caches(self, repo, monkeypatch, log_messages):
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = None
        location = make_city_location(city="Toulouse", postal_code="31000", insee="31555")

        assert geo.resolve_slug(location, repo) == "toulouse-31000_c3"

        assert calls == [location]
        repo.set_cached.assert_called_once_with("31555", "toulouse-31000_c3")
        assert any("Toulouse (31000) -> toulouse-31000_c3" in m for m in log_messages)

    @freeze_time(FROZEN)
    def test_a_failed_resolution_is_cached_as_none_with_a_warning(self, repo, monkeypatch, log_messages):
        self.spy_resolution(monkeypatch, result=None)
        repo.get_cached.return_value = None

        assert geo.resolve_slug(make_city_location(insee="31555"), repo) is None

        repo.set_cached.assert_called_once_with("31555", None)
        assert any("Aucun slug trouvé" in m for m in log_messages)

    @pytest.mark.parametrize(
        ("location", "expected_key"),
        [
            (make_city_location(insee="75115"), "75115"),
            (make_whole_city_location(insee="86194"), "city:86194"),
        ],
        ids=["city", "whole_city"],
    )
    @freeze_time(FROZEN)
    def test_the_cache_is_read_and_written_with_the_area_key(self, repo, monkeypatch, location, expected_key):
        self.spy_resolution(monkeypatch)

        geo.resolve_slug(location, repo)

        repo.get_cached.assert_called_once_with(expected_key)
        repo.set_cached.assert_called_once_with(expected_key, "toulouse-31000_c3")
