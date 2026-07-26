"""Tests unitaires de `services/seloger_geocode.py`.

Ce module traduit un périmètre canonique (voir core/criteria.py) en `placeId`
SeLoger, en interrogeant l'autocomplete du site et en mémorisant le résultat.
Deux zones sensibles :

  * **la clé de cache** : elle doit être injective. Le département 75 et la
    région 75 (Nouvelle-Aquitaine) coexistent ; une collision ferait scraper
    la Gironde à qui demande Paris ;
  * **la table de vérité du cache** de `resolve_place_id` : quatre lignes
    (placeId connu, échec récent, échec périmé, absence), chacune avec un
    nombre d'appels réseau différent. Un test par ligne.

Aucun appel réseau : le socle bloque le transport HTTP (tests/conftest.py) et
`requests_mock` sert d'adaptateur pour les tests qui exercent la requête.

Remplace tests/_legacy/test_seloger_geocode.py (30 tests, mais aucun sur
`_seconds_since`, la frontière exacte du cooldown, ni le payload complet).
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time
from loguru import logger

from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY
from repositories.seloger_geo_repo import SelogerGeoRepository
from services import seloger_geocode as geo
from tests.helpers.factories import (
    make_city_location,
    make_criteria,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)

FROZEN = "2026-07-26 12:00:00"

# Réponses de l'autocomplete, telles qu'observées en live.
PARIS_WHOLE_CITY = {
    "id": "AD08FR31096", "type_key": "AD08", "labels": ["Paris (75)"],
    "postal_codes": [f"750{i:02d}" for i in range(1, 21)],
}
PARIS_15E = {
    "id": "AD09FR40", "type_key": "AD09", "labels": ["Paris 15ème arrondissement (75015)"],
    "postal_codes": ["75015"],
}
NANTES = {
    "id": "AD08FR17221", "type_key": "AD08", "labels": ["Nantes (44)"],
    "postal_codes": ["44000", "44100", "44200", "44300"],
}
IDF = {"id": "AD04FR5", "type_key": "AD04", "labels": ["Ile-de-France"], "postal_codes": []}
GIRONDE_DEPT = {"id": "AD06FR34", "type_key": "AD06", "labels": ["Gironde (33)"], "postal_codes": []}
GIRONDE_COMMUNE = {
    "id": "AD08FR13223", "type_key": "AD08",
    "labels": ["Gironde-sur-Dropt (33190)"], "postal_codes": ["33190"],
}


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
    double = MagicMock(spec=SelogerGeoRepository)
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
    def test_the_payload_matches_what_the_site_s_own_search_bar_sends(self, requests_mock):
        """Le contrat exact avec l'endpoint : `placeTypes` ET `parentTypes`
        doivent contenir les mêmes types, et `locale` être "fr" — c'est la
        forme capturée sur le front SeLoger, et le BFF de recherche a déjà
        été perdu une fois pour une divergence de schéma."""
        mock = requests_mock.post(geo.AUTOCOMPLETE_URL, json=[PARIS_15E])

        assert geo._query_autocomplete("75015") == [PARIS_15E]

        request = mock.last_request
        assert request.json() == {
            "text": "75015",
            "limit": 10,
            "placeTypes": geo.PLACE_TYPES,
            "parentTypes": geo.PLACE_TYPES,
            "locale": "fr",
        }
        assert request.headers["Referer"] == "https://www.seloger.com/"
        assert request.headers["Accept-Language"] == "fr-FR,fr;q=0.9"
        assert "Chrome" in request.headers["User-Agent"]

    @pytest.mark.parametrize("text", ["", "p", "7"], ids=["empty", "one_letter", "one_digit"])
    def test_a_text_shorter_than_two_characters_never_reaches_the_network(self, text, requests_mock):
        mock = requests_mock.post(geo.AUTOCOMPLETE_URL, json=[PARIS_15E])

        assert geo._query_autocomplete(text) == []

        assert mock.call_count == 0, "l'endpoint refuse ces requêtes, autant les éviter"

    @pytest.mark.parametrize("status", [400, 429, 500, 503], ids=["bad_request", "throttled", "server", "unavailable"])
    def test_an_http_error_is_raised_not_swallowed(self, status, requests_mock):
        """`raise_for_status` : l'erreur remonte jusqu'à `_resolve_uncached`,
        qui est le seul endroit où elle est rattrapée. C'est ce qui distingue
        « pas de résultat » de « site en panne » dans les logs."""
        requests_mock.post(geo.AUTOCOMPLETE_URL, status_code=status, json={})

        with pytest.raises(Exception, match=str(status)):
            geo._query_autocomplete("75015")

    def test_an_empty_result_list_is_returned_as_is(self, requests_mock):
        requests_mock.post(geo.AUTOCOMPLETE_URL, json=[])

        assert geo._query_autocomplete("00000") == []


# ---------------------------------------------------------------------------
# _pick_best_match — trois niveaux de préférence
# ---------------------------------------------------------------------------

class TestPickBestMatch:
    @pytest.mark.parametrize(
        "results",
        [
            [PARIS_WHOLE_CITY, PARIS_15E],
            [PARIS_15E, PARIS_WHOLE_CITY],
        ],
        ids=["broad_first", "exact_first"],
    )
    def test_an_exact_postal_code_match_wins_whatever_the_api_order(self, results):
        """L'entrée « Paris entier » contient AUSSI 75015 dans ses
        postal_codes : sans la préférence pour l'égalité stricte, une recherche
        sur le 15e ratisserait les 20 arrondissements."""
        assert geo._pick_best_match(results, "75015")["id"] == "AD09FR40"

    def test_a_broader_entry_containing_the_code_is_the_second_choice(self):
        """Nantes n'a pas d'entrée par arrondissement : l'entrée ville, qui
        contient le code postal, est la bonne réponse."""
        assert geo._pick_best_match([NANTES], "44100")["id"] == "AD08FR17221"

    def test_the_first_result_is_the_last_resort(self):
        """Repli défensif : ne jamais rendre None alors que l'API a répondu."""
        assert geo._pick_best_match([NANTES], "99999")["id"] == "AD08FR17221"

    @pytest.mark.parametrize(
        "results",
        [[], [{"id": "X"}], [{"id": "X", "postal_codes": None}]],
        ids=["no_results", "no_postal_codes_key", "null_postal_codes"],
    )
    def test_a_missing_postal_codes_key_does_not_crash(self, results):
        """`r.get("postal_codes") or []` : une réponse tronquée passe par le
        repli, elle ne lève pas."""
        match = geo._pick_best_match(results, "75015")

        assert match == (results[0] if results else None)


# ---------------------------------------------------------------------------
# _find_city_place_id / _find_wide_area_place_id
# ---------------------------------------------------------------------------

class TestFindCityPlaceId:
    def test_the_query_uses_the_postal_code_and_nothing_else(self, autocomplete):
        """Pas de repli par nom de ville : l'autocomplete plafonne à 10
        résultats, et pour une ville à 20 arrondissements la bonne entrée peut
        tomber hors de la fenêtre — `_pick_best_match` retomberait alors sur un
        périmètre bien plus large."""
        asked = autocomplete([PARIS_WHOLE_CITY, PARIS_15E])

        assert geo._find_city_place_id("75015") == "AD09FR40"
        assert asked == ["75015"], "une seule requête, par code postal"

    def test_no_result_means_no_place_id_and_no_fallback_query(self, autocomplete):
        asked = autocomplete([])

        assert geo._find_city_place_id("44000") is None
        assert asked == ["44000"]


class TestFindWideAreaPlaceId:
    @pytest.mark.parametrize(
        ("name", "kind", "results", "expected"),
        [
            ("Île-de-France", REGION, [IDF], "AD04FR5"),
            ("Gironde", DEPARTMENT, [GIRONDE_DEPT], "AD06FR34"),
            ("Paris", WHOLE_CITY, [PARIS_WHOLE_CITY], "AD08FR31096"),
        ],
        ids=["region", "department", "whole_city"],
    )
    def test_each_level_is_found_by_name(self, autocomplete, name, kind, results, expected):
        asked = autocomplete(results)

        assert geo._find_wide_area_place_id(name, kind) == expected
        assert asked == [name], "les périmètres larges se cherchent par NOM"

    def test_the_expected_type_discriminates_a_homonym(self, autocomplete):
        """« Gironde » désigne aussi une commune (Gironde-sur-Dropt). Sans le
        filtre sur `type_key`, une recherche départementale scraperait un
        village de 1 000 habitants — et l'entrée commune est renvoyée EN
        PREMIER par l'API."""
        autocomplete([GIRONDE_COMMUNE, GIRONDE_DEPT])

        assert geo._find_wide_area_place_id("Gironde", DEPARTMENT) == "AD06FR34"

    def test_the_first_entry_of_the_expected_type_wins(self, autocomplete):
        autocomplete([GIRONDE_COMMUNE, GIRONDE_DEPT, {"id": "AD06FR99", "type_key": "AD06"}])

        assert geo._find_wide_area_place_id("Gironde", DEPARTMENT) == "AD06FR34"

    def test_no_entry_of_the_expected_type_returns_none(self, autocomplete):
        """Plutôt rendre None qu'un périmètre du mauvais niveau : un placeId
        erroné produirait des annonces silencieusement hors périmètre."""
        autocomplete([GIRONDE_COMMUNE])

        assert geo._find_wide_area_place_id("Gironde", DEPARTMENT) is None

    @pytest.mark.parametrize(
        ("name", "kind", "case"),
        [
            ("", REGION, "nom vide"),
            (None, DEPARTMENT, "nom absent"),
            ("Gironde", CITY, "niveau commune : pas de type large associé"),
            ("Gironde", "nawak", "niveau inconnu"),
        ],
        ids=["empty_name", "none_name", "city_kind", "unknown_kind"],
    )
    def test_nothing_to_search_means_no_request_at_all(self, autocomplete, name, kind, case):
        asked = autocomplete([GIRONDE_DEPT])

        assert geo._find_wide_area_place_id(name, kind) is None, case
        assert asked == [], f"aucune requête ne doit partir ({case})"


# ---------------------------------------------------------------------------
# area_cache_key
# ---------------------------------------------------------------------------

class TestAreaCacheKey:
    def test_the_commune_level_keeps_the_bare_insee_code(self):
        """Convention d'avant les périmètres larges. La préserver évite
        d'invalider tout le cache déjà constitué (une résolution = une requête
        chez SeLoger)."""
        assert geo.area_cache_key(make_city_location(insee="75115")) == "75115"

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
        """Le département 75 (Paris) et la région 75 (Nouvelle-Aquitaine)
        existent tous les deux : c'est la raison d'être du préfixe."""
        keys = {
            geo.area_cache_key(make_department_location(code="75")),
            geo.area_cache_key(make_region_location(code="75")),
            geo.area_cache_key(make_city_location(insee="75")),
        }

        assert len(keys) == 3, f"collision de clés : {keys}"

    def test_the_kind_defaults_to_city(self):
        """Une localisation sans `kind` vient de l'ancien format à plat : elle
        doit être traitée comme une commune."""
        assert geo.area_cache_key({"inseeCode": "75115", "city": "Paris"}) == "75115"

    @pytest.mark.parametrize(
        ("location", "case"),
        [
            ({"kind": CITY, "city": "X", "postalCode": "99999"}, "commune sans code INSEE"),
            ({"kind": CITY, "inseeCode": ""}, "code INSEE vide"),
            ({"kind": CITY, "inseeCode": None}, "code INSEE nul"),
            ({"kind": WHOLE_CITY, "city": "Poitiers"}, "ville entière sans code INSEE"),
            ({"kind": REGION, "name": "Nulle part"}, "région sans code"),
            ({"kind": DEPARTMENT, "name": "Gironde", "code": ""}, "département à code vide"),
        ],
        ids=["city_no_insee", "empty_insee", "null_insee", "whole_city_no_insee", "region_no_code", "dept_empty_code"],
    )
    def test_an_unidentifiable_area_has_no_key(self, location, case):
        """Une localisation saisie à la main (sans autocomplete) n'a pas de code
        INSEE : il n'y a rien pour indexer le cache, et c'est ce `None` qui
        arrête `resolve_place_id` avant tout appel réseau."""
        assert geo.area_cache_key(location) is None, case


# ---------------------------------------------------------------------------
# _describe (messages de log)
# ---------------------------------------------------------------------------

class TestDescribe:
    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            (make_city_location(city="Paris", postal_code="75015"), "Paris (75015)"),
            (make_whole_city_location(city="Poitiers"), "Poitiers (toute la ville)"),
            (make_department_location(name="Gironde"), "département Gironde"),
            (make_region_location(name="Nouvelle-Aquitaine"), "région Nouvelle-Aquitaine"),
            ({"kind": REGION, "code": "75"}, "région 75"),
            ({"kind": DEPARTMENT, "code": "33"}, "département 33"),
        ],
        ids=["city", "whole_city", "department", "region", "region_by_code", "dept_by_code"],
    )
    def test_every_level_is_described_for_the_logs(self, location, expected):
        """Le repli sur le code quand le nom manque évite un « région None »
        dans les logs — seule trace disponible pour diagnostiquer une
        résolution ratée."""
        assert geo._describe(location) == expected


# ---------------------------------------------------------------------------
# _resolve_uncached
# ---------------------------------------------------------------------------

class TestResolveUncached:
    @pytest.mark.parametrize(
        ("location", "expected_call"),
        [
            (make_city_location(postal_code="75015"), ("city", "75015")),
            (make_whole_city_location(city="Poitiers"), ("wide", ("Poitiers", WHOLE_CITY))),
            (make_department_location(name="Gironde"), ("wide", ("Gironde", DEPARTMENT))),
            (make_region_location(name="Nouvelle-Aquitaine"), ("wide", ("Nouvelle-Aquitaine", REGION))),
        ],
        ids=["city", "whole_city", "department", "region"],
    )
    def test_each_level_is_routed_to_the_right_lookup(self, monkeypatch, location, expected_call):
        """Détail qui compte : la ville entière est cherchée par `city`, les
        région/département par `name`. Confondre les deux clés rendrait un
        `None` silencieux."""
        calls: list[tuple] = []
        monkeypatch.setattr(
            geo, "_find_city_place_id", lambda pc: calls.append(("city", pc)) or "POCO1"
        )
        monkeypatch.setattr(
            geo, "_find_wide_area_place_id", lambda name, kind: calls.append(("wide", (name, kind))) or "AD06X"
        )

        result = geo._resolve_uncached(location)

        assert calls == [expected_call]
        assert result == ("POCO1" if expected_call[0] == "city" else "AD06X")

    @pytest.mark.parametrize(
        ("error", "case"),
        [
            (ConnectionError("réseau coupé"), "panne réseau"),
            (ValueError("réponse illisible"), "JSON inattendu"),
            (KeyError("id"), "champ manquant dans la réponse"),
            (TimeoutError("délai dépassé"), "timeout"),
        ],
        ids=["network", "bad_json", "missing_field", "timeout"],
    )
    def test_it_never_raises_whatever_goes_wrong(self, autocomplete, log_messages, error, case):
        """Contrat central : `_resolve_uncached` est appelée pendant un scrape,
        sur un thread de fond. Une exception qui remonterait ferait échouer le
        scrape entier alors qu'une source seulement est indisponible."""
        autocomplete([], side_effect=error)

        assert geo._resolve_uncached(make_city_location()) is None, case
        assert geo._resolve_uncached(make_department_location()) is None, case
        assert any("Résolution échouée" in m for m in log_messages)

    def test_a_city_without_postal_code_is_also_swallowed(self, autocomplete):
        """`location["postalCode"]` sur une localisation incomplète lève un
        `KeyError`, rattrapé par le même `except Exception` : le contrat « ne
        lève jamais » couvre aussi les critères malformés."""
        autocomplete([])

        assert geo._resolve_uncached({"kind": CITY, "city": "Paris"}) is None

    def test_an_unknown_kind_falls_back_to_the_wide_area_lookup(self, monkeypatch):
        """Toute valeur de `kind` non gérée explicitement passe par la branche
        « périmètre large », qui rend None faute de type attendu."""
        monkeypatch.setattr(geo, "_query_autocomplete", lambda text: [GIRONDE_DEPT])

        assert geo._resolve_uncached({"kind": "canton", "name": "Gironde"}) is None


# ---------------------------------------------------------------------------
# _seconds_since
# ---------------------------------------------------------------------------

class TestSecondsSince:
    @freeze_time(FROZEN)
    def test_none_has_no_age(self):
        """Une ligne de cache sans `resolved_at` (colonne nullable) doit rendre
        None, ce que `resolve_place_id` interprète comme « âge inconnu » et non
        comme « âge nul »."""
        assert geo._seconds_since(None) is None

    @freeze_time(FROZEN)
    def test_a_naive_datetime_is_compared_against_utcnow(self):
        naive = datetime.datetime(2026, 7, 26, 10, 0, 0)

        assert geo._seconds_since(naive) == 2 * 3600

    @pytest.mark.parametrize(
        ("tzinfo", "case"),
        [
            (datetime.UTC, "aware UTC"),
            (datetime.timezone(datetime.timedelta(hours=2)), "aware UTC+2"),
            (datetime.timezone(datetime.timedelta(hours=-5)), "aware UTC-5"),
        ],
        ids=["utc", "plus_two", "minus_five"],
    )
    @freeze_time(FROZEN)
    def test_an_aware_datetime_is_compared_in_its_own_timezone(self, tzinfo, case):
        """`datetime.now(resolved_at.tzinfo)` : l'instant est le même quel que
        soit le fuseau stocké, donc l'âge doit être identique. C'est ce qui
        permet à la colonne d'être TIMESTAMP ou TIMESTAMPTZ sans changer le
        comportement du cooldown."""
        instant = datetime.datetime(2026, 7, 26, 10, 0, 0, tzinfo=datetime.UTC)

        assert geo._seconds_since(instant.astimezone(tzinfo)) == 2 * 3600, case

    @freeze_time(FROZEN)
    def test_a_future_timestamp_gives_a_negative_age(self):
        """Horloge décalée entre l'app et la base : l'âge devient négatif, donc
        `< _RETRY_COOLDOWN_SECONDS`, donc l'échec est considéré récent. Pas de
        crash, et le pire cas est une résolution différée."""
        future = datetime.datetime(2026, 7, 27, 12, 0, 0)

        assert geo._seconds_since(future) == -86400


# ---------------------------------------------------------------------------
# resolve_place_id — la table de vérité du cache
# ---------------------------------------------------------------------------

class TestResolvePlaceIdCacheTruthTable:
    """Une ligne = un état du cache = un nombre d'appels réseau attendu.

    | état de la ligne en cache          | retour   | résolution ? | set_cached ? |
    |------------------------------------|----------|--------------|--------------|
    | clé None (périmètre non identifié) | None     | non          | non          |
    | place_id présent                   | place_id | non          | non          |
    | place_id None, âge < 7 j           | None     | non          | non          |
    | place_id None, âge >= 7 j          | résolu   | oui          | oui          |
    | pas de ligne                       | résolu   | oui          | oui          |
    """

    @staticmethod
    def spy_resolution(monkeypatch, result="AD08FR31096"):
        calls: list[dict] = []
        monkeypatch.setattr(
            geo, "_resolve_uncached", lambda location: calls.append(location) or result
        )
        return calls

    @freeze_time(FROZEN)
    def test_an_unidentifiable_area_warns_and_never_touches_the_cache(
        self, repo, monkeypatch, log_messages
    ):
        calls = self.spy_resolution(monkeypatch)
        location = {"kind": CITY, "city": "Saisie manuelle", "postalCode": "99999"}

        assert geo.resolve_place_id(location, repo=repo) is None

        assert calls == [], "aucune requête : il n'y a même pas de clé pour mémoriser le résultat"
        repo.get_cached.assert_not_called()
        repo.set_cached.assert_not_called()
        assert any("Périmètre non identifiable" in m for m in log_messages)

    @freeze_time(FROZEN)
    def test_a_cached_place_id_is_returned_immediately(self, repo, monkeypatch):
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = {"place_id": "AD09FR40", "resolved_at": None}

        assert geo.resolve_place_id(make_city_location(insee="75115"), repo=repo) == "AD09FR40"

        repo.get_cached.assert_called_once_with("75115")
        assert calls == []
        repo.set_cached.assert_not_called(), "un hit ne doit pas réécrire la ligne"

    @pytest.mark.parametrize(
        ("age_seconds", "case"),
        [
            (0, "à l'instant"),
            (3600, "il y a une heure"),
            (7 * 24 * 3600 - 1, "une seconde avant la fin du cooldown"),
        ],
        ids=["just_now", "one_hour", "one_second_before_expiry"],
    )
    @freeze_time(FROZEN)
    def test_a_recent_failure_is_not_retried(self, repo, monkeypatch, age_seconds, case):
        """Un échec est mémorisé comme `place_id=None` et respecté pendant 7
        jours : sans ce cooldown, chaque scrape re-interrogerait SeLoger pour
        un périmètre qu'il ne sait pas résoudre."""
        calls = self.spy_resolution(monkeypatch)
        resolved_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=age_seconds)
        repo.get_cached.return_value = {"place_id": None, "resolved_at": resolved_at}

        assert geo.resolve_place_id(make_city_location(), repo=repo) is None, case

        assert calls == [], case
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        ("age_seconds", "case"),
        [
            (7 * 24 * 3600, "exactement 7 jours : la borne est inclusive côté re-résolution"),
            (30 * 24 * 3600, "un mois"),
        ],
        ids=["exactly_seven_days", "one_month"],
    )
    @freeze_time(FROZEN)
    def test_an_expired_failure_is_retried_and_rewritten(self, repo, monkeypatch, age_seconds, case):
        calls = self.spy_resolution(monkeypatch)
        resolved_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=age_seconds)
        repo.get_cached.return_value = {"place_id": None, "resolved_at": resolved_at}

        assert geo.resolve_place_id(make_city_location(insee="75115"), repo=repo) == "AD08FR31096", case

        assert len(calls) == 1, case
        repo.set_cached.assert_called_once_with("75115", "AD08FR31096")

    @freeze_time(FROZEN)
    def test_a_failure_without_a_resolution_date_is_retried(self, repo, monkeypatch):
        """`resolved_at` nul -> âge inconnu -> on retente. C'est le choix sûr :
        une ligne mal remplie ne doit pas bloquer un périmètre pour toujours."""
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = {"place_id": None, "resolved_at": None}

        assert geo.resolve_place_id(make_city_location(), repo=repo) == "AD08FR31096"

        assert len(calls) == 1

    @freeze_time(FROZEN)
    def test_a_first_lookup_resolves_and_caches(self, repo, monkeypatch, log_messages):
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = None
        location = make_city_location(city="Paris", postal_code="75015", insee="75115")

        assert geo.resolve_place_id(location, repo=repo) == "AD08FR31096"

        assert calls == [location]
        repo.set_cached.assert_called_once_with("75115", "AD08FR31096")
        assert any("Paris (75015) -> AD08FR31096" in m for m in log_messages)

    @freeze_time(FROZEN)
    def test_a_failed_resolution_is_cached_as_none_with_a_warning(
        self, repo, monkeypatch, log_messages
    ):
        """L'échec DOIT être écrit : c'est lui qui arme le cooldown. Ne rien
        écrire ferait re-tenter la résolution à chaque scrape."""
        self.spy_resolution(monkeypatch, result=None)
        repo.get_cached.return_value = None

        assert geo.resolve_place_id(make_city_location(insee="75115"), repo=repo) is None

        repo.set_cached.assert_called_once_with("75115", None)
        assert any("Aucun placeId trouvé" in m for m in log_messages)

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

        geo.resolve_place_id(location, repo=repo)

        repo.get_cached.assert_called_once_with(expected_key)
        repo.set_cached.assert_called_once_with(expected_key, "AD08FR31096")


# ---------------------------------------------------------------------------
# remember_manual_place_id
# ---------------------------------------------------------------------------

class TestRememberManualPlaceId:
    @staticmethod
    def criteria_with(place_ids, locations):
        return make_criteria(
            locations=locations,
            sourceOverrides={"seloger": {"placeIds": place_ids}},
        )

    def test_one_place_id_and_one_location_are_banked_together(self, repo):
        """Le seul cas non ambigu : la liste de placeIds de SeLoger n'est pas
        appariée aux localisations, donc seule une saisie 1-1 permet d'attribuer
        l'identifiant à un périmètre."""
        criteria = self.criteria_with(["AD09FR40"], [make_city_location(insee="75115")])

        geo.remember_manual_place_id(criteria, repo=repo)

        repo.set_cached.assert_called_once_with("75115", "AD09FR40")

    @pytest.mark.parametrize(
        ("location", "expected_key"),
        [
            (make_whole_city_location(insee="86194"), "city:86194"),
            (make_department_location(code="33"), "dept:33"),
            (make_region_location(code="75"), "region:75"),
        ],
        ids=["whole_city", "department", "region"],
    )
    def test_a_manual_place_id_can_be_banked_for_a_wide_area_too(self, repo, location, expected_key):
        criteria = self.criteria_with(["AD06FR34"], [location])

        geo.remember_manual_place_id(criteria, repo=repo)

        repo.set_cached.assert_called_once_with(expected_key, "AD06FR34")

    def test_an_existing_cache_entry_is_never_overwritten(self, repo):
        """Une résolution automatique (ou une saisie antérieure) fait autorité :
        un placeId manuel ne doit pas pouvoir la remplacer, sinon une faute de
        frappe empoisonnerait le cache pour tous les utilisateurs."""
        repo.get_cached.return_value = {"place_id": "AD09FR40", "resolved_at": None}
        criteria = self.criteria_with(["AD08FR99999"], [make_city_location(insee="75115")])

        geo.remember_manual_place_id(criteria, repo=repo)

        repo.get_cached.assert_called_once_with("75115")
        repo.set_cached.assert_not_called()

    def test_a_cached_failure_is_also_left_alone(self, repo):
        """`get_cached() is None` est le seul feu vert : une ligne d'échec
        (place_id=None) compte comme « déjà en cache » et bloque la
        mémorisation manuelle jusqu'à l'expiration du cooldown. Discutable —
        c'est justement le cas où une saisie manuelle serait utile — mais c'est
        le comportement actuel.
        """
        repo.get_cached.return_value = {"place_id": None, "resolved_at": datetime.datetime(2026, 7, 26)}
        criteria = self.criteria_with(["AD09FR40"], [make_city_location(insee="75115")])

        geo.remember_manual_place_id(criteria, repo=repo)

        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        ("place_ids", "locations", "case"),
        [
            (
                ["AD09FR40", "AD08FR31096"],
                [make_city_location(insee="75115")],
                "deux placeIds, une localisation",
            ),
            (
                ["AD09FR40"],
                [
                    make_city_location(insee="75115"),
                    make_city_location(city="Lyon", postal_code="69007", insee="69387"),
                ],
                "un placeId, deux localisations",
            ),
            (
                ["AD09FR40", "AD08FR31096"],
                [
                    make_city_location(insee="75115"),
                    make_city_location(city="Lyon", postal_code="69007", insee="69387"),
                ],
                "deux et deux : l'appariement reste indéterminé",
            ),
            ([], [make_city_location(insee="75115")], "aucun placeId"),
            (["AD09FR40"], [], "aucune localisation"),
        ],
        ids=["two_ids", "two_locations", "two_and_two", "no_id", "no_location"],
    )
    def test_an_ambiguous_pairing_is_never_banked(self, repo, place_ids, locations, case):
        criteria = self.criteria_with(place_ids, locations)

        geo.remember_manual_place_id(criteria, repo=repo)

        repo.set_cached.assert_not_called(), case

    def test_a_location_without_an_insee_code_has_nothing_to_key_on(self, repo):
        """Localisation tapée à la main sans passer par l'autocomplete : pas de
        code INSEE, donc pas de clé. No-op silencieux, pas une erreur."""
        criteria = self.criteria_with(["AD09FR40"], [{"city": "Paris", "postalCode": "75015"}])

        geo.remember_manual_place_id(criteria, repo=repo)

        repo.get_cached.assert_not_called()
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        ("criteria", "case"),
        [
            ({}, "critères vides"),
            ({"locations": []}, "ni placeIds ni localisations"),
            ({"sourceOverrides": {"laforet": {"placeIds": ["X"]}}}, "surcharge d'une AUTRE source"),
            ({"sourceOverrides": "pas un dict"}, "surcharges malformées"),
            ({"sourceOverrides": {"seloger": {"placeIds": None}}}, "placeIds nul"),
        ],
        ids=["empty", "no_overrides", "other_source", "malformed_overrides", "null_place_ids"],
    )
    def test_nothing_to_remember_is_a_silent_no_op(self, repo, criteria, case):
        geo.remember_manual_place_id(criteria, repo=repo)

        repo.get_cached.assert_not_called(), case
        repo.set_cached.assert_not_called(), case
