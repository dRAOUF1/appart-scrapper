"""Tests unitaires de `services/century21_geocode.py`.

Ce module traduit un périmètre canonique (voir core/criteria.py) en slug d'URL
Century 21 (`v-paris`, `cp-75001`, `v-st+etienne`), en interrogeant
l'autocomplete public du site et en mémorisant le résultat — même rôle que
services/seloger_geocode.py et services/bienici_geocode.py, dont ce fichier de
test reprend la structure.

Deux différences de fond, chacune avec sa section de tests :

  * le slug est OPAQUE mais non dérivable du code INSEE : Century 21 normalise
    lui-même les noms (« SAINT-ÉTIENNE » devient « ST ETIENNE »), il faut donc
    toujours interroger l'autocomplete — la résolution croise le code postal
    avec le NOM de la ville, avec un repli sur la première ville de ce code
    postal quand les noms ne correspondent pas ;
  * un département se résout en slug COMPLET (`d-33_gironde`) : l'autocomplete
    renvoie une entrée à l'id incomplet (`d-33`) dont le suffixe de nom se
    dérive du libellé ; le slug nu renvoie 410 (vérifié en direct). Dérogations :
    la Corse (2A/2B) n'a aucune entrée et se requête par ses codes postaux
    201/202, et Paris (75) n'a pas d'entrée départementale — repli sur la ville
    entière. Une RÉGION, elle, n'a AUCUN identifiant propre (autocomplete muet)
    : `resolve_slug_id` renvoie None — c'est le parser qui l'élargit à ses
    départements.

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
from repositories.century21_geo_repo import Century21GeoRepository
from services import century21_geocode as geo
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)

FROZEN = "2026-08-15 12:00:00"

# Réponses de l'autocomplete, telles qu'observées en direct le 15/08/2026.
PARIS_CITY = {"id": "v-paris", "name": "PARIS", "cp": "75015"}
PARIS_15E = {"id": "cp-75015", "name": "PARIS (75015)", "cp": "75015"}
PARIS_1ER = {"id": "cp-75001", "name": "PARIS (75001)", "cp": "75001"}
LYON_CITY = {"id": "v-lyon", "name": "LYON", "cp": "69003"}
VILLEURBANNE = {"id": "v-villeurbanne", "name": "VILLEURBANNE", "cp": "69003"}
SAINT_ETIENNE = {"id": "v-st+etienne", "name": "ST ETIENNE", "cp": "42000"}


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
    double = MagicMock(spec=Century21GeoRepository)
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
        """Le contrat exact avec l'endpoint : un GET sur `/autocomplete/localite/`
        avec `q`, et les trois headers sans lesquels la réponse est vide
        (vérifié en direct) — Referer + X-Requested-With + User-Agent Chrome."""
        mock = requests_mock.get(geo.SUGGEST_URL, json=[PARIS_15E])

        assert geo._query_autocomplete("75015") == [PARIS_15E]

        request = mock.last_request
        assert request.qs["q"] == ["75015"]
        assert request.headers["Referer"] == "https://www.century21.fr/"
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        assert "Chrome" in request.headers["User-Agent"]

    @pytest.mark.parametrize("text", ["", "p", None], ids=["empty", "one_letter", "none"])
    def test_a_text_shorter_than_two_characters_never_reaches_the_network(self, text, requests_mock):
        mock = requests_mock.get(geo.SUGGEST_URL, json=[PARIS_15E])

        assert geo._query_autocomplete(text) == []

        assert mock.call_count == 0

    @pytest.mark.parametrize("status", [400, 429, 500, 503])
    def test_an_http_error_is_raised_not_swallowed(self, status, requests_mock):
        requests_mock.get(geo.SUGGEST_URL, status_code=status, json={})

        with pytest.raises(Exception, match=str(status)):
            geo._query_autocomplete("75015")

    def test_a_non_list_response_is_treated_as_no_result(self, requests_mock):
        """Une réponse inattendue (dict d'erreur) vaut liste vide : l'échec sera
        mémorisé comme tel par l'appelant plutôt que de lever en pleine
        résolution."""
        requests_mock.get(geo.SUGGEST_URL, json={"error": "oops"})

        assert geo._query_autocomplete("75015") == []


# ---------------------------------------------------------------------------
# _normalize_name / _names_match
# ---------------------------------------------------------------------------

class TestNormalizeName:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Paris", "PARIS"),
            # Le « (75015) » accolé par les entrées de code postal est retiré.
            ("PARIS (75015)", "PARIS"),
            ("SAINT-ÉTIENNE", "SAINT ETIENNE"),
            ("Saint-Étienne", "SAINT ETIENNE"),
            ("ST ETIENNE", "ST ETIENNE"),
            ("Villeurbanne", "VILLEURBANNE"),
            # Ponctuation et espaces resserrés.
            ("L'Haÿ-les-Roses", "L HAY LES ROSES"),
        ],
    )
    def test_names_are_upper_ascii_and_compact(self, text, expected):
        assert geo._normalize_name(text) == expected


class TestNamesMatch:
    @pytest.mark.parametrize(
        ("entry", "city", "expected"),
        [
            ("PARIS (75015)", "Paris", True),
            ("PARIS", "Paris", True),
            # Même code postal (69003), ville différente : ne doit PAS matcher.
            ("LYON (69003)", "Villeurbanne", False),
            ("LYON", "Villeurbanne", False),
            # Noms abrégés par le site : irrapprochables, d'où le repli.
            ("ST ETIENNE", "Saint-Étienne", False),
        ],
    )
    def test_the_name_carries_the_disambiguation(self, entry, city, expected):
        assert geo._names_match(entry, city) is expected

    @pytest.mark.parametrize(
        ("entry", "city"),
        [("", "Paris"), (None, "Paris"), ("PARIS", ""), ("PARIS", None)],
        ids=["entry_vide", "entry_none", "city_vide", "city_none"],
    )
    def test_a_missing_name_never_matches(self, entry, city):
        assert geo._names_match(entry, city) is False


# ---------------------------------------------------------------------------
# _pick_city_slug
# ---------------------------------------------------------------------------

class TestPickCitySlug:
    def test_an_exact_arrondissement_wins_when_its_name_matches(self):
        """Paris/Lyon/Marseille arrondissement par arrondissement : seul le slug
        `cp-75015` vise le 15e précisément. `v-paris` est écarté."""
        results = [PARIS_CITY, PARIS_15E]

        assert geo._pick_city_slug(results, "75015", "Paris") == "cp-75015"

    def test_a_city_of_the_same_postal_code_is_picked_by_name(self):
        """69003 = Lyon 3e ET Villeurbanne : la recherche Villeurbanne doit
        rendre `v-villeurbanne`, jamais `v-lyon` pourtant de même code postal."""
        results = [LYON_CITY, VILLEURBANNE]

        assert geo._pick_city_slug(results, "69003", "Villeurbanne") == "v-villeurbanne"

    @pytest.mark.parametrize(
        "results",
        [[VILLEURBANNE, LYON_CITY], [LYON_CITY, VILLEURBANNE]],
        ids=["villeurbanne_first", "lyon_first"],
    )
    def test_the_name_match_wins_whatever_the_api_order(self, results):
        assert geo._pick_city_slug(results, "69003", "Villeurbanne") == "v-villeurbanne"

    def test_fallback_to_the_first_city_of_the_postal_code(self):
        """« SAINT-ÉTIENNE » est abrégé en « ST ETIENNE » par le site : le nom ne
        se rapproche pas, mais la première `v-*` du code postal reste le bon
        périmètre — le contrôle `matches_locations` en aval garde le résultat
        honnête."""
        assert geo._pick_city_slug([SAINT_ETIENNE], "42000", "Saint-Étienne") == "v-st+etienne"

    def test_no_candidate_returns_none(self):
        assert geo._pick_city_slug([PARIS_15E], "69003", "Villeurbanne") is None

    def test_an_entry_of_a_different_postal_code_is_never_a_candidate(self):
        assert geo._pick_city_slug([PARIS_15E], "75001", "Paris") is None


# ---------------------------------------------------------------------------
# _pick_whole_city_slug
# ---------------------------------------------------------------------------

class TestPickWholeCitySlug:
    def test_the_first_v_slug_is_the_whole_city(self):
        """« Paris » ville entière -> `v-paris`, distinct des arrondissements
        `cp-*` qui suivent dans la réponse."""
        results = [PARIS_15E, PARIS_1ER, PARIS_CITY]

        assert geo._pick_whole_city_slug(results) == "v-paris"

    def test_the_cp_entries_are_skipped(self):
        assert geo._pick_whole_city_slug([PARIS_15E, PARIS_1ER]) is None

    def test_an_empty_result_returns_none(self):
        assert geo._pick_whole_city_slug([]) is None


# ---------------------------------------------------------------------------
# _pick_department_slug / _department_query_code — le slug complet d'un département
# ---------------------------------------------------------------------------

GIRONDE_ENTRY = {"id": "d-33", "name": "33 - Gironde"}
HAUTS_DE_SEINE_ENTRY = {"id": "d-92", "name": "92 - Hauts-de-Seine"}
CORSE_DU_SUD_ENTRY = {"id": "d-201", "name": "201 - Corse-du-Sud"}


class TestDepartmentQueryCode:
    def test_the_corse_is_queried_by_its_postal_code(self):
        """2A/2B n'ont AUCUNE entrée autocomplete (q=2A -> [], vérifié en
        direct) : le site les référence sous leurs codes postaux 201/202."""
        assert geo._department_query_code("2A") == "201"
        assert geo._department_query_code("2B") == "202"

    @pytest.mark.parametrize("code", ["33", "75", "975"], ids=["gironde", "paris", "outre_mer"])
    def test_any_other_department_is_queried_as_is(self, code):
        assert geo._department_query_code(code) == code


class TestSlugSuffixFromLabel:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("33 - Gironde", "gironde"),
            # Multi-mots : les tirets du libellé deviennent underscores, comme
            # dans les slugs réels du site (d-92_hauts_de_seine, 200 vérifié).
            ("92 - Hauts-de-Seine", "hauts_de_seine"),
            # Accents et apostrophe : même normalisation que le site
            # (d-69_rhone et d-21_cote_d_or, 200 vérifiés en direct).
            ("69 - Rhône", "rhone"),
            ("21 - Côte-d'Or", "cote_d_or"),
        ],
    )
    def test_the_suffix_follows_the_site_s_own_normalisation(self, label, expected):
        assert geo._slug_suffix_from_label(label, label.split(" - ")[0]) == expected

    @pytest.mark.parametrize(
        ("label", "case"),
        [("33-Gironde", "sans espace-tiret-espace"), ("Gironde", "sans code"), ("", "vide")],
        ids=["mal_forme", "sans_code", "vide"],
    )
    def test_an_unexpected_label_has_no_suffix(self, label, case):
        assert geo._slug_suffix_from_label(label, "33") is None, case


class TestPickDepartmentSlug:
    def test_the_incomplete_id_becomes_a_complete_slug(self):
        """L'autocomplete renvoie `d-33` SANS nom ; la page de résultats exige
        le slug COMPLET (`/annonces/f/achat/d-33/` -> 410 contre 200 pour
        `/d-33_gironde/`, vérifié en direct) : le suffixe se dérive du libellé."""
        assert geo._pick_department_slug([GIRONDE_ENTRY], "33") == "d-33_gironde"

    def test_an_entry_of_another_department_is_ignored(self):
        assert geo._pick_department_slug([GIRONDE_ENTRY], "92") is None

    def test_no_d_entry_at_all_returns_none(self):
        """Le cas Paris : pas d'entrée `d-75`, seulement des villes — c'est
        l'appelant qui décide du repli (voir TestResolveUncached)."""
        assert geo._pick_department_slug([PARIS_CITY], "75") is None

    def test_an_unreadable_label_gives_up(self):
        assert geo._pick_department_slug([{"id": "d-33", "name": "?"}], "33") is None


# ---------------------------------------------------------------------------
# area_cache_key — même convention que SeLoger / bienici
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
        """Une commune tapée à la main (sans code INSEE) a quand même une clé,
        dérivée du code postal — même contrat que bienici."""
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
# _resolve_uncached — routage par niveau
# ---------------------------------------------------------------------------

class TestResolveUncached:
    def test_a_city_queries_by_postal_code_and_picks_by_name(self, autocomplete):
        asked = autocomplete([PARIS_15E, PARIS_CITY])

        slug = geo._resolve_uncached(make_city_location("Paris", "75015", "75115"))

        assert slug == "cp-75015"
        assert asked == ["75015"]

    def test_a_whole_city_queries_by_name(self, autocomplete):
        asked = autocomplete([PARIS_15E, PARIS_CITY])

        slug = geo._resolve_uncached(make_whole_city_location("Paris", ("75015",), "75056"))

        assert slug == "v-paris"
        assert asked == ["Paris"]

    def test_a_department_queries_by_code_and_derives_the_full_slug(self, autocomplete):
        asked = autocomplete([GIRONDE_ENTRY])

        slug = geo._resolve_uncached(make_department_location(code="33"))

        assert slug == "d-33_gironde"
        assert asked == ["33"]

    def test_the_corse_is_resolved_through_its_postal_code(self, autocomplete):
        asked = autocomplete([CORSE_DU_SUD_ENTRY])

        slug = geo._resolve_uncached(make_department_location(code="2A", name="Corse-du-Sud"))

        assert slug == "d-201_corse_du_sud"
        assert asked == ["201"]

    def test_paris_falls_back_to_the_whole_city(self, autocomplete):
        """Paris n'a pas d'entrée `d-75` : l'autocomplete ne propose que la
        ville entière et ses arrondissements (vérifié en direct). Le repli sur
        la première ville est exact — une seule commune couvre tout le 75."""
        autocomplete([PARIS_CITY, PARIS_15E])

        assert geo._resolve_uncached(make_department_location(code="75", name="Paris")) == "v-paris"

    def test_a_region_has_no_slug_of_its_own(self, autocomplete):
        """Aucune entrée région dans l'autocomplete (`q=ile de france` -> [],
        vérifié en direct) : None SANS requête. C'est le parser qui élargit la
        région à ses départements avant de les résoudre un par un."""
        asked = autocomplete([PARIS_CITY])

        assert geo._resolve_uncached(make_region_location()) is None

        assert asked == []

    def test_an_unknown_kind_resolves_to_none(self, autocomplete):
        autocomplete([PARIS_CITY])

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
    def test_it_never_raises_whatever_goes_wrong(self, autocomplete, log_messages, error, case):
        autocomplete([], side_effect=error)

        assert geo._resolve_uncached(make_city_location()) is None, case
        assert geo._resolve_uncached(make_department_location()) is None, case
        assert any("Résolution échouée" in m for m in log_messages)

    def test_a_city_without_postal_code_is_swallowed(self, autocomplete):
        autocomplete([])

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
        naive = datetime.datetime(2026, 8, 15, 10, 0, 0)

        assert geo._seconds_since(naive) == 2 * 3600

    @pytest.mark.parametrize(
        "tzinfo",
        [datetime.UTC, datetime.timezone(datetime.timedelta(hours=2)), datetime.timezone(datetime.timedelta(hours=-5))],
        ids=["utc", "plus_two", "minus_five"],
    )
    @freeze_time(FROZEN)
    def test_an_aware_datetime_is_compared_in_its_own_timezone(self, tzinfo):
        instant = datetime.datetime(2026, 8, 15, 10, 0, 0, tzinfo=datetime.UTC)

        assert geo._seconds_since(instant.astimezone(tzinfo)) == 2 * 3600


# ---------------------------------------------------------------------------
# resolve_slug_id — la table de vérité du cache (même forme que SeLoger/bienici)
# ---------------------------------------------------------------------------

class TestResolveSlugIdCacheTruthTable:
    @staticmethod
    def spy_resolution(monkeypatch, result="v-paris"):
        calls: list[dict] = []
        monkeypatch.setattr(geo, "_resolve_uncached", lambda location: calls.append(location) or result)
        return calls

    @freeze_time(FROZEN)
    def test_an_unidentifiable_area_warns_and_never_touches_the_cache(self, repo, monkeypatch, log_messages):
        calls = self.spy_resolution(monkeypatch)
        location = {"kind": CITY}

        assert geo.resolve_slug_id(location, repo=repo) is None

        assert calls == []
        repo.get_cached.assert_not_called()
        repo.set_cached.assert_not_called()
        assert any("Périmètre non identifiable" in m for m in log_messages)

    @freeze_time(FROZEN)
    def test_a_cached_slug_is_returned_immediately(self, repo, monkeypatch):
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = {"slug_id": "cp-75015", "resolved_at": None}

        assert geo.resolve_slug_id(make_city_location(insee="75115"), repo=repo) == "cp-75015"

        repo.get_cached.assert_called_once_with("75115")
        assert calls == []
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        "age_seconds", [0, 3600, 7 * 24 * 3600 - 1], ids=["just_now", "one_hour", "one_second_before_expiry"],
    )
    @freeze_time(FROZEN)
    def test_a_recent_failure_is_not_retried(self, repo, monkeypatch, age_seconds):
        """🔒 L'échec mémorisé (slug_id NULL) coupe toute nouvelle tentative
        avant 7 jours : sans ce délai, un périmètre introuvable martèlerait
        l'autocomplete à chaque cycle de scrape (toutes les 30 s)."""
        calls = self.spy_resolution(monkeypatch)
        resolved_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=age_seconds)
        repo.get_cached.return_value = {"slug_id": None, "resolved_at": resolved_at}

        assert geo.resolve_slug_id(make_city_location(), repo=repo) is None

        assert calls == []
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        "age_seconds", [7 * 24 * 3600, 30 * 24 * 3600], ids=["exactly_seven_days", "one_month"],
    )
    @freeze_time(FROZEN)
    def test_an_expired_failure_is_retried_and_rewritten(self, repo, monkeypatch, age_seconds):
        calls = self.spy_resolution(monkeypatch)
        resolved_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=age_seconds)
        repo.get_cached.return_value = {"slug_id": None, "resolved_at": resolved_at}

        assert geo.resolve_slug_id(make_city_location(insee="75115"), repo=repo) == "v-paris"

        assert len(calls) == 1
        repo.set_cached.assert_called_once_with("75115", "v-paris")

    @freeze_time(FROZEN)
    def test_a_failure_without_a_resolution_date_is_retried(self, repo, monkeypatch):
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = {"slug_id": None, "resolved_at": None}

        assert geo.resolve_slug_id(make_city_location(), repo=repo) == "v-paris"
        assert len(calls) == 1

    @freeze_time(FROZEN)
    def test_a_first_lookup_resolves_and_caches(self, repo, monkeypatch, log_messages):
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = None
        location = make_city_location(city="Paris", postal_code="75015", insee="75115")

        assert geo.resolve_slug_id(location, repo=repo) == "v-paris"

        assert calls == [location]
        repo.set_cached.assert_called_once_with("75115", "v-paris")
        assert any("Paris (75015) -> v-paris" in m for m in log_messages)

    @freeze_time(FROZEN)
    def test_a_failed_resolution_is_cached_as_none_with_a_warning(self, repo, monkeypatch, log_messages):
        self.spy_resolution(monkeypatch, result=None)
        repo.get_cached.return_value = None

        assert geo.resolve_slug_id(make_city_location(insee="75115"), repo=repo) is None

        repo.set_cached.assert_called_once_with("75115", None)
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
    def test_the_cache_is_read_and_written_with_the_area_key(
        self, repo, monkeypatch, location, expected_key
    ):
        self.spy_resolution(monkeypatch)

        geo.resolve_slug_id(location, repo=repo)

        repo.get_cached.assert_called_once_with(expected_key)
        repo.set_cached.assert_called_once_with(expected_key, "v-paris")

    @freeze_time(FROZEN)
    def test_a_department_is_banked_like_any_area(self, repo, monkeypatch):
        """Le niveau département est couvert : sa clé propre (`dept:33`) porte
        le slug complet résolu — un département et une ville de même code ne
        partagent jamais de ligne."""
        calls: list[dict] = []
        monkeypatch.setattr(
            geo, "_resolve_uncached", lambda location: calls.append(location) or "d-33_gironde"
        )
        repo.get_cached.return_value = None

        assert geo.resolve_slug_id(make_department_location(code="33"), repo=repo) == "d-33_gironde"

        assert len(calls) == 1
        repo.get_cached.assert_called_once_with("dept:33")
        repo.set_cached.assert_called_once_with("dept:33", "d-33_gironde")

    @freeze_time(FROZEN)
    def test_a_region_still_resolves_to_none_and_banks_the_failure(self, repo, monkeypatch):
        """`area_cache_key` rend bien une clé pour une région, mais le niveau
        n'a pas d'identifiant Century 21 : la résolution renvoie None et
        mémorise l'échec (une ligne à NULL), plutôt que de laisser croire à un
        périmètre supporté. L'absence de requête réseau est couverte par
        TestResolveUncached.test_a_region_has_no_slug_of_its_own."""
        calls: list[dict] = []
        monkeypatch.setattr(geo, "_resolve_uncached", lambda location: calls.append(location) or None)
        repo.get_cached.return_value = None

        assert geo.resolve_slug_id(make_region_location(code="75"), repo=repo) is None

        assert len(calls) == 1
        repo.set_cached.assert_called_once_with("region:75", None)


# ---------------------------------------------------------------------------
# remember_manual_slugs
# ---------------------------------------------------------------------------

class TestRememberManualSlugs:
    @staticmethod
    def criteria_with(slugs, locations):
        from tests.helpers.factories import make_criteria

        return make_criteria(locations=locations, sourceOverrides={"century21": {"slugs": slugs}})

    def test_slugs_and_one_location_are_banked_together(self, repo):
        criteria = self.criteria_with(["v-montrouge"], [make_city_location(insee="92049")])

        geo.remember_manual_slugs(criteria, repo=repo)

        repo.set_cached.assert_called_once_with("92049", "v-montrouge")

    def test_an_existing_cache_entry_is_never_overwritten(self, repo):
        repo.get_cached.return_value = {"slug_id": "v-montrouge", "resolved_at": None}
        criteria = self.criteria_with(["v-autres"], [make_city_location(insee="92049")])

        geo.remember_manual_slugs(criteria, repo=repo)

        repo.get_cached.assert_called_once_with("92049")
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        ("slugs", "locations", "case"),
        [
            (["v-paris"], [], "aucune localisation"),
            (
                ["v-paris"],
                [
                    make_city_location(insee="75115"),
                    make_city_location(city="Lyon", postal_code="69007", insee="69387"),
                ],
                "deux localisations",
            ),
            ([], [make_city_location(insee="75115")], "aucun slug"),
        ],
        ids=["no_location", "two_locations", "no_slug"],
    )
    def test_nothing_to_remember_is_a_silent_no_op(self, repo, slugs, locations, case):
        criteria = self.criteria_with(slugs, locations)

        geo.remember_manual_slugs(criteria, repo=repo)

        repo.set_cached.assert_not_called(), case

    def test_a_location_without_insee_is_still_banked_under_its_postal_key(self, repo):
        repo.get_cached.return_value = None
        criteria = self.criteria_with(["v-montrouge"], [{"kind": CITY, "city": "Montrouge", "postalCode": "92120"}])

        geo.remember_manual_slugs(criteria, repo=repo)

        repo.set_cached.assert_called_once_with("postal:92120", "v-montrouge")

    def test_a_location_with_no_key_at_all_has_nothing_to_key_on(self, repo):
        criteria = self.criteria_with(["v-paris"], [{"kind": CITY}])

        geo.remember_manual_slugs(criteria, repo=repo)

        repo.get_cached.assert_not_called()
        repo.set_cached.assert_not_called()
