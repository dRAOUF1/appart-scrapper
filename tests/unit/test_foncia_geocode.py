"""Tests unitaires de `services/foncia_geocode.py`.

Ce module traduit un périmètre canonique (voir core/criteria.py) en slug de
localité Foncia (`toulouse-31`, `rosny-sous-bois-93110`, `haute-garonne-31`,
`occitanie`) — même rôle que services/orpi_geocode.py, dont ce fichier de test
reprend la structure.

La différence de fond avec Orpi/Century 21 porte toute la logique : le slug
Foncia est DÉRIVABLE du périmètre (`{ville}-{cp}`, `{ville}-{dept}`,
`{nom}-{code}`, `{nom}`) mais la dérivation n'est jamais crue sur parole :
chaque slug candidat est vérifié contre l'API géo du site, qui renvoie
l'identifiant officiel attendu (codeInsee / codeDepartement / codeRegion).
Toute la matière de test vient donc de réponses géo telles qu'observées en
direct le 23/08/2026 (captures dans tests/fixtures/foncia/).

Aucun appel réseau : le socle bloque le transport HTTP (tests/conftest.py),
les tests doublent `_fetch_locality` / `_official_name`.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

from repositories.foncia_geo_repo import FonciaGeoRepository
from services import foncia_geocode as geo
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)

FROZEN = "2026-08-15 12:00:00"

# Réponses de l'API géo, telles qu'observées en direct le 23/08/2026.
TOULOUSE = {
    "reference": "ville_tout_toulouse",
    "slug": "toulouse-31",
    "type": "ville",
    "libelle": "Toulouse",
    "libelleDisplay": "Toulouse (31)",
    "codeInsee": "31555",
    "codePostal": ["31000", "31100", "31200", "31300", "31400", "31500"],
    "pluriDistribue": True,
}
ROSNY = {
    "reference": "ville_32275",
    "slug": "rosny-sous-bois-93110",
    "type": "ville",
    "libelle": "Rosny-sous-Bois",
    "libelleDisplay": "Rosny-sous-Bois (93110)",
    "codeInsee": "93064",
    "codePostal": ["93110"],
    "pluriDistribue": False,
}
PARIS_1ER = {  # un arrondissement parisien : INSEE 75101 attendu côté canonique
    "reference": "ville_33771",
    "slug": "paris-75001",
    "type": "ville",
    "libelle": "Paris 1er",
    "codeInsee": "75101",
    "codePostal": ["75001"],
    "pluriDistribue": False,
}
HAUTE_GARONNE = {
    "reference": "departement_31",
    "slug": "haute-garonne-31",
    "type": "departement",
    "libelle": "Haute-Garonne",
    "libelleDisplay": "Haute-Garonne (31)",
    "codeDepartement": "31",
}
CORSE_DU_SUD = {  # le code reste EN MAJUSCULES : les minuscules ne résolvent pas
    "reference": "departement_2A",
    "slug": "corse-du-sud-2A",
    "type": "departement",
    "libelle": "Corse-du-Sud",
    "codeDepartement": "2A",
}
OCCITANIE = {
    "reference": "region_76",
    "slug": "occitanie",
    "type": "region",
    "libelle": "Occitanie",
    "codeRegion": "76",
}


class TestHttpHelpers:
    def test_official_name_and_locality_success(self, requests_mock):
        official_url = "https://geo.api.gouv.fr/departements/31"
        requests_mock.get(official_url, json={"nom": "Haute-Garonne"})
        requests_mock.get(
            f"{geo.LOCALITY_BY_SLUG_URL}/toulouse-31",
            json={"items": [TOULOUSE]},
        )

        assert geo._official_name(official_url) == "Haute-Garonne"
        assert geo._fetch_locality(" toulouse-31 ") == TOULOUSE

    @pytest.mark.parametrize("status", [500, 404])
    def test_http_errors_degrade_to_none(self, requests_mock, status):
        official_url = "https://geo.api.gouv.fr/departements/99"
        requests_mock.get(official_url, status_code=status)
        requests_mock.get(f"{geo.LOCALITY_BY_SLUG_URL}/inconnue", status_code=status)

        assert geo._official_name(official_url) is None
        assert geo._fetch_locality("inconnue") is None

    @pytest.mark.parametrize("payload", [[], {}, {"items": None}, {"items": ["invalide"]}])
    def test_unexpected_locality_payload_is_empty(self, requests_mock, payload):
        requests_mock.get(f"{geo.LOCALITY_BY_SLUG_URL}/x", json=payload)
        assert geo._fetch_locality("x") is None

    def test_empty_slug_never_calls_http(self, no_network):
        assert geo._fetch_locality(" ") is None


@pytest.fixture
def repo():
    """Le cache persistant, doublé. `spec=` interdit d'appeler une méthode que
    le vrai repository n'a pas."""
    double = MagicMock(spec=FonciaGeoRepository)
    double.get_cached.return_value = None
    return double


@pytest.fixture
def localities(monkeypatch):
    """Remplace `_fetch_locality` par une table slug -> réponse programmée.

    `install(mapping)` renvoie aussi la liste des slugs demandés, pour
    vérifier ce qui a été dérivé."""

    def install(mapping: dict[str, dict | None]):
        queried: list[str] = []

        def fake(slug: str):
            queried.append(slug)
            return mapping.get(slug)

        monkeypatch.setattr(geo, "_fetch_locality", fake)
        return queried

    return install


class TestSlugDerivation:
    def test_the_slugification_matches_the_site_own_format(self):
        """« L'Haÿ-les-Roses » -> `l-hay-les-roses`, forme vérifiée contre
        l'API du site (la variante sans tiret du « l' » ne résout pas)."""
        assert geo._slugify("L'Haÿ-les-Roses") == "l-hay-les-roses"
        assert geo._slugify("Île-de-France") == "ile-de-france"
        assert geo._slugify("Corse-du-Sud") == "corse-du-sud"

    def test_the_department_code_comes_from_the_insee_prefix(self):
        assert geo._department_code_of_insee("31555") == "31"
        assert geo._department_code_of_insee("2A004") == "2A"  # Corse, tel quel
        assert geo._department_code_of_insee("97123") == "971"  # outre-mer

    @pytest.mark.parametrize(
        ("location", "key"),
        [
            (make_city_location(), "75113"),
            (make_whole_city_location(), "city:86194"),
            (make_department_location(), "dept:33"),
            (make_region_location(), "region:75"),
        ],
        ids=["commune", "ville-entiere", "departement", "region"],
    )
    def test_area_cache_key_uses_the_shared_convention(self, location, key):
        assert geo.area_cache_key(location) == key

    def test_an_unidentifiable_scope_has_no_cache_key(self):
        assert geo.area_cache_key({"kind": "city", "city": "X"}) is None


class TestCityResolution:
    def test_a_commune_resolves_from_name_and_postal_code(self, localities):
        queried = localities({"rosny-sous-bois-93110": ROSNY})
        location = make_city_location(
            city="Rosny-sous-Bois", postal_code="93110", insee="93064"
        )

        assert geo._resolve_city(location) == "rosny-sous-bois-93110"
        assert queried == ["rosny-sous-bois-93110"]

    def test_an_arrondissement_matches_on_its_own_insee(self, localities):
        """Paris 1er : le canonique porte l'INSEE d'arrondissement (75101,
        cf. core.geocode._arrondissement_insee_code) et Foncia aussi — la
        dérivation par code postal tombe juste."""
        localities({"paris-75001": PARIS_1ER})
        location = make_city_location(city="Paris", postal_code="75001", insee="75101")

        assert geo._resolve_city(location) == "paris-75001"

    def test_a_wrong_insee_is_refused_even_if_the_slug_exists(self, localities):
        """Un slug dérivé qui répond une AUTRE commune n'est jamais accepté :
        le contrôle INSEE échoue fermé plutôt que de risquer un périmètre
        silencieusement faux."""
        other = {**ROSNY, "slug": "vannes-56000", "libelle": "Vannes"}
        localities({"vannes-56000": other})
        location = make_city_location(city="Vannes", postal_code="56000", insee="99999")

        assert geo._resolve_city(location) is None

    def test_a_fused_commune_is_accepted_on_postal_code_and_libelle_agreement(
        self, localities
    ):
        """Fusion de communes : l'INSEE du site peut diverger de celui de
        geo.api.gouv.fr. L'accord code postal + libellé tranche alors — deux
        indices concordants valent mieux qu'un refus systématique."""
        fused = {**ROSNY, "codeInsee": "93099"}  # INSEE site != attendu 93064
        localities({"rosny-sous-bois-93110": fused})
        location = make_city_location(
            city="Rosny-sous-Bois", postal_code="93110", insee="93064"
        )

        assert geo._resolve_city(location) == "rosny-sous-bois-93110"

    def test_no_postal_code_means_nothing_to_derive(self, localities):
        queried = localities({})

        assert geo._resolve_city(make_city_location(postal_code="", insee="")) is None
        assert queried == []


class TestWholeCityResolution:
    def test_a_whole_city_resolves_on_its_department_entry(self, localities):
        """`{ville}-{département}` : l'entrée pluriDistribue (« tout Toulouse »),
        pas une liste d'arrondissements ni un seul code postal."""
        queried = localities({"toulouse-31": TOULOUSE})
        location = make_whole_city_location(
            city="Toulouse", postal_codes=("31000", "31200"), insee="31555"
        )

        assert geo._resolve_whole_city(location) == "toulouse-31"
        assert queried == ["toulouse-31"]

    def test_a_mismatched_locality_is_refused(self, localities):
        localities({"lyon-69": None})
        location = make_whole_city_location(city="Lyon", insee="69123")

        assert geo._resolve_whole_city(location) is None


class TestDepartmentResolution:
    def test_a_department_resolves_from_its_name_and_code(self, localities):
        localities({"haute-garonne-31": HAUTE_GARONNE})
        location = make_department_location(code="31", name="Haute-Garonne")

        assert geo._resolve_department(location) == "haute-garonne-31"

    def test_the_official_name_is_fetched_when_the_scope_has_none(self, localities, monkeypatch):
        """Un département réduit à son code reste résoluble : le nom officiel
        vient de geo.api.gouv.fr."""
        localities({"gironde-33": {**HAUTE_GARONNE, "slug": "gironde-33",
                                   "libelle": "Gironde", "codeDepartement": "33"}})
        monkeypatch.setattr(geo, "_official_name", lambda url: "Gironde")
        location = make_department_location(code="33", name="")

        assert geo._resolve_department(location) == "gironde-33"

    def test_corse_departments_keep_their_uppercase_code(self, localities):
        """Les slugs corses ne résolvent QU'EN MAJUSCULES
        (`corse-du-sud-2a` rend une réponse vide, vérifié en direct) : le
        code canonique part tel quel."""
        queried = localities({"corse-du-sud-2A": CORSE_DU_SUD})
        location = make_department_location(code="2A", name="Corse-du-Sud")

        assert geo._resolve_department(location) == "corse-du-sud-2A"
        assert queried == ["corse-du-sud-2A"]

    def test_a_wrong_department_code_is_refused(self, localities):
        localities({"gironde-33": HAUTE_GARONNE})  # répond le 31 pour un slug du 33
        location = make_department_location(code="33", name="Gironde")

        assert geo._resolve_department(location) is None


class TestRegionResolution:
    def test_a_region_resolves_from_its_name(self, localities):
        localities({"occitanie": OCCITANIE})
        location = make_region_location(code="76", name="Occitanie")

        assert geo._resolve_region(location) == "occitanie"

    def test_the_official_name_is_fetched_when_the_scope_has_none(self, localities, monkeypatch):
        localities({"nouvelle-aquitaine": {
            **OCCITANIE, "slug": "nouvelle-aquitaine", "libelle": "Nouvelle-Aquitaine",
            "codeRegion": "75",
        }})
        monkeypatch.setattr(geo, "_official_name", lambda url: "Nouvelle-Aquitaine")
        location = make_region_location(code="75", name="")

        assert geo._resolve_region(location) == "nouvelle-aquitaine"


class TestResolveSlugIdCache:
    def test_a_cached_slug_short_circuits_the_api(self, repo, localities):
        queried = localities({})
        repo.get_cached.return_value = {"area_key": "31555", "slug_id": "toulouse-31",
                                        "resolved_at": None}

        location = make_whole_city_location(city="Toulouse", insee="31555")
        assert geo.resolve_slug_id(location, repo) == "toulouse-31"
        assert queried == []

    @freeze_time(FROZEN)
    def test_a_recent_failure_is_not_retried(self, repo, localities):
        """Échec mémorisé il y a moins de 7 jours : on ne remarte pas l'API."""
        queried = localities({})
        age = datetime.timedelta(days=1)
        repo.get_cached.return_value = {
            "area_key": "dept:999", "slug_id": None,
            "resolved_at": datetime.datetime(2026, 8, 14, 12, 0, 0) - age,
        }

        location = make_department_location(code="999", name="Nulle-part")
        assert geo.resolve_slug_id(location, repo) is None
        assert queried == []

    @freeze_time(FROZEN)
    def test_an_old_failure_is_retried_and_remembered(self, repo, localities):
        queried = localities({"gironde-33": {**HAUTE_GARONNE, "slug": "gironde-33",
                                      "libelle": "Gironde", "codeDepartement": "33"}})
        repo.get_cached.return_value = {
            "area_key": "dept:33", "slug_id": None,
            "resolved_at": datetime.datetime(2026, 8, 1),
        }

        location = make_department_location()
        assert geo.resolve_slug_id(location, repo) == "gironde-33"
        assert queried == ["gironde-33"]
        repo.set_cached.assert_called_once_with("dept:33", "gironde-33")


class TestRememberManualSlugs:
    def test_manual_slugs_are_banked_for_a_single_scope(self, repo):
        repo.get_cached.return_value = None
        criteria = {
            "locations": [make_city_location(city="Vannes", postal_code="56000",
                                             insee="56260")],
            "sourceOverrides": {"foncia": {"slugs": ["vannes-56000"]}},
        }

        geo.remember_manual_slugs(criteria, repo)

        repo.set_cached.assert_called_once_with("56260", "vannes-56000")

    def test_manual_slugs_are_not_banked_for_several_scopes(self, repo):
        """Plusieurs périmètres : impossible de savoir quel slug va avec
        quelle ville — on ne banque rien plutôt qu'une association fausse."""
        criteria = {"locations": [make_city_location(), make_department_location()]}

        geo.remember_manual_slugs(criteria, repo)

        repo.set_cached.assert_not_called()


class TestFetchLocality:
    def test_the_items_envelope_is_unwrapped(self, requests_mock):
        """La réponse est toujours enveloppée ({items: [...]}, formes ville et
        département/région vérifiées identiques en direct)."""
        requests_mock.get(
            f"{geo.LOCALITY_BY_SLUG_URL}/toulouse-31",
            json={"items": [TOULOUSE], "total": 1, "count": 1},
        )

        item = geo._fetch_locality("toulouse-31")

        assert item == TOULOUSE
        request = requests_mock.request_history[0]
        assert "Mozilla/5.0" in request.headers["User-Agent"]

    def test_an_empty_or_broken_response_yields_none(self, requests_mock):
        requests_mock.get(f"{geo.LOCALITY_BY_SLUG_URL}/nulle-part-000", json={"items": []})
        requests_mock.get(f"{geo.LOCALITY_BY_SLUG_URL}/casse-000", status_code=500)

        assert geo._fetch_locality("nulle-part-000") is None
        assert geo._fetch_locality("casse-000") is None
