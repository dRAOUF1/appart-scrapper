"""Tests unitaires de `services/orpi_geocode.py`.

Ce module traduit un périmètre canonique (voir core/criteria.py) en slug d'URL
Orpi (`rosny-sous-bois`, `gironde`, `ile-de-france`), en interrogeant
l'autocomplete public du site et en mémorisant le résultat — même rôle que
services/seloger_geocode.py et services/century21_geocode.py, dont ce fichier
de test reprend la structure.

Trois différences de fond, chacune avec sa section de tests :

  * l'autocomplete répond un DICT de groupes par type de périmètre (`city`,
    `department`, `region`...) — pas une liste plate comme Century 21/PAP —
    et les groupes absents sont gérés (_group) ;
  * les RÉGIONS ont un identifiant natif (« Île-de-France » ->
    `ile-de-france`) mais AUCUNE requête possible par code (« 11 » renvoie
    l'Aude) : la requête passe par le nom slugifié, et seul le NOM demandé
    est retenu contre les correspondances floues du site ;
  * la Corse n'a aucune entrée par code (« 2A » -> [], capture réelle d'un
    TABLEAU JSON vide) : ses départements se requêtent par leur NOM.

Les captures réelles du 2026-08-23 (tests/fixtures/orpi/, voir SCENARIOS.md)
sont rejouées telles quelles : pièges « 33 Hectares », trois régions floues,
districts mélangés aux villes.

Aucun appel réseau : le socle bloque le transport HTTP (tests/conftest.py),
`requests_mock` sert d'adaptateur pour les tests qui exercent la requête.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time
from loguru import logger

from core.geocode import CITY, DEPARTMENT, REGION, WHOLE_CITY
from repositories.orpi_geo_repo import OrpiGeoRepository
from services import orpi_geocode as geo
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)

FROZEN = "2026-08-23 12:00:00"

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "orpi"


def load_fixture(name: str) -> dict:
    """Une capture réelle de l'autocomplete Orpi (octets originaux)."""
    return json.loads((FIXTURES_DIR / name).read_text())


# Réponses de l'autocomplete TELLES QUE capturées en direct le 2026-08-23.
VILLE_93110 = load_fixture("autocomplete_ville_93110.json")
DEPT_33 = load_fixture("autocomplete_departement_33.json")
REGION_IDF = load_fixture("autocomplete_region_par_nom.json")
CORSE_PAR_NOM = load_fixture("autocomplete_departement_corse_du_sud_par_nom.json")
CORSE_2A = load_fixture("autocomplete_departement_2A.json")  # Un TABLEAU [] !

# Entrées synthétiques : homonymes partageant le même code postal (cas cité
# par le code lui-même), jamais capturables dans une seule réponse.
HAYBES = {"value": "haybes", "area": "city", "zipcode": ["08170"], "name": "Haybes"}
FUMAY = {"value": "fumay", "area": "city", "zipcode": ["08170"], "name": "Fumay"}
PARIS_DEPARTMENT = {"value": "paris", "area": "department", "name": "Paris"}
LYON_ARRONDISSEMENTS = [
    {"value": "lyon-1", "area": "district", "name": "Lyon 1er"},
    {"value": "lyon-3", "area": "district", "name": "Lyon 3e"},
]


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
    double = MagicMock(spec=OrpiGeoRepository)
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
# _query_autocomplete — P2 : le contrat HTTP avec l'endpoint public
# ---------------------------------------------------------------------------

class TestQueryAutocomplete:
    def test_the_term_is_a_path_segment_with_the_desktop_headers(self, requests_mock):
        """Le terme est un segment de CHEMIN (/recherche/autocompletion/93110),
        pas un paramètre ?q= — capture rejouée à l'identique."""
        mock = requests_mock.get(f"{geo.AUTOCOMPLETION_URL}93110", json=VILLE_93110)

        assert geo._query_autocomplete("93110") == VILLE_93110

        request = mock.last_request
        assert request.path == "/recherche/autocompletion/93110"
        assert request.headers["Accept"] == "application/json"
        assert "Chrome" in request.headers["User-Agent"]

    @pytest.mark.parametrize("text", ["", "9", None], ids=["vide", "un_caractere", "none"])
    def test_a_text_shorter_than_two_characters_never_reaches_the_network(self, text, requests_mock):
        mock = requests_mock.get(f"{geo.AUTOCOMPLETION_URL}x", json={})

        assert geo._query_autocomplete(text) == {}

        assert mock.call_count == 0

    def test_a_non_dict_response_is_treated_as_empty(self, requests_mock):
        """🔒 Capture autocomplete_departement_2A.json : la Corse répond un
        TABLEAU JSON vide de 2 octets — réponse inattendue mais valide. Elle
        vaut « aucun groupe » plutôt qu'une exception : l'échec sera mémorisé
        comme tel par l'appelant."""
        requests_mock.get(f"{geo.AUTOCOMPLETION_URL}2A", text=json.dumps(CORSE_2A))

        assert geo._query_autocomplete("2A") == {}

    @pytest.mark.parametrize("status", [400, 429, 500, 503])
    def test_an_http_error_is_raised_not_swallowed(self, status, requests_mock):
        """C'est _resolve_uncached qui avale les erreurs (et les mémorise) :
        cette fonction doit laisser lever, sinon un échec réseau passerait
        pour « aucun groupe » sans trace."""
        requests_mock.get(f"{geo.AUTOCOMPLETION_URL}33", status_code=status, json={})

        with pytest.raises(Exception, match=str(status)):
            geo._query_autocomplete("33")


# ---------------------------------------------------------------------------
# _normalize_name / _names_match
# ---------------------------------------------------------------------------

class TestNormalizeName:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Rosny-sous-Bois", "ROSNY SOUS BOIS"),
            # Le « (93110) » accolé par les entrées de l'autocomplete est retiré.
            ("Rosny-sous-Bois (93110)", "ROSNY SOUS BOIS"),
            # Accents : Île-de-France et Ile-de-France convergent.
            ("Île-de-France", "ILE DE FRANCE"),
            ("Ile-de-France", "ILE DE FRANCE"),
            ("Corse-du-Sud", "CORSE DU SUD"),
            ("France d'outre-mer", "FRANCE D OUTRE MER"),
        ],
        ids=["simple", "avec_cp", "accentee", "non_accentee", "tirets", "apostrophe"],
    )
    def test_names_are_upper_ascii_and_compact(self, text, expected):
        assert geo._normalize_name(text) == expected


class TestNamesMatch:
    @pytest.mark.parametrize(
        ("entry_name", "target", "expected"),
        [
            ("Rosny-sous-Bois (93110)", "Rosny-sous-Bois", True),
            ("Ile-de-France", "Île-de-France", True),
            ("Corse-du-Sud", "Haute-Corse", False),
            ("Hauts-de-France", "Île-de-France", False),
        ],
        ids=["cp_accole", "accents", "departements_distincts", "regions_floues"],
    )
    def test_the_name_carries_the_disambiguation(self, entry_name, target, expected):
        assert geo._names_match({"name": entry_name}, target) is expected

    def test_the_label_is_used_when_the_name_is_missing(self):
        assert geo._names_match({"label": "Gironde"}, "gironde") is True

    @pytest.mark.parametrize(
        ("entry", "target"),
        [({}, "Paris"), ({"name": ""}, "Paris"), ({"name": "Paris"}, ""), ({"name": "Paris"}, None)],
        ids=["entree_vide", "nom_vide", "cible_vide", "cible_none"],
    )
    def test_a_missing_name_never_matches(self, entry, target):
        assert geo._names_match(entry, target) is False


# ---------------------------------------------------------------------------
# _pick_city — commune précise depuis une requête PAR CODE POSTAL
# ---------------------------------------------------------------------------

class TestPickCity:
    def test_a_real_capture_resolves_by_name_and_postal_code(self):
        """Capture autocomplete_ville_93110.json : le groupe city porte la
        liste des CP ; les DISTRICTS (rosny-sous-bois-centre-ville...) partagent
        la réponse et ne doivent jamais être pris pour des villes."""
        assert geo._pick_city(VILLE_93110, "93110", "Rosny-sous-Bois") == "rosny-sous-bois"

    def test_the_zipcode_group_value_is_never_used_as_a_slug(self):
        """Piège de la capture : l'entrée zipcode vaut `cp-93110`, préfixée —
        ce n'est PAS un slug utilisable. Seul le groupe city est lu."""
        slug = geo._pick_city(VILLE_93110, "93110", "Rosny-sous-Bois")

        assert slug == "rosny-sous-bois"
        assert not slug.startswith("cp-")

    @pytest.mark.parametrize(
        "results", [[FUMAY, HAYBES], [HAYBES, FUMAY]], ids=["fumay_dabord", "haybes_dabord"]
    )
    def test_a_shared_postal_code_is_disambiguated_by_name(self, results):
        """« Haybes » et « Fumay » partagent le 08170 : seul le nom tranche,
        quel que soit l'ordre de la réponse."""
        assert geo._pick_city({"city": results}, "08170", "Haybes") == "haybes"
        assert geo._pick_city({"city": results}, "08170", "Fumay") == "fumay"

    def test_a_fallback_to_the_first_city_of_the_postal_code(self):
        """Un nom abrégé par le site ne se rapproche pas : la première ville du
        CP reste le bon périmètre — matches_locations garde le résultat honnête."""
        abbreviated = [{"value": "st-etienne", "area": "city", "zipcode": ["42000"], "name": "ST ETIENNE"}]

        assert geo._pick_city({"city": abbreviated}, "42000", "Saint-Étienne") == "st-etienne"

    def test_no_candidate_returns_none(self):
        assert geo._pick_city(VILLE_93110, "75001", "Paris") is None

    def test_an_entry_of_another_postal_code_is_never_a_candidate(self):
        assert geo._pick_city(VILLE_93110, "08170", "Rosny-sous-Bois") is None


# ---------------------------------------------------------------------------
# _pick_whole_city — ville ENTIÈRE (tous arrondissements confondus)
# ---------------------------------------------------------------------------

class TestPickWholeCity:
    def test_paris_takes_the_homonymous_department_entry(self):
        """Vérifié en direct : q=75 comme q=paris renvoient une entrée
        départementale `paris`, qui couvre les 20 arrondissements."""
        results = {
            "department": [PARIS_DEPARTMENT],
            "city": [{"value": "paris-1", "area": "city", "name": "Paris 1er"}],
        }

        assert geo._pick_whole_city(results, "Paris") == "paris"

    def test_lyon_falls_back_to_the_naked_slug(self):
        """L'autocomplete n'expose QUE les arrondissements (`lyon-1`...) mais
        le moteur accepte le slug nu (399 annonces vérifiées en live)."""
        assert geo._pick_whole_city({"city": LYON_ARRONDISSEMENTS}, "Lyon") == "lyon"

    def test_the_naked_slug_comes_even_from_an_empty_response(self):
        assert geo._pick_whole_city({}, "Lyon") == "lyon"

    def test_accents_are_stripped_from_the_derived_slug(self):
        assert geo._pick_whole_city({}, "Châteauroux") == "chateauroux"

    def test_an_empty_name_yields_none(self):
        assert geo._pick_whole_city({}, "") is None


# ---------------------------------------------------------------------------
# _pick_department — filtre area == "department" et désambiguïsation par nom
# ---------------------------------------------------------------------------

class TestPickDepartment:
    def test_the_real_capture_ignores_the_district_named_33_hectares(self):
        """🔒 Piège tangible de autocomplete_departement_33.json : la requête
        était le CODE 33, mais le groupe district contient « 33 Hectares »
        (Neuilly-sur-Marne). Sans le filtre `area == "department"`, on
        résoudrait Neuilly-sur-Marne au lieu de la Gironde."""
        assert geo._pick_department(DEPT_33, "") == "gironde"

    def test_the_name_picks_the_right_department_among_fuzzy_results(self):
        """Capture corse par nom : le site renvoie Corse-du-Sud ET Haute-Corse.
        Le nom demandé tranche — jamais « première entrée »."""
        assert geo._pick_department(CORSE_PAR_NOM, "Corse-du-Sud") == "corse-du-sud"
        assert geo._pick_department(CORSE_PAR_NOM, "Haute-Corse") == "haute-corse"

    def test_without_a_name_the_first_department_wins(self):
        """Requête par code sans nom de désambiguïsation (le cas nominal :
        « 33 » ne renvoie qu'un département)."""
        assert geo._pick_department(DEPT_33, "") == "gironde"
        assert geo._pick_department(DEPT_33, None) == "gironde"

    def test_no_department_group_returns_none(self):
        """La capture région n'a AUCUNE clé department : pas d'entrée à lire,
        pas d'exception."""
        assert geo._pick_department(REGION_IDF, "") is None


# ---------------------------------------------------------------------------
# _pick_region — correspondance EXACTE du nom normalisé, sans repli flou
# ---------------------------------------------------------------------------

class TestPickRegion:
    def test_the_real_capture_keeps_only_the_exact_region(self):
        """🔒 La requête « ile-de-france » renvoie TROIS régions (flou du site)
        plus des dizaines de villes contenant « france » : seule la région dont
        le nom normalisé correspond exactement est retenue."""
        assert geo._pick_region(REGION_IDF, "Île-de-France") == "ile-de-france"

    def test_an_unknown_name_is_a_visible_failure_not_a_fuzzy_guess(self):
        """Un nom non reconnu doit rester un échec : un repli flou risquerait
        un périmètre faux (Hauts-de-France pour qui demande la Bretagne)."""
        assert geo._pick_region(REGION_IDF, "Bretagne") is None

    def test_no_region_group_returns_none(self):
        assert geo._pick_region(DEPT_33, "Gironde") is None


# ---------------------------------------------------------------------------
# area_cache_key — même convention que SeLoger / bienici / Century 21 / PAP
# ---------------------------------------------------------------------------

class TestAreaCacheKey:
    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            (make_city_location(insee="93066"), "93066"),
            (make_whole_city_location(insee="93000"), "city:93000"),
            (make_department_location(code="93"), "dept:93"),
            (make_region_location(code="11"), "region:11"),
        ],
        ids=["city", "whole_city", "department", "region"],
    )
    def test_each_level_has_its_own_namespace(self, location, expected):
        assert geo.area_cache_key(location) == expected

    def test_the_same_code_at_two_levels_never_collides(self):
        keys = {
            geo.area_cache_key(make_city_location(insee="11")),
            geo.area_cache_key(make_department_location(code="11")),
            geo.area_cache_key(make_region_location(code="11")),
        }
        assert len(keys) == 3, f"collision de clés : {keys}"

    def test_a_city_without_insee_falls_back_to_its_postal_code(self):
        """Une commune tapée à la main (sans code INSEE) a quand même une clé,
        dérivée du code postal — même contrat que Century 21."""
        assert geo.area_cache_key({"kind": CITY, "city": "Rosny", "postalCode": "93110"}) == (
            "postal:93110"
        )

    def test_a_whole_city_without_insee_falls_back_to_its_name(self):
        assert geo.area_cache_key({"kind": WHOLE_CITY, "city": "Montreuil"}) == "city_name:montreuil"

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
# _resolve_uncached — routage par niveau, sur les captures réelles
# ---------------------------------------------------------------------------

class TestResolveUncached:
    def test_a_city_queries_by_postal_code_and_picks_by_name(self, autocomplete):
        asked = autocomplete(VILLE_93110)

        slug = geo._resolve_uncached(make_city_location("Rosny-sous-Bois", "93110", "93066"))

        assert slug == "rosny-sous-bois"
        assert asked == ["93110"]

    def test_a_whole_city_queries_by_name_and_derives_the_naked_slug(self, autocomplete):
        asked = autocomplete({"city": LYON_ARRONDISSEMENTS})

        slug = geo._resolve_uncached(make_whole_city_location("Lyon", ("69001",), "69381"))

        assert slug == "lyon"
        assert asked == ["Lyon"]

    def test_a_department_queries_by_code(self, autocomplete):
        asked = autocomplete(DEPT_33)

        slug = geo._resolve_uncached(make_department_location(code="33", name="Gironde"))

        assert slug == "gironde"
        assert asked == ["33"]

    def test_the_corse_is_queried_by_its_name_because_the_code_answers_an_empty_array(
        self, autocomplete
    ):
        """🔒 « 2A » répond [] en live (capture d'un tableau vide) : le code
        est traduit par `_CORSE_DEPARTMENT_NAMES` en nom (« Corse-du-Sud »),
        qui sert À LA FOIS de terme de requête ET de critère de matching dans
        `_pick_department` — le résultat vient du nom, jamais d'un repli sur
        la première entrée."""
        asked = autocomplete(CORSE_PAR_NOM)

        assert geo._resolve_uncached(make_department_location(code="2A")) == "corse-du-sud"
        assert asked == ["Corse-du-Sud"]

    def test_haute_corse_resolves_when_the_site_lists_it_first(self, autocomplete):
        """Miroir synthétique du cas 2B (aucune capture dédiée) : la requête
        « Haute-Corse » est supposée lister Haute-Corse en premier."""
        results = {
            "department": [e for e in CORSE_PAR_NOM["department"] if e["value"] == "haute-corse"]
        }
        asked = autocomplete(results)

        assert geo._resolve_uncached(make_department_location(code="2B")) == "haute-corse"
        assert asked == ["Haute-Corse"]

    def test_2b_resolves_to_haute_corse_on_the_full_capture_not_to_the_first_entry(
        self, autocomplete
    ):
        """🔒 Non-régression sur la capture COMPLÈTE par nom : elle liste LES
        DEUX départements, corse-du-sud en PREMIER. Avant le matching par nom,
        « 2B » ne matchait aucun libellé et retombait silencieusement sur la
        première entrée — « corse-du-sud », un périmètre faux sans signal.
        Le nom issu de `_CORSE_DEPARTMENT_NAMES` tranche désormais ; et le 2A
        reste juste sur cette même réponse (invariant)."""
        asked = autocomplete(CORSE_PAR_NOM)

        assert geo._resolve_uncached({"kind": DEPARTMENT, "code": "2B"}) == "haute-corse"
        assert geo._resolve_uncached({"kind": DEPARTMENT, "code": "2A"}) == "corse-du-sud"

        # Chaque code a requisêté par son NOM (« 2A »/« 2B » ne renvoient rien).
        assert asked == ["Haute-Corse", "Corse-du-Sud"]

    def test_a_region_with_a_name_queries_by_its_slugified_name(self, autocomplete):
        """Pas de requête région possible par code (« 11 » renvoie l'Aude) :
        la requête passe par le NOM slugifié, l'identifiant natif du site."""
        asked = autocomplete(REGION_IDF)

        slug = geo._resolve_uncached({"kind": REGION, "code": "11", "name": "Île-de-France"})

        assert slug == "ile-de-france"
        assert asked == ["ile-de-france"]

    def test_a_region_without_a_name_is_never_queried(self, autocomplete):
        """Sans nom, c'est le PARSER qui élargit aux départements : aucune
        requête ne doit partir (un code région interrogerait le mauvais espace)."""
        asked = autocomplete(REGION_IDF)

        assert geo._resolve_uncached({"kind": REGION, "code": "11"}) is None
        assert asked == []

    def test_a_department_without_a_code_is_never_queried(self, autocomplete):
        asked = autocomplete({})

        assert geo._resolve_uncached({"kind": DEPARTMENT, "name": "Gironde"}) is None
        assert asked == []

    def test_an_unknown_kind_resolves_to_none(self, autocomplete):
        asked = autocomplete(VILLE_93110)

        assert geo._resolve_uncached({"kind": "canton", "name": "Nawak"}) is None
        assert asked == []

    @pytest.mark.parametrize(
        ("error", "case"),
        [
            (ConnectionError("réseau coupé"), "panne réseau"),
            (ValueError("réponse illisible"), "JSON inattendu"),
            (KeyError("zipcode"), "champ manquant"),
            (TimeoutError("délai dépassé"), "timeout"),
        ],
        ids=["network", "bad_json", "missing_field", "timeout"],
    )
    def test_it_never_raises_whatever_goes_wrong(self, autocomplete, log_messages, error, case):
        """Toute tentative ratée vaut « aucun slug » (et sera mémorisée comme
        échec réessayable par resolve_slug_id) — jamais une exception en plein
        milieu d'un scrape."""
        autocomplete({}, side_effect=error)

        assert geo._resolve_uncached(make_city_location()) is None, case
        assert geo._resolve_uncached(make_department_location()) is None, case
        assert any("Résolution échouée" in m for m in log_messages)


# ---------------------------------------------------------------------------
# _seconds_since
# ---------------------------------------------------------------------------

NAIVE_NOW = datetime.datetime(2026, 8, 23, 12, 0, 0)


class TestSecondsSince:
    @freeze_time(FROZEN)
    def test_none_has_no_age(self):
        assert geo._seconds_since(None) is None

    @freeze_time(FROZEN)
    def test_a_naive_datetime_is_compared_against_utc_now(self):
        assert geo._seconds_since(NAIVE_NOW - datetime.timedelta(hours=2)) == 2 * 3600

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
# resolve_slug_id — la table de vérité du cache (même forme que SeLoger/bienici)
# ---------------------------------------------------------------------------

class TestResolveSlugIdCacheTruthTable:
    @staticmethod
    def spy_resolution(monkeypatch, result="rosny-sous-bois"):
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
        repo.get_cached.return_value = {"slug_id": "rosny-sous-bois", "resolved_at": None}

        assert geo.resolve_slug_id(make_city_location(insee="93066"), repo=repo) == "rosny-sous-bois"

        repo.get_cached.assert_called_once_with("93066")
        assert calls == []
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        "age_seconds", [0, 3600, 7 * 24 * 3600 - 1], ids=["a_l_instant", "une_heure", "une_seconde_avant_expiration"],
    )
    @freeze_time(FROZEN)
    def test_a_recent_failure_is_not_retried(self, repo, monkeypatch, age_seconds):
        """🔒 L'échec mémorisé (slug_id NULL) coupe toute nouvelle tentative
        avant 7 jours : sans ce délai, un périmètre introuvable martèlerait
        l'autocomplete à chaque cycle de scrape (toutes les 30 s)."""
        calls = self.spy_resolution(monkeypatch)
        resolved_at = NAIVE_NOW - datetime.timedelta(seconds=age_seconds)
        repo.get_cached.return_value = {"slug_id": None, "resolved_at": resolved_at}

        assert geo.resolve_slug_id(make_city_location(), repo=repo) is None

        assert calls == []
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        "age_seconds", [7 * 24 * 3600, 30 * 24 * 3600], ids=["sept_jours_pile", "un_mois"],
    )
    @freeze_time(FROZEN)
    def test_an_expired_failure_is_retried_and_rewritten(self, repo, monkeypatch, age_seconds):
        calls = self.spy_resolution(monkeypatch)
        resolved_at = NAIVE_NOW - datetime.timedelta(seconds=age_seconds)
        repo.get_cached.return_value = {"slug_id": None, "resolved_at": resolved_at}

        assert geo.resolve_slug_id(make_city_location(insee="93066"), repo=repo) == "rosny-sous-bois"

        assert len(calls) == 1
        repo.set_cached.assert_called_once_with("93066", "rosny-sous-bois")

    @freeze_time(FROZEN)
    def test_a_failure_without_a_resolution_date_is_retried(self, repo, monkeypatch):
        calls = self.spy_resolution(monkeypatch)
        repo.get_cached.return_value = {"slug_id": None, "resolved_at": None}

        assert geo.resolve_slug_id(make_city_location(), repo=repo) == "rosny-sous-bois"
        assert len(calls) == 1

    @freeze_time(FROZEN)
    def test_a_first_lookup_resolves_and_caches(self, repo, monkeypatch, log_messages):
        calls = self.spy_resolution(monkeypatch)
        location = make_city_location("Rosny-sous-Bois", "93110", "93066")

        assert geo.resolve_slug_id(location, repo=repo) == "rosny-sous-bois"

        assert calls == [location]
        repo.set_cached.assert_called_once_with("93066", "rosny-sous-bois")
        assert any("Rosny-sous-Bois (93110) -> rosny-sous-bois" in m for m in log_messages)

    @freeze_time(FROZEN)
    def test_a_failed_resolution_is_cached_as_none_with_a_warning(self, repo, monkeypatch, log_messages):
        self.spy_resolution(monkeypatch, result=None)

        assert geo.resolve_slug_id(make_city_location(insee="93066"), repo=repo) is None

        repo.set_cached.assert_called_once_with("93066", None)
        assert any("Aucun slug trouvé" in m for m in log_messages)

    @pytest.mark.parametrize(
        ("location", "expected_key"),
        [
            (make_city_location(insee="93066"), "93066"),
            (make_whole_city_location(insee="93000"), "city:93000"),
            (make_department_location(code="93"), "dept:93"),
            (make_region_location(code="11"), "region:11"),
        ],
        ids=["city", "whole_city", "department", "region"],
    )
    @freeze_time(FROZEN)
    def test_the_cache_is_read_and_written_with_the_area_key(self, repo, monkeypatch, location, expected_key):
        self.spy_resolution(monkeypatch)

        geo.resolve_slug_id(location, repo=repo)

        repo.get_cached.assert_called_once_with(expected_key)
        repo.set_cached.assert_called_once_with(expected_key, "rosny-sous-bois")


# ---------------------------------------------------------------------------
# remember_manual_slugs
# ---------------------------------------------------------------------------

class TestRememberManualSlugs:
    @staticmethod
    def criteria_with(slugs, locations):
        from tests.helpers.factories import make_criteria

        return make_criteria(locations=locations, sourceOverrides={"orpi": {"slugs": slugs}})

    def test_slugs_and_one_location_are_banked_together(self, repo):
        criteria = self.criteria_with(["rosny-sous-bois"], [make_city_location(insee="93066")])

        geo.remember_manual_slugs(criteria, repo=repo)

        repo.set_cached.assert_called_once_with("93066", "rosny-sous-bois")

    def test_an_existing_cache_entry_is_never_overwritten(self, repo):
        repo.get_cached.return_value = {"slug_id": "autre-slug", "resolved_at": None}
        criteria = self.criteria_with(["manuel"], [make_city_location(insee="93066")])

        geo.remember_manual_slugs(criteria, repo=repo)

        repo.get_cached.assert_called_once_with("93066")
        repo.set_cached.assert_not_called()

    @pytest.mark.parametrize(
        ("slugs", "locations", "case"),
        [
            ([], [make_city_location(insee="93066")], "aucun slug"),
            (
                ["rosny-sous-bois"],
                [
                    make_city_location(insee="93066"),
                    make_city_location(city="Montreuil", postal_code="93100", insee="93000"),
                ],
                "deux localisations",
            ),
        ],
        ids=["no_slug", "two_locations"],
    )
    def test_nothing_to_remember_is_a_silent_no_op(self, repo, slugs, locations, case):
        criteria = self.criteria_with(slugs, locations)

        geo.remember_manual_slugs(criteria, repo=repo)

        assert repo.set_cached.call_count == 0, case

    def test_a_location_without_insee_is_still_banked_under_its_postal_key(self, repo):
        criteria = self.criteria_with(
            ["rosny-sous-bois"], [{"kind": CITY, "city": "Rosny", "postalCode": "93110"}]
        )

        geo.remember_manual_slugs(criteria, repo=repo)

        repo.set_cached.assert_called_once_with("postal:93110", "rosny-sous-bois")

    def test_a_location_with_no_key_at_all_has_nothing_to_key_on(self, repo):
        criteria = self.criteria_with(["rosny-sous-bois"], [{"kind": CITY}])

        geo.remember_manual_slugs(criteria, repo=repo)

        repo.get_cached.assert_not_called()
        repo.set_cached.assert_not_called()

