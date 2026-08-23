"""Tests unitaires de `services/pap_geocode.py`.

Ce module traduit un périmètre canonique (voir core/criteria.py) en
identifiant numérique opaque de pap.fr (`439` pour Paris, `37782` pour Paris
15e, `397` pour la Gironde), en interrogeant l'autocomplete public du site et
en mémorisant le résultat — même rôle que services/seloger_geocode.py,
services/bienici_geocode.py et services/century21_geocode.py.

Deux différences de fond, chacune avec sa section de tests :

  * TOUS les niveaux de périmètre ont un identifiant natif — ville, ville
    entière, département ET région (« Île-de-France » -> 471) : aucune
    élargissement région -> départements ici ;
  * la désambiguïsation lit les formes affichées par le site : parenthèse
    courte = numéro de département (« Rennes (35) »), parenthèse longue = code
    postal (« Courbevoie (92400) »), suffixe ordinal = arrondissement
    (« Paris 15e »), départements affichés « {nom} - {code} ». Cas particulier
    vérifié en direct : le 75 n'a aucune entrée « - 75 » (l'autocomplete
    renvoie la ville puis ses arrondissements) — repli sur la ville entière.

Les réponses d'autocomplete utilisées ici sont les CAPTURES RÉELLES du
2026-08-23 (tests/fixtures/pap/ac-geo/, voir SCENARIOS.md).

Aucun appel réseau : PAP passe par curl_cffi (empreinte TLS navigateur), que
ni le garde-fou global `no_network` ni requests_mock ne couvrent — le module
curl_cffi est donc doublé ci-dessous via unittest.mock.patch.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from curl_cffi.requests.exceptions import HTTPError as CurlHTTPError
from freezegun import freeze_time
from loguru import logger

from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY
from repositories.pap_geo_repo import PapGeoRepository
from services import pap_geocode as geo
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)

FROZEN = "2026-08-23 12:00:00"

# ---------------------------------------------------------------------------
# Captures réelles de l'autocomplete /json/ac-geo (octets originaux)
# ---------------------------------------------------------------------------

AC_GEO_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "pap" / "ac-geo"


def load_ac(name: str) -> list[dict]:
    """Une capture réelle de l'autocomplete pap.fr."""
    return json.loads((AC_GEO_DIR / name).read_text())


RENNES_AC = load_ac("ville-rennes.json")
PARIS_AC = load_ac("ville-paris-arrondissements.json")
PARIS_15_AC = load_ac("ville-cp-arrondissement-75015.json")
GIRONDE_AC = load_ac("departement-33.json")

IDF_AC = [{"id": 471, "name": "Île-de-France"}]
CORSE_DU_SUD_AC = [{"id": 383, "name": "Corse-du-Sud - 2A"}]


# ---------------------------------------------------------------------------
# Doubles et fixtures
# ---------------------------------------------------------------------------

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
    double = MagicMock(spec=PapGeoRepository)
    double.get_cached.return_value = None
    return double


@pytest.fixture
def autocomplete(monkeypatch):
    """Remplace le transport curl_cffi du module et enregistre les requêtes.

    `install(results)` rejoue une réponse JSON ; `side_effect` lève à la place.
    Renvoie la liste des kwargs reçus (params/headers/timeout vérifiables)."""

    def install(results=None, side_effect=None):
        calls: list[dict] = []

        def fake_get(url, **kwargs):
            calls.append({"url": url, **kwargs})
            if side_effect is not None:
                raise side_effect
            response = MagicMock()
            response.raise_for_status.return_value = None
            response.json.return_value = results if results is not None else []
            return response

        fake_module = MagicMock()
        fake_module.get.side_effect = fake_get
        monkeypatch.setattr(geo, "curl_requests", fake_module)
        return calls

    return install


# ===========================================================================
# P1 — _query_autocomplete : le contrat exact avec l'endpoint
# ===========================================================================


class TestQueryAutocomplete:
    def test_the_request_matches_the_site_s_own_search_bar(self, autocomplete):
        """Un GET sur `/json/ac-geo` avec `q`, X-Requested-With (endpoint AJAX)
        et un Accept JSON — sans quoi la réponse est vide (SCENARIOS.md)."""
        calls = autocomplete(PARIS_15_AC)

        assert geo._query_autocomplete("75015") == PARIS_15_AC

        assert len(calls) == 1
        assert calls[0]["url"] == geo.AC_GEO_URL
        assert calls[0]["params"] == {"q": "75015"}
        assert calls[0]["headers"]["X-Requested-With"] == "XMLHttpRequest"
        assert "application/json" in calls[0]["headers"]["Accept"]
        assert calls[0]["timeout"] == 10
        # L'empreinte TLS navigateur est obligatoire (Cloudflare filtre sinon).
        assert calls[0]["impersonate"] == geo.IMPERSONATE

    @pytest.mark.parametrize("text", ["", "   ", None], ids=["vide", "blancs", "none"])
    def test_an_empty_query_never_reaches_the_network(self, autocomplete, text):
        calls = autocomplete([])

        assert geo._query_autocomplete(text) == []
        assert calls == [], "aucune requête pour une requête vide"

    def test_an_http_error_is_raised_not_swallowed(self, autocomplete):
        """raise_for_status reste responsable ici : c'est _resolve_uncached
        qui rattrape tout et mémorise l'échec comme réessayable."""
        response = MagicMock()
        response.raise_for_status.side_effect = CurlHTTPError("500 Server Error")
        autocomplete(side_effect=response.raise_for_status.side_effect)

        with pytest.raises(CurlHTTPError, match="500"):
            geo._query_autocomplete("75015")

    def test_a_non_list_response_is_treated_as_no_result(self, autocomplete):
        """Une réponse inattendue (dict d'erreur) vaut liste vide : l'échec
        sera mémorisé comme tel par l'appelant plutôt que de lever."""
        calls = autocomplete({"erreur": "oops"})

        assert geo._query_autocomplete("75015") == []
        assert len(calls) == 1


# ===========================================================================
# P1 — _normalize_name / _names_match : parenthèses et ordinaux
# ===========================================================================


class TestNormalizeName:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Paris", "PARIS"),
            # Parenthèse courte OU longue : toutes deux retirées.
            ("Paris (75)", "PARIS"),
            ("Courbevoie (92400)", "COURBEVOIE"),
            # Suffixes ordinaux des arrondissements, toutes graphies observées.
            ("Paris 15e", "PARIS"),
            ("Paris 1er", "PARIS"),
            ("Lyon 3eme", "LYON"),
            ("Marseille 2nde", "MARSEILLE"),
            ("Paris 5E", "PARIS"),
            # Accents, ponctuation, espaces resserrés.
            ("Saint-Étienne", "SAINT ETIENNE"),
            ("Île-de-France", "ILE DE FRANCE"),
            ("L'Haÿ-les-Roses", "L HAY LES ROSES"),
        ],
    )
    def test_names_are_upper_ascii_and_compact(self, text, expected):
        assert geo._normalize_name(text) == expected

    def test_the_ordinal_removal_stays_bounded_to_name_endings(self):
        """Le retrait ordinal ne touche que les FINS de nom : « Le Mans » ou
        « Vaux-le-Pénil » ne perdent rien (aucun chiffre final)."""
        assert geo._normalize_name("Vaux-le-Pénil") == "VAUX LE PENIL"


class TestNamesMatch:
    @pytest.mark.parametrize(
        ("entry", "target", "expected"),
        [
            # Ville entière, arrondissement et CP convergent vers PARIS.
            ("Paris (75)", "Paris", True),
            ("Paris 15e", "Paris", True),
            ("Paris (75015)", "Paris", True),
            ("Rennes (35)", "Rennes", True),
            # Homonymes composés : jamais rapprochés de la ville simple.
            ("Rennes-sur-Loue (25440)", "Rennes", False),
            # Même forme, autre ville : non.
            ("Courbevoie (92400)", "Paris", False),
        ],
        ids=["ville_entiere", "arrondissement", "cp_parenthese", "parenthese_courte",
             "homonyme_compose", "autre_ville"],
    )
    def test_the_display_forms_converge_or_diverge(self, entry, target, expected):
        assert geo._names_match(entry, target) is expected

    @pytest.mark.parametrize(
        ("entry", "target"),
        [("", "Paris"), (None, "Paris"), ("PARIS", ""), ("PARIS", None)],
        ids=["entry_vide", "entry_none", "cible_vide", "cible_none"],
    )
    def test_a_missing_name_never_matches(self, entry, target):
        assert geo._names_match(entry, target) is False


# ===========================================================================
# P1 — _pick_city / _pick_whole_city / _pick_department / _pick_region
# ===========================================================================


class TestPickCity:
    def test_the_arrondissement_entry_wins_by_name_after_ordinal_strip(self):
        """Capture q=75015 : UNE entrée « Paris 15e » (37782), rapprochée de
        « Paris » après retrait de l'ordinal."""
        assert geo._pick_city(PARIS_15_AC, "Paris")["id"] == 37782

    def test_fallback_to_the_first_entry_of_the_postal_code(self):
        """Aucune entrée ne porte le nom demandé : la première du code postal
        convient — matches_locations garde en aval le résultat honnête."""
        entries = [{"id": 2441, "name": "33500"}, {"id": 99999, "name": "Nawak"}]

        assert geo._pick_city(entries, "Bordeaux")["id"] == 2441

    def test_no_result_returns_none(self):
        assert geo._pick_city([], "Paris") is None


class TestPickWholeCity:
    def test_rennes_resolves_to_the_whole_city_never_to_a_homonym(self):
        """🔒 Capture q=Rennes : « Rennes (35) » (43618) devant quatre
        homonymes composés (« Rennes-sur-Loue », « Rennes-le-Château »...) —
        JAMAIS de repli sur un homonyme (16451 etc.)."""
        picked = geo._pick_whole_city(RENNES_AC, "Rennes")

        assert picked["id"] == 43618
        assert picked["name"] == "Rennes (35)"

    def test_paris_prefers_the_short_parenthesis_over_the_arrondissements(self):
        """🔒 Capture q=Paris : « Paris (75) » (439, parenthèse courte = numéro
        de département) devant dix-neuf arrondissements sans parenthèse."""
        assert geo._pick_whole_city(PARIS_AC, "Paris")["id"] == 439

    def test_a_long_parenthesis_postal_code_is_accepted_as_fallback(self):
        """« Courbevoie (92400) » : parenthèse longue = code postal, acceptée
        en repli (commune unique de son nom)."""
        entries = [{"id": 12345, "name": "Courbevoie (92400)"}]

        assert geo._pick_whole_city(entries, "Courbevoie")["id"] == 12345

    def test_a_non_matching_candidate_is_never_returned_blindly(self):
        """Une requête par nom n'est pas scopée : aucun candidat ne portant le
        nom demandé -> None, jamais la première entrée venue."""
        assert geo._pick_whole_city(RENNES_AC, "Nantes") is None

    def test_an_empty_result_returns_none(self):
        assert geo._pick_whole_city([], "Paris") is None


class TestPickDepartment:
    def test_gironde_is_read_in_the_site_s_own_dash_form(self):
        """🔒 Capture q=33 : « Gironde - 33 » (397) devant des codes postaux
        nus (« 33500 » id=2441...) qui ne doivent pas être pris pour le
        département."""
        picked = geo._pick_department(GIRONDE_AC, "33", "Gironde")

        assert picked["id"] == 397
        assert picked["name"] == "Gironde - 33"

    def test_the_corse_department_code_2a_is_matched(self):
        assert geo._pick_department(CORSE_DU_SUD_AC, "2A", "Corse-du-Sud")["id"] == 383

    def test_paris_has_no_dash_75_entry_and_falls_back_to_the_whole_city(self):
        """Cas particulier vérifié en direct : q=75 renvoie la ville entière
        puis ses arrondissements, AUCUNE entrée « - 75 ». La commune unique
        couvrant tout le département, le repli est exact."""
        assert geo._pick_department(PARIS_AC, "75", "Paris")["id"] == 439

    def test_no_match_and_no_name_gives_up(self):
        assert geo._pick_department([{"id": 1, "name": "Nawak"}], "99", "") is None


class TestPickRegion:
    def test_the_region_is_matched_on_its_official_normalized_name(self):
        """PAP référence les régions sous leurs noms officiels actuels :
        correspondance exacte après normalisation (accents compris)."""
        assert geo._pick_region(IDF_AC, "Île-de-France")["id"] == 471

    def test_an_unrecognized_region_name_is_a_visible_failure(self):
        """Aucun repli flou : un nom non reconnu doit rester un échec visible
        plutôt que risquer un périmètre faux."""
        assert geo._pick_region(IDF_AC, "Bretagne") is None
        assert geo._pick_region([], "Île-de-France") is None


# ===========================================================================
# P1 — area_cache_key : même convention que SeLoger / bienici / Century 21
# ===========================================================================


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
            geo.area_cache_key(make_department_location(code="35")),
            geo.area_cache_key(make_region_location(code="35")),
            geo.area_cache_key(make_city_location(insee="35")),
        }
        assert len(keys) == 3, f"collision de clés : {keys}"

    def test_the_insee_code_is_the_preferred_key_when_present(self):
        """🔒 Le inseeCode fourni par l'autocomplete ne doit jamais être perdu
        en route : c'est lui qui fait la clé quand il est là."""
        location = make_city_location("Paris", "75015", "75115")

        assert geo.area_cache_key(location) == "75115"
        assert geo.area_cache_key(make_whole_city_location("Paris", ("75015",), "75056")) == "city:75056"

    def test_a_city_without_insee_falls_back_to_its_postal_code(self):
        """Une commune tapée à la main (sans code INSEE) a quand même une clé,
        dérivée du code postal — même contrat que bienici."""
        assert geo.area_cache_key({"kind": CITY, "city": "Rennes", "postalCode": "35000"}) == "postal:35000"

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


# ===========================================================================
# P1 — _resolve_uncached : routage par niveau sur les captures réelles
# ===========================================================================


class TestResolveUncached:
    def test_a_city_queries_by_postal_code_and_picks_by_name(self, autocomplete):
        calls = autocomplete(PARIS_15_AC)
        location = make_city_location("Paris", "75015", "75115")

        assert geo._resolve_uncached(location) == "37782"
        assert [c["params"]["q"] for c in calls] == ["75015"]

    def test_a_whole_city_queries_by_name_and_avoids_homonyms(self, autocomplete):
        calls = autocomplete(RENNES_AC)

        assert geo._resolve_uncached(make_whole_city_location("Rennes", ("35000",), "35238")) == "43618"
        assert [c["params"]["q"] for c in calls] == ["Rennes"]

    def test_a_department_queries_by_its_code(self, autocomplete):
        calls = autocomplete(GIRONDE_AC)

        assert geo._resolve_uncached(make_department_location(code="33")) == "397"
        assert [c["params"]["q"] for c in calls] == ["33"]

    def test_a_region_has_a_native_identifier_queried_by_name(self, autocomplete):
        """Contrairement aux autres sources, la RÉGION a un identifiant PAP :
        pas d'élargissement région -> départements ici."""
        calls = autocomplete(IDF_AC)

        assert geo._resolve_uncached(make_region_location(code="11", name="Île-de-France")) == "471"
        assert [c["params"]["q"] for c in calls] == ["Île-de-France"]

    def test_a_region_without_a_name_never_queries_anything(self, autocomplete):
        """Pas de requête par code possible pour une région (« 11 » renvoie
        l'Aude : codes région et département partagent le même espace ambigu)."""
        calls = autocomplete([])

        assert geo._resolve_uncached(make_region_location(code="11", name="")) is None
        assert calls == []

    def test_a_city_without_postal_code_makes_no_request_at_all(self, autocomplete):
        calls = autocomplete([])

        assert geo._resolve_uncached({"kind": CITY, "city": "Paris"}) is None
        assert calls == [], "_query_autocomplete('') court-circuite avant le réseau"

    def test_an_unknown_kind_resolves_to_none(self, autocomplete):
        autocomplete([])

        assert geo._resolve_uncached({"kind": "canton", "name": "Gironde"}) is None

    @pytest.mark.parametrize(
        ("error", "case"),
        [
            (CurlHTTPError("réseau coupé"), "panne réseau"),
            (ValueError("réponse illisible"), "JSON inattendu"),
            (KeyError("id"), "champ manquant"),
            (TimeoutError("délai dépassé"), "timeout"),
        ],
        ids=["network", "bad_json", "missing_field", "timeout"],
    )
    def test_it_never_raises_whatever_goes_wrong(self, autocomplete, log_messages, error, case):
        """None sur tout ce qui n'est pas une correspondance nette — jamais
        d'exception levée, les appelants mémorisent ça comme un échec
        réessayable."""
        autocomplete([], side_effect=error)

        assert geo._resolve_uncached(make_city_location()) is None, case
        assert geo._resolve_uncached(make_department_location()) is None, case
        assert any("Résolution échouée" in m for m in log_messages)


# ===========================================================================
# P1 — _seconds_since : âge d'un échec mémorisé
# ===========================================================================


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


# ===========================================================================
# P1 — resolve_geo_id : la table de vérité du cache
# (miss -> résolution -> set_cached ; hit ; cooldown sur échec mémorisé)
# ===========================================================================


class TestResolveGeoIdCacheTruthTable:
    @staticmethod
    def spy_resolution(monkeypatch, result="43618"):
        calls: list[dict] = []
        monkeypatch.setattr(geo, "_resolve_uncached", lambda location: calls.append(location) or result)
        return calls

    @freeze_time(FROZEN)
    def test_an_unidentifiable_area_warns_and_never_touches_the_cache(self, repo, monkeypatch, log_messages):
        calls = self.spy_resolution(monkeypatch)

        assert geo.resolve_geo_id({"kind": CITY}, repo=repo) is None

        assert calls == []
        repo.get_cached.assert_not_called()
        repo.set_cached.assert_not_called()
        assert any("Périmètre non identifiable" in m for m in log_messages)

    @freeze_time(FROZEN)
    def test_a_cached_geo_id_is_returned_immediately(self, repo, monkeypatch):
        """Hit : la résolution réseau ne doit jamais être tentée, ni réécrite."""
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = {"area_key": "35238", "geo_id": "43618", "resolved_at": None}

        assert geo.resolve_geo_id(make_city_location(insee="35238"), repo=repo) == "43618"

        repo.get_cached.assert_called_once_with("35238")
        assert calls == []
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        "age_seconds", [0, 3600, 7 * 24 * 3600 - 1], ids=["just_now", "une_heure", "une_seconde_avant_exp"],
    )
    @freeze_time(FROZEN)
    def test_a_recent_failure_is_not_retried(self, repo, monkeypatch, age_seconds):
        """🔒 CACHE NÉGATIF ≠ jamais tenté : un échec mémorisé (geo_id NULL)
        coupe toute nouvelle tentative avant 7 jours — sans ce délai, un
        périmètre introuvable martèlerait l'autocomplete à chaque scrape."""
        calls = self.spy_resolution(monkeypatch)
        resolved_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=age_seconds)
        repo.get_cached.return_value = {"area_key": "35238", "geo_id": None, "resolved_at": resolved_at}

        assert geo.resolve_geo_id(make_city_location(insee="35238"), repo=repo) is None

        assert calls == []
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        "age_seconds", [7 * 24 * 3600, 30 * 24 * 3600], ids=["exactement_sept_jours", "un_mois"],
    )
    @freeze_time(FROZEN)
    def test_an_expired_failure_is_retried_and_rewritten(self, repo, monkeypatch, age_seconds):
        calls = self.spy_resolution(monkeypatch)
        resolved_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=age_seconds)
        repo.get_cached.return_value = {"area_key": "35238", "geo_id": None, "resolved_at": resolved_at}

        assert geo.resolve_geo_id(make_city_location(insee="35238"), repo=repo) == "43618"

        assert len(calls) == 1
        repo.set_cached.assert_called_once_with("35238", "43618")

    @freeze_time(FROZEN)
    def test_a_failure_without_a_resolution_date_is_retried(self, repo, monkeypatch):
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = {"area_key": "35238", "geo_id": None, "resolved_at": None}

        assert geo.resolve_geo_id(make_city_location(insee="35238"), repo=repo) == "43618"
        assert len(calls) == 1

    @freeze_time(FROZEN)
    def test_a_first_lookup_resolves_then_caches(self, repo, monkeypatch, log_messages):
        """Miss -> résolution -> set_cached : le couple (clé, id) est écrit
        pour que les scrapes suivants court-circuitent le réseau."""
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = None
        location = make_city_location("Rennes", "35000", "35238")

        assert geo.resolve_geo_id(location, repo=repo) == "43618"

        assert calls == [location]
        repo.set_cached.assert_called_once_with("35238", "43618")
        assert any("Rennes (35000) -> g43618" in m for m in log_messages)

    @freeze_time(FROZEN)
    def test_a_failed_resolution_is_cached_as_none_with_a_warning(self, repo, monkeypatch, log_messages):
        self.spy_resolution(monkeypatch, result=None)
        repo.get_cached.return_value = None

        assert geo.resolve_geo_id(make_city_location(insee="35238"), repo=repo) is None

        repo.set_cached.assert_called_once_with("35238", None)
        assert any("Aucun identifiant trouvé" in m for m in log_messages)

    @pytest.mark.parametrize(
        ("location", "expected_key"),
        [
            (make_city_location(insee="35238"), "35238"),
            (make_whole_city_location(insee="75056"), "city:75056"),
            (make_department_location(code="33"), "dept:33"),
            (make_region_location(code="11"), "region:11"),
        ],
        ids=["city", "whole_city", "department", "region"],
    )
    @freeze_time(FROZEN)
    def test_every_level_reads_and_writes_under_its_area_key(self, repo, monkeypatch, location, expected_key):
        """Le niveau fait partie de la clé : un même code à deux niveaux ne
        partage jamais de ligne."""
        self.spy_resolution(monkeypatch)

        geo.resolve_geo_id(location, repo=repo)

        repo.get_cached.assert_called_once_with(expected_key)
        repo.set_cached.assert_called_once_with(expected_key, "43618")


# ===========================================================================
# P1 — remember_manual_geo_ids : banque des identifiants saisis à la main
# ===========================================================================


class TestRememberManualGeoIds:
    @staticmethod
    def criteria_with(geo_ids, locations):
        from tests.helpers.factories import make_criteria

        return make_criteria(locations=locations, sourceOverrides={"pap": {"geoIds": geo_ids}})

    def test_one_manual_id_and_one_location_are_banked_together(self, repo):
        criteria = self.criteria_with(["43618"], [make_city_location(insee="35238")])

        geo.remember_manual_geo_ids(criteria, repo=repo)

        repo.set_cached.assert_called_once_with("35238", "43618")

    def test_the_first_id_wins_when_the_user_pasted_several(self, repo):
        criteria = self.criteria_with(["43618", "37782"], [make_city_location(insee="35238")])

        geo.remember_manual_geo_ids(criteria, repo=repo)

        repo.set_cached.assert_called_once_with("35238", "43618")

    def test_an_existing_cache_entry_is_never_overwritten(self, repo):
        """La banque ne sert qu'à capitaliser ce que la résolution ignore :
        elle n'écrase jamais une résolution automatique existante."""
        repo.get_cached.return_value = {"area_key": "35238", "geo_id": "16451", "resolved_at": None}
        criteria = self.criteria_with(["43618"], [make_city_location(insee="35238")])

        geo.remember_manual_geo_ids(criteria, repo=repo)

        repo.get_cached.assert_called_once_with("35238")
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        ("geo_ids", "locations", "case"),
        [
            (["43618"], [], "aucune localisation"),
            (
                ["43618"],
                [make_city_location(insee="35238"), make_city_location("Paris", "75015", "75115")],
                "deux localisations : association ambiguë",
            ),
            ([], [make_city_location(insee="35238")], "aucun id"),
        ],
        ids=["sans_localisation", "deux_localisations", "sans_id"],
    )
    def test_nothing_to_remember_is_a_silent_no_op(self, repo, geo_ids, locations, case):
        criteria = self.criteria_with(geo_ids, locations)

        geo.remember_manual_geo_ids(criteria, repo=repo)

        assert repo.set_cached.call_count == 0, case

    def test_a_location_without_insee_is_still_banked_under_its_postal_key(self, repo):
        repo.get_cached.return_value = None
        criteria = self.criteria_with(["37782"], [{"kind": CITY, "city": "Paris", "postalCode": "75015"}])

        geo.remember_manual_geo_ids(criteria, repo=repo)

        repo.set_cached.assert_called_once_with("postal:75015", "37782")

    def test_a_location_with_no_key_at_all_has_nothing_to_key_on(self, repo):
        criteria = self.criteria_with(["439"], [{"kind": CITY}])

        geo.remember_manual_geo_ids(criteria, repo=repo)

        repo.get_cached.assert_not_called()
        repo.set_cached.assert_not_called()
