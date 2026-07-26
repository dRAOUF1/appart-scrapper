"""Tests de `core/geocode.py` — la traduction « texte saisi -> périmètre ».

Ce module est le seul point du dépôt qui parle à geo.api.gouv.fr, et tout ce
qui en sort alimente ensuite les deux sources. Trois familles de comportements
sont figées ici :

* les **formules pures** (codes d'arrondissement, préfixes de code postal) :
  aucune n'est déductible, chacune a été vérifiée contre des valeurs réelles et
  une régression y serait invisible (un code INSEE faux renvoie des annonces
  d'un autre quartier, pas une erreur) ;
* la **politique de cache**, qui n'est pas la même selon la fonction :
  `resolve_insee_code` et `department_main_city` mémorisent leurs échecs (donc
  ne réessaient jamais), `region_departments` ne mémorise que ses succès (donc
  réessaie). Chaque test compte les requêtes réellement émises ;
* la **tolérance aux pannes** de l'autocomplete : chaque niveau est interrogé
  dans son propre try/except pour qu'une API partiellement indisponible
  n'efface pas les suggestions des autres niveaux.

Le réseau est coupé par le socle (voir tests/conftest.py) et les trois caches
de module sont vidés entre chaque test : aucun reset manuel ici. C'est ce qui
manquait à l'ancienne suite, où deux tests vidaient `_REGION_DEPARTMENTS_CACHE`
à la main et laissaient fuiter `"94" -> ["2A", "2B"]` dans tout le reste du run.
"""

from __future__ import annotations

import pytest
import requests

from core.geocode import (
    CITY,
    COMMUNES_API,
    DEPARTEMENTS_API,
    DEPARTMENT,
    LOCATION_KINDS,
    REGION,
    REGIONS_API,
    WHOLE_CITY,
    _arrondissement_insee_code,
    department_main_city,
    postal_prefix,
    region_departments,
    resolve_insee_code,
    search_locations,
)

# ---------------------------------------------------------------------------
# Outils de simulation de geo.api.gouv.fr
# ---------------------------------------------------------------------------


def register_geo(requests_mock, *, regions=None, departments=None, communes=None, region_departments=None):
    """Branche les trois niveaux de l'API géo sur des réponses fixes.

    Les trois sont TOUJOURS enregistrés, même vides : `search_locations` les
    interroge tous, et laisser un endpoint sans réponse simulée le ferait
    échouer par `NoMockAddress` — une panne réseau accidentelle, indiscernable
    d'un « aucun résultat » puisque le module attrape tout.

    `region_departments` mappe un code de région vers ses codes de département,
    pour le sous-appel `/regions/<code>/departements`.
    """
    requests_mock.get(REGIONS_API, json=[] if regions is None else regions)
    requests_mock.get(DEPARTEMENTS_API, json=[] if departments is None else departments)
    requests_mock.get(COMMUNES_API, json=[] if communes is None else communes)
    for region_code, codes in (region_departments or {}).items():
        requests_mock.get(f"{REGIONS_API}/{region_code}/departements", json=[{"code": c} for c in codes])


def commune(nom: str, code: str, postal_codes, lon: float = 0.0, lat: float = 0.0, **extra) -> dict:
    """Une commune telle que la renvoie `/communes` (coordonnées en GeoJSON)."""
    return {
        "nom": nom,
        "code": code,
        "codesPostaux": list(postal_codes),
        "centre": {"type": "Point", "coordinates": [lon, lat]},
        **extra,
    }


def kinds(suggestions: list[dict]) -> list[str]:
    return [s["kind"] for s in suggestions]


# ===========================================================================
# _arrondissement_insee_code — formules pures
# ===========================================================================


@pytest.mark.parametrize(
    ("postal_code", "expected"),
    [
        # --- Paris : 75xxx -> 751<arr sur 2 chiffres> ---
        pytest.param("75001", "75101", id="paris-1er"),
        pytest.param("75009", "75109", id="paris-9e"),
        pytest.param("75014", "75114", id="paris-14e-valeur-verifiee-chez-laforet"),
        pytest.param("75020", "75120", id="paris-20e-dernier-arrondissement"),
        # 75116 est le second code postal du 16e (l'ouest de l'arrondissement).
        # La formule le renvoie inchangé — et c'est le bon code INSEE du 16e,
        # qui est justement 75116. Coïncidence utile, pas cas particulier.
        pytest.param("75116", "75116", id="paris-16e-second-code-postal"),
        # --- Lyon : 690x -> 693<80 + arr> ---
        pytest.param("69001", "69381", id="lyon-1er"),
        pytest.param("69007", "69387", id="lyon-7e-valeur-verifiee-chez-laforet"),
        pytest.param("69009", "69389", id="lyon-9e-dernier-arrondissement"),
        # --- Marseille : 130xx -> 132<arr sur 2 chiffres> ---
        pytest.param("13001", "13201", id="marseille-1er-valeur-verifiee-chez-laforet"),
        pytest.param("13008", "13208", id="marseille-8e"),
        pytest.param("13016", "13216", id="marseille-16e-dernier-arrondissement"),
    ],
)
def test_the_three_arrondissement_formulas_produce_the_verified_insee_codes(postal_code, expected):
    """Aucune de ces trois formules n'est devinable : elles ont été relevées
    dans l'état de page embarqué de Laforêt pour plusieurs arrondissements de
    chaque ville (voir le docstring de la fonction). Une régression ici ne lève
    rien — elle renvoie les annonces d'un autre arrondissement."""
    assert _arrondissement_insee_code(postal_code) == expected


@pytest.mark.parametrize(
    "postal_code",
    [
        pytest.param("7501", id="quatre-chiffres"),
        pytest.param("750144", id="six-chiffres"),
        pytest.param("", id="chaine-vide"),
        pytest.param("abcde", id="cinq-lettres"),
        pytest.param("7501a", id="quatre-chiffres-et-une-lettre"),
        pytest.param("75 14", id="espace-au-milieu"),
        pytest.param("75.14", id="point-au-milieu"),
        pytest.param("-7501", id="signe-moins"),
        pytest.param(" 75014", id="espace-de-tete-la-longueur-passe-mais-pas-isdigit"),
    ],
)
def test_anything_that_is_not_five_digits_resolves_to_no_arrondissement(postal_code):
    """Le garde-fou `len == 5 and isdigit` est ce qui permet d'appeler la
    fonction sur n'importe quelle saisie utilisateur avant tout appel réseau."""
    assert _arrondissement_insee_code(postal_code) is None


@pytest.mark.parametrize(
    "postal_code",
    [
        pytest.param("44000", id="nantes-ville-ordinaire"),
        pytest.param("86000", id="poitiers-ville-ordinaire"),
        pytest.param("33000", id="bordeaux-plusieurs-codes-postaux-mais-pas-d-arrondissements"),
        pytest.param("13100", id="aix-en-provence-prefixe-131-pas-130"),
        pytest.param("69100", id="villeurbanne-prefixe-691-pas-690"),
        pytest.param("20000", id="ajaccio"),
    ],
)
def test_a_city_without_arrondissements_has_no_special_insee_code(postal_code):
    """Seules Paris, Lyon et Marseille ont des « communes associées » par
    arrondissement : partout ailleurs le code INSEE de la commune suffit et doit
    être résolu par l'API."""
    assert _arrondissement_insee_code(postal_code) is None


@pytest.mark.parametrize(
    "postal_code",
    [
        pytest.param("75000", id="paris-arrondissement-0"),
        pytest.param("75021", id="paris-arrondissement-21-inexistant"),
        pytest.param("75056", id="paris-code-insee-de-la-commune-passe-par-erreur"),
        pytest.param("69000", id="lyon-arrondissement-0"),
        pytest.param("69010", id="lyon-dernier-chiffre-0-arrondissement-10-inexistant"),
        pytest.param("13000", id="marseille-arrondissement-0"),
        pytest.param("13017", id="marseille-arrondissement-17-inexistant"),
        pytest.param("13055", id="marseille-code-insee-de-la-commune-passe-par-erreur"),
    ],
)
def test_an_arrondissement_number_outside_the_real_range_resolves_to_none(postal_code):
    """Les bornes 1..20 / 1..9 / 1..16 sont les arrondissements qui existent
    réellement. Hors bornes, mieux vaut aucun code qu'un code inventé : le
    repli est la résolution par l'API.

    Les codes INSEE des communes agrégées (75056, 13055) sont inclus parce que
    `_commune_suggestions` les manipule à côté des codes postaux — les confondre
    est l'erreur que ce module existe pour éviter.
    """
    assert _arrondissement_insee_code(postal_code) is None


def test_a_unicode_digit_that_int_cannot_parse_raises_instead_of_returning_none():
    """# BUG : `str.isdigit()` est vrai pour les exposants Unicode ("²") alors
    que `int()` les refuse. Le garde-fou laisse donc passer "7501²", et la ligne
    `int(postal_code[-2:])` lève une `ValueError` là où le contrat de la
    fonction (`-> str | None`) promet un `None`.

    Remonté jusqu'à `resolve_insee_code`, qui n'entoure PAS cet appel d'un
    try/except (contrairement à `_lookup_insee_code`) : un code postal
    fantaisiste en base ferait échouer le scrape entier au lieu d'être ignoré.

    Le correctif serait `postal_code.isascii() and postal_code.isdigit()`.
    Comportement ACTUEL figé ci-dessous.
    """
    assert "7501²".isdigit(), "prémisse du bug : isdigit() accepte les exposants Unicode"

    with pytest.raises(ValueError, match="invalid literal for int"):
        _arrondissement_insee_code("7501²")


# ===========================================================================
# postal_prefix
# ===========================================================================


@pytest.mark.parametrize(
    ("department_code", "expected"),
    [
        pytest.param("33", "33", id="gironde-metropole"),
        pytest.param("75", "75", id="paris"),
        pytest.param("01", "01", id="ain-zero-de-tete-conserve"),
        pytest.param("971", "971", id="guadeloupe-outre-mer-trois-chiffres"),
        pytest.param("974", "974", id="la-reunion"),
    ],
)
def test_an_ordinary_department_code_is_its_own_postal_prefix(department_code, expected):
    """`33` -> `33xxx`, `971` -> `971xx` : la déduction est directe partout sauf
    en Corse."""
    assert postal_prefix(department_code) == expected


@pytest.mark.parametrize(
    "department_code",
    ["2A", "2B", "2a", "2b"],
)
def test_both_corsican_departments_share_the_postal_prefix_20(department_code):
    """Vérifié via l'API : les 59 codes postaux de 2A et les 51 de 2B commencent
    tous par « 20 », aucun par « 2A »/« 2B ». Sans cette surcharge, le contrôle
    local de périmètre (`matches_locations`) rejetterait TOUTES les annonces
    corses — le préfixe « 2A » ne correspondrait à aucun code postal réel.

    La casse est normalisée pour que le code écrit en minuscules fonctionne.
    """
    assert postal_prefix(department_code) == "20"


def test_a_code_that_is_not_an_override_is_returned_without_being_uppercased():
    """L'`upper()` ne sert qu'à la recherche dans la table de surcharges : hors
    surcharge, le code est renvoyé exactement tel qu'il est entré. Aucun code de
    département réel n'est concerné, mais le fige évite de croire à une
    normalisation générale de la casse."""
    assert postal_prefix("2c") == "2c"


# ===========================================================================
# resolve_insee_code / _lookup_insee_code
# ===========================================================================


@pytest.mark.parametrize(
    ("postal_code", "expected"),
    [
        pytest.param("75014", "75114", id="paris-14e"),
        pytest.param("69007", "69387", id="lyon-7e"),
        pytest.param("13001", "13201", id="marseille-1er"),
    ],
)
def test_an_arrondissement_is_resolved_locally_without_touching_the_network(
    requests_mock, postal_code, expected
):
    """Trois villes, 45 arrondissements : les résoudre par l'API serait 45
    requêtes pour un résultat que l'API ne sait de toute façon pas donner (elle
    ne connaît que 75056/69123/13055)."""
    register_geo(requests_mock)

    assert resolve_insee_code(postal_code) == expected
    assert requests_mock.call_count == 0


def test_any_other_postal_code_is_resolved_through_the_public_geo_api(requests_mock):
    register_geo(requests_mock, communes=[{"code": "86194"}])

    assert resolve_insee_code("86000") == "86194"
    assert requests_mock.call_count == 1
    assert requests_mock.last_request.qs == {"codepostal": ["86000"], "fields": ["code"]}


def test_only_the_first_matching_commune_is_kept(requests_mock):
    """Un code postal peut couvrir plusieurs communes (code postal partagé) :
    la première réponse est retenue, l'API les classant par pertinence."""
    register_geo(requests_mock, communes=[{"code": "86194"}, {"code": "86999"}])

    assert resolve_insee_code("86000") == "86194"


def test_a_resolved_code_is_memoized_for_the_life_of_the_process(requests_mock):
    """Une recherche scrapée toutes les 5 minutes résoudrait sinon les mêmes
    codes postaux indéfiniment — les codes INSEE ne changent jamais."""
    register_geo(requests_mock, communes=[{"code": "86194"}])

    assert [resolve_insee_code("86000") for _ in range(4)] == ["86194"] * 4
    assert requests_mock.call_count == 1


@pytest.mark.parametrize(
    ("kwargs", "description"),
    [
        pytest.param({"json": []}, "aucune commune ne correspond", id="liste-vide"),
        pytest.param({"json": [{}]}, "la commune n'a pas de champ code", id="commune-sans-code"),
        pytest.param({"json": {"code": "86194"}}, "l'API renvoie un objet au lieu d'une liste", id="json-non-liste"),
        pytest.param({"status_code": 500}, "l'API est en panne", id="http-500"),
        pytest.param({"status_code": 404}, "l'endpoint a changé", id="http-404"),
        pytest.param({"text": "<html>maintenance</html>"}, "réponse non JSON", id="corps-non-json"),
        pytest.param({"exc": requests.ConnectionError}, "réseau injoignable", id="erreur-de-connexion"),
        pytest.param({"exc": requests.Timeout}, "API trop lente", id="timeout"),
    ],
)
def test_every_failure_mode_yields_none_rather_than_propagating(requests_mock, kwargs, description):
    """`except Exception` volontairement large : l'appelant sait se passer d'un
    code INSEE (voir le docstring), alors qu'une exception ferait tomber tout le
    scrape."""
    requests_mock.get(COMMUNES_API, **kwargs)

    assert resolve_insee_code("99999") is None, description


def test_a_failed_resolution_is_memoized_too_so_it_is_never_retried(requests_mock):
    """`if postal_code not in _INSEE_CACHE` (et non `if not _INSEE_CACHE.get(...)`) :
    les `None` sont mémorisés. Un code postal étranger ou inexistant dans les
    critères d'une recherche ne provoque donc qu'UNE requête pour toute la vie
    du process — au prix de ne jamais rattraper une panne temporaire de l'API.
    """
    register_geo(requests_mock, communes=[])

    assert resolve_insee_code("99999") is None
    assert requests_mock.call_count == 1

    assert resolve_insee_code("99999") is None
    assert requests_mock.call_count == 1, "un second appel ne doit émettre aucune requête"


def test_two_different_postal_codes_are_cached_independently(requests_mock):
    requests_mock.get(COMMUNES_API, json=[{"code": "86194"}])
    assert resolve_insee_code("86000") == "86194"

    requests_mock.get(COMMUNES_API, json=[{"code": "44109"}])
    assert resolve_insee_code("44000") == "44109"
    assert resolve_insee_code("86000") == "86194"

    assert requests_mock.call_count == 2


# ===========================================================================
# department_main_city
# ===========================================================================

_GIRONDE_COMMUNES = [
    # Ordre alphabétique : c'est ce que renvoie /communes sans `boost`.
    {"nom": "Abzac", "codesPostaux": ["33230"], "population": 2000},
    {"nom": "Bordeaux", "codesPostaux": ["33800", "33000", "33300"], "population": 261804},
    {"nom": "Mérignac", "codesPostaux": ["33700"], "population": 71152},
]


def test_the_most_populated_city_is_chosen_and_not_the_first_one_returned(requests_mock):
    """Le tri est fait CÔTÉ CLIENT parce que le `boost=population` de l'API ne
    s'applique qu'à une recherche par nom : sans ce `max`, la Gironde
    renverrait « Abzac » (premier par ordre alphabétique) et l'URL Laforêt
    construite autour serait absurde à lire."""
    register_geo(requests_mock, communes=_GIRONDE_COMMUNES)

    assert department_main_city("33") == {"city": "Bordeaux", "postalCode": "33000"}


def test_the_lowest_postal_code_is_picked_when_the_city_has_several(requests_mock):
    """`sorted(codesPostaux)[0]` : Bordeaux couvre 33000/33300/33800 et l'API ne
    les renvoie pas triés. Un choix stable est nécessaire — l'URL construite ne
    doit pas changer d'un scrape à l'autre."""
    register_geo(requests_mock, communes=_GIRONDE_COMMUNES)

    assert department_main_city("33")["postalCode"] == "33000"


def test_the_query_asks_for_the_department_and_only_the_three_useful_fields(requests_mock):
    """Un département peut compter 500 communes : demander tous les champs
    multiplierait le volume transféré pour rien."""
    register_geo(requests_mock, communes=_GIRONDE_COMMUNES)

    department_main_city("33")

    assert requests_mock.last_request.qs == {
        "codedepartement": ["33"],
        "fields": ["nom,codespostaux,population"],
    }


@pytest.mark.parametrize(
    ("unusable", "reason"),
    [
        pytest.param({"nom": "Sans pop"}, "ni population ni codes postaux", id="ni-l-un-ni-l-autre"),
        pytest.param({"nom": "Sans pop", "codesPostaux": ["33999"]}, "population absente", id="population-absente"),
        pytest.param(
            {"nom": "Pop nulle", "codesPostaux": ["33999"], "population": 0},
            "population à 0",
            id="population-zero",
        ),
        pytest.param(
            {"nom": "Pop inconnue", "codesPostaux": ["33999"], "population": None},
            "population None",
            id="population-none",
        ),
        pytest.param({"nom": "Sans CP", "population": 999999}, "codes postaux absents", id="codes-postaux-absents"),
        pytest.param(
            {"nom": "CP vides", "codesPostaux": [], "population": 999999},
            "liste de codes postaux vide",
            id="codes-postaux-liste-vide",
        ),
    ],
)
def test_a_commune_missing_population_or_postal_codes_is_never_chosen(requests_mock, unusable, reason):
    """Le filtre `population and codesPostaux` protège les deux accès qui
    suivent : sans lui, `max(key=...)` planterait sur une population `None` et
    `sorted(codesPostaux)[0]` sur une liste vide. Les communes fictives à
    population énorme ci-dessus vérifient que le filtre passe AVANT le tri."""
    register_geo(requests_mock, communes=[unusable, {"nom": "Bordeaux", "codesPostaux": ["33000"], "population": 100}])

    assert department_main_city("33") == {"city": "Bordeaux", "postalCode": "33000"}, reason


@pytest.mark.parametrize(
    ("communes", "description"),
    [
        pytest.param([], "département vide ou inconnu", id="aucune-commune"),
        pytest.param([{"nom": "X"}], "aucune commune exploitable", id="communes-inexploitables"),
        pytest.param({"erreur": "not found"}, "l'API renvoie un objet, pas une liste", id="json-non-liste"),
    ],
)
def test_no_usable_commune_yields_none(requests_mock, communes, description):
    register_geo(requests_mock, communes=communes)

    assert department_main_city("99") is None, description


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"status_code": 500}, id="http-500"),
        pytest.param({"exc": requests.ConnectionError}, id="erreur-de-connexion"),
        pytest.param({"text": "pas du json"}, id="corps-non-json"),
    ],
)
def test_an_api_failure_yields_none_without_propagating(requests_mock, kwargs):
    """L'appelant (parsers/laforet.py) sait se passer de la ville principale :
    le chemin d'URL n'est que cosmétique quand `filter[departments][]` est là."""
    requests_mock.get(COMMUNES_API, **kwargs)

    assert department_main_city("33") is None


def test_the_main_city_is_memoized_after_a_success(requests_mock):
    register_geo(requests_mock, communes=_GIRONDE_COMMUNES)

    first = department_main_city("33")
    second = department_main_city("33")

    assert first == second == {"city": "Bordeaux", "postalCode": "33000"}
    assert requests_mock.call_count == 1


def test_a_failure_is_memoized_too_so_the_api_is_not_hammered(requests_mock):
    """`if department_code in _DEPARTMENT_MAIN_CITY_CACHE` teste la présence de
    la clé, pas la vérité de la valeur : un `None` est donc définitif. Choix
    assumé (une région de 12 départements ne doit pas produire 12 requêtes par
    scrape) mais il faut redémarrer le process pour rattraper une panne."""
    requests_mock.get(COMMUNES_API, status_code=500)

    assert department_main_city("33") is None
    assert requests_mock.call_count == 1

    assert department_main_city("33") is None
    assert requests_mock.call_count == 1, "l'échec mémorisé ne doit pas être réessayé"


def test_two_departments_are_cached_independently(requests_mock):
    requests_mock.get(COMMUNES_API, json=[{"nom": "Bordeaux", "codesPostaux": ["33000"], "population": 261804}])
    assert department_main_city("33")["city"] == "Bordeaux"

    requests_mock.get(COMMUNES_API, json=[{"nom": "Poitiers", "codesPostaux": ["86000"], "population": 88291}])
    assert department_main_city("86")["city"] == "Poitiers"
    assert department_main_city("33")["city"] == "Bordeaux"

    assert requests_mock.call_count == 2


# ===========================================================================
# region_departments
# ===========================================================================


def test_the_department_codes_of_a_region_are_returned_in_the_api_order(requests_mock):
    """Sert à traduire une recherche régionale pour une source qui ne connaît
    que les départements (Laforêt et son `filter[departments][]`)."""
    register_geo(requests_mock, region_departments={"11": ["75", "77", "78", "91", "92", "93", "94", "95"]})

    assert region_departments("11") == ["75", "77", "78", "91", "92", "93", "94", "95"]


def test_corsica_keeps_its_two_letter_department_codes(requests_mock):
    """Les codes ne sont pas normalisés en nombres : « 2A » et « 2B » doivent
    survivre intacts jusqu'au filtre de la source."""
    register_geo(requests_mock, region_departments={"94": ["2A", "2B"]})

    assert region_departments("94") == ["2A", "2B"]


def test_a_successful_lookup_is_memoized(requests_mock):
    register_geo(requests_mock, region_departments={"75": ["33", "40"]})

    assert region_departments("75") == ["33", "40"]
    assert region_departments("75") == ["33", "40"]
    assert requests_mock.call_count == 1


@pytest.mark.parametrize(
    ("kwargs", "description"),
    [
        pytest.param({"status_code": 500}, "API en panne", id="http-500"),
        pytest.param({"status_code": 404}, "région inconnue", id="http-404"),
        pytest.param({"exc": requests.ConnectionError}, "réseau injoignable", id="erreur-de-connexion"),
        pytest.param({"text": "pas du json"}, "corps non JSON", id="corps-non-json"),
        pytest.param({"json": [{"nom": "Gironde"}]}, "départements sans code", id="departements-sans-code"),
        pytest.param({"json": {"erreur": "boom"}}, "objet au lieu d'une liste", id="json-non-liste"),
    ],
)
def test_a_failed_lookup_returns_an_empty_list(requests_mock, kwargs, description):
    requests_mock.get(f"{REGIONS_API}/11/departements", **kwargs)

    assert region_departments("11") == [], description


def test_a_failure_is_not_memoized_so_the_next_call_retries(requests_mock):
    """`if codes:` — seuls les résultats NON VIDES entrent en cache. C'est la
    politique inverse de `resolve_insee_code` et de `department_main_city`, et
    c'est celle qu'il faut ici : les départements d'une région sont indispensables
    au scrape régional (sans eux Laforêt ne filtre rien), une panne passagère ne
    doit donc pas condamner la recherche pour toute la vie du process."""
    requests_mock.get(f"{REGIONS_API}/11/departements", status_code=500)

    assert region_departments("11") == []
    assert requests_mock.call_count == 1

    assert region_departments("11") == []
    assert requests_mock.call_count == 2, "un échec doit être réessayé"


def test_a_retry_after_a_failure_picks_up_the_recovered_api(requests_mock):
    """Corollaire concret du test précédent : la panne réparée est vue."""
    requests_mock.get(f"{REGIONS_API}/11/departements", status_code=503)
    assert region_departments("11") == []

    requests_mock.get(f"{REGIONS_API}/11/departements", json=[{"code": "75"}, {"code": "92"}])
    assert region_departments("11") == ["75", "92"]


def test_an_empty_region_is_retried_as_well(requests_mock):
    """Une région sans département est indiscernable d'un échec pour le cache
    (`codes` vaut `[]` dans les deux cas) : elle est donc réinterrogée."""
    register_geo(requests_mock, region_departments={"11": []})

    assert region_departments("11") == []
    assert region_departments("11") == []
    assert requests_mock.call_count == 2


# ===========================================================================
# search_locations — garde-fou d'entrée
# ===========================================================================


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("", id="chaine-vide"),
        pytest.param(" ", id="un-espace"),
        pytest.param("   ", id="plusieurs-espaces"),
        pytest.param("p", id="une-lettre"),
        pytest.param(" p ", id="une-lettre-entouree-d-espaces"),
        pytest.param("\t\n", id="espaces-invisibles"),
        pytest.param("é", id="une-lettre-accentuee"),
    ],
)
def test_a_query_shorter_than_two_characters_returns_nothing_without_any_request(requests_mock, query):
    """Ce garde-fou est appelé à chaque frappe de l'autocomplete : sans lui, la
    première lettre tapée déclencherait trois requêtes vers une API publique
    pour un résultat inutilisable (toutes les communes commençant par « p »)."""
    register_geo(requests_mock, departments=[{"nom": "Paris", "code": "75"}])

    assert search_locations(query) == []
    assert requests_mock.call_count == 0


@pytest.mark.parametrize("query", ["ab", " ab ", "  paris  "])
def test_a_query_of_two_characters_or_more_is_searched_after_being_trimmed(requests_mock, query):
    """Les espaces sont retirés AVANT de compter : « ab » entouré d'espaces est
    une requête valide, et c'est la version nettoyée qui part à l'API."""
    register_geo(requests_mock, communes=[commune("Nantes", "44109", ["44000"])])

    assert search_locations(query) != []
    assert requests_mock.request_history[-1].qs["nom"] == [query.strip()]


# ===========================================================================
# search_locations — niveau commune
# ===========================================================================


def test_an_ordinary_commune_produces_exactly_one_suggestion(requests_mock):
    """Cas le plus courant : une commune, un code postal, une entrée. Le contrat
    complet d'une suggestion `city` est figé ici, champ par champ — c'est
    directement une entrée de `locations` dans les critères canoniques."""
    register_geo(requests_mock, communes=[commune("Nantes", "44109", ["44000"], lon=-1.5603, lat=47.2382)])

    assert search_locations("nantes") == [
        {
            "kind": CITY,
            "label": "Nantes (44000)",
            "city": "Nantes",
            "postalCode": "44000",
            "inseeCode": "44109",
            "lat": 47.2382,
            "lon": -1.5603,
        }
    ]


def test_latitude_and_longitude_are_read_in_geojson_order_not_in_reading_order(requests_mock):
    """Piège de lecture à figer : `lon, lat = coords[0], coords[1]`.

    GeoJSON impose `[longitude, latitude]`, l'inverse de l'ordre usuel « lat,
    lon » — le code est donc CORRECT, mais quiconque relit cette ligne en
    pensant « lat d'abord » l'inversera. Nantes est choisie parce que
    l'inversion serait flagrante : (47,2 ; -1,56) est bien la Loire-Atlantique,
    tandis que (-1,56 ; 47,2) tombe dans l'océan Indien.
    """
    register_geo(requests_mock, communes=[commune("Nantes", "44109", ["44000"], lon=-1.5603, lat=47.2382)])

    suggestion = search_locations("nantes")[0]

    assert (suggestion["lat"], suggestion["lon"]) == (47.2382, -1.5603)


@pytest.mark.parametrize(
    ("centre", "description"),
    [
        pytest.param({}, "objet centre vide", id="centre-vide"),
        pytest.param({"type": "Point"}, "centre sans coordonnées", id="coordonnees-absentes"),
        pytest.param({"coordinates": []}, "liste de coordonnées vide", id="coordonnees-liste-vide"),
        pytest.param({"coordinates": None}, "coordonnées nulles", id="coordonnees-none"),
    ],
)
def test_a_commune_without_usable_coordinates_is_still_suggested(requests_mock, centre, description):
    """Les coordonnées sont un bonus (elles servent au repli géographique de
    SeLoger) : leur absence ne doit pas faire disparaître la commune de
    l'autocomplete, seulement laisser lat/lon à None."""
    register_geo(
        requests_mock,
        communes=[{"nom": "Nantes", "code": "44109", "codesPostaux": ["44000"], "centre": centre}],
    )

    suggestion = search_locations("nantes")[0]

    assert (suggestion["lat"], suggestion["lon"]) == (None, None), description
    assert suggestion["postalCode"] == "44000"


def test_a_commune_with_a_single_coordinate_crashes_the_whole_autocomplete(requests_mock):
    """# BUG : `coords = centre.get("coordinates") or [None, None]` ne protège que
    le cas FALSY (absent, `None`, liste vide). Une liste tronquée à un élément
    passe le repli, puis `coords[1]` lève `IndexError`.

    Aggravant : la boucle `for commune in communes: suggestions.extend(...)` de
    `search_locations` est la SEULE des trois qui ne soit pas dans un try/except
    (comparer aux lignes 316-320 et 322-331 de core/geocode.py). Une seule
    commune malformée renvoyée par l'API géo fait donc remonter une IndexError
    jusqu'à la route d'autocomplete — soit un 500 sur la création de recherche,
    alors que les suggestions de région et de département étaient déjà prêtes.

    Le correctif serait de dérouler proprement (`lon, lat = (coords + [None,
    None])[:2]`) et/ou d'entourer cette boucle du même try/except que les autres.
    Comportement ACTUEL figé ci-dessous.
    """
    register_geo(
        requests_mock,
        departments=[{"nom": "Loire-Atlantique", "code": "44"}],
        communes=[{"nom": "Nantes", "code": "44109", "codesPostaux": ["44000"], "centre": {"coordinates": [-1.56]}}],
    )

    with pytest.raises(IndexError, match="list index out of range"):
        search_locations("nantes")


@pytest.mark.parametrize(
    ("postal_codes", "description"),
    [
        pytest.param([], "liste vide", id="codes-postaux-liste-vide"),
        pytest.param(None, "champ à None", id="codes-postaux-none"),
    ],
)
def test_a_commune_without_any_postal_code_is_dropped(requests_mock, postal_codes, description):
    """Sans code postal, la commune n'est exploitable par aucune source : la
    proposer donnerait une recherche qui ne remonte rien."""
    register_geo(requests_mock, communes=[{"nom": "Nulle part", "code": "00000", "codesPostaux": postal_codes}])

    assert search_locations("nulle part") == [], description


def test_a_multi_postal_code_commune_gets_a_whole_city_entry_before_its_postal_codes(requests_mock):
    """Bordeaux couvre 5 codes postaux : sans l'entrée « toute la ville », il
    faudrait ajouter 5 lignes à la main pour chercher dans Bordeaux."""
    register_geo(
        requests_mock,
        communes=[commune("Bordeaux", "33063", ["33800", "33000", "33200", "33300", "33100"], lon=-0.57, lat=44.84)],
    )

    suggestions = search_locations("bordeaux")

    assert kinds(suggestions) == [WHOLE_CITY] + [CITY] * 5
    assert suggestions[0] == {
        "kind": WHOLE_CITY,
        "label": "Bordeaux — toute la ville (5 codes postaux)",
        "city": "Bordeaux",
        "inseeCode": "33063",
        "postalCodes": ["33000", "33100", "33200", "33300", "33800"],
        "lat": 44.84,
        "lon": -0.57,
    }


def test_postal_codes_are_sorted_even_when_the_api_returns_them_shuffled(requests_mock):
    """Un ordre stable est nécessaire : ces suggestions sont réaffichées telles
    quelles dans le formulaire, et un ordre changeant d'une frappe à l'autre
    ferait bouger la liste sous le curseur."""
    register_geo(requests_mock, communes=[commune("Bordeaux", "33063", ["33800", "33000", "33300"])])

    suggestions = search_locations("bordeaux")

    assert [s["postalCode"] for s in suggestions if s["kind"] == CITY] == ["33000", "33300", "33800"]
    assert suggestions[0]["postalCodes"] == ["33000", "33300", "33800"]


def test_a_single_postal_code_commune_gets_no_redundant_whole_city_entry(requests_mock):
    """« Poitiers — toute la ville (1 code postal) » et « Poitiers (86000) »
    désigneraient exactement le même périmètre : la première serait du bruit."""
    register_geo(requests_mock, communes=[commune("Poitiers", "86194", ["86000"], lon=0.37, lat=46.58)])

    assert kinds(search_locations("poitiers")) == [CITY]


@pytest.mark.parametrize(
    ("city", "insee", "postal_codes", "expected"),
    [
        pytest.param(
            "Paris",
            "75056",
            ["75001", "75002", "75015", "75020"],
            {"75001": "75101", "75002": "75102", "75015": "75115", "75020": "75120"},
            id="paris-20-arrondissements",
        ),
        pytest.param(
            "Lyon",
            "69123",
            ["69001", "69007", "69009"],
            {"69001": "69381", "69007": "69387", "69009": "69389"},
            id="lyon-9-arrondissements",
        ),
        pytest.param(
            "Marseille",
            "13055",
            ["13001", "13008", "13016"],
            {"13001": "13201", "13008": "13208", "13016": "13216"},
            id="marseille-16-arrondissements",
        ),
    ],
)
def test_each_arrondissement_gets_its_own_insee_code_not_the_aggregate_one(
    requests_mock, city, insee, postal_codes, expected
):
    """L'API renvoie Paris/Lyon/Marseille comme UN agrégat portant tous les
    codes postaux de leurs arrondissements sous un seul code INSEE
    (75056/69123/13055). Associer cet agrégat à chaque code postal produirait
    des paires fausses — « Paris (75001) » tagué 75056 — et Laforêt chercherait
    dans tout Paris au lieu du 1er arrondissement."""
    register_geo(requests_mock, communes=[commune(city, insee, postal_codes)])

    suggestions = search_locations(city.lower())

    assert {s["postalCode"]: s["inseeCode"] for s in suggestions if s["kind"] == CITY} == expected
    assert insee not in [s["inseeCode"] for s in suggestions if s["kind"] == CITY]


def test_the_whole_city_entry_keeps_the_aggregate_insee_code(requests_mock):
    """Au niveau « toute la ville », l'agrégat est justement le bon identifiant :
    75056 désigne bien Paris entier."""
    register_geo(requests_mock, communes=[commune("Paris", "75056", ["75001", "75002"])])

    whole_city = search_locations("paris")[0]

    assert (whole_city["kind"], whole_city["inseeCode"]) == (WHOLE_CITY, "75056")


def test_a_multi_postal_code_city_without_arrondissements_falls_back_to_its_commune_code(requests_mock):
    """`(_arrondissement_insee_code(cp) if len(cps) > 1 else insee) or insee` :
    Bordeaux a 5 codes postaux mais pas d'arrondissements, la formule renvoie
    None pour chacun et le `or insee` rattrape. Sans ce repli, les 5 entrées de
    Bordeaux sortiraient avec `inseeCode: None` et SeLoger ne saurait plus
    résoudre le moindre placeId."""
    register_geo(requests_mock, communes=[commune("Bordeaux", "33063", ["33000", "33100", "33200"])])

    city_entries = [s for s in search_locations("bordeaux") if s["kind"] == CITY]

    assert {s["inseeCode"] for s in city_entries} == {"33063"}
    assert len(city_entries) == 3


def test_several_communes_are_all_expanded_in_the_order_the_api_ranked_them(requests_mock):
    """`boost=population` classe les homonymes par taille : « saint » doit
    proposer Saint-Étienne avant Saint-Bidule-sur-Rien."""
    register_geo(
        requests_mock,
        communes=[
            commune("Saint-Étienne", "42218", ["42000", "42100"]),
            commune("Saint-Denis", "93066", ["93200"]),
        ],
    )

    suggestions = search_locations("saint")

    assert kinds(suggestions) == [WHOLE_CITY, CITY, CITY, CITY]
    assert [s.get("city") for s in suggestions] == ["Saint-Étienne"] * 3 + ["Saint-Denis"]


def test_the_communes_query_boosts_by_population_and_caps_the_api_side_count(requests_mock):
    """`limit: 5` est envoyé à l'API et n'a rien à voir avec le `limit` de
    `search_locations` : quel que soit ce dernier, au plus 5 communes sont
    ramenées (donc les 5 plus peuplées, grâce au boost). Un `limit=100` côté
    appelant ne fait donc PAS remonter plus de communes."""
    register_geo(requests_mock, communes=[commune("Nantes", "44109", ["44000"])])

    search_locations("nantes", limit=100)

    assert requests_mock.last_request.qs == {
        "nom": ["nantes"],
        "boost": ["population"],
        "fields": ["nom,code,codespostaux,centre"],
        "limit": ["5"],
    }


# ===========================================================================
# search_locations — périmètres larges
# ===========================================================================


def test_a_region_is_suggested_with_its_departments_precomputed(requests_mock):
    """Les départements sont mémorisés À LA SAISIE, pas au scrape : les sources
    qui ne connaissent que les départements en ont besoin à chaque exécution, et
    réinterroger l'API géo à ce moment-là ajouterait une panne possible au
    milieu du pipeline."""
    register_geo(
        requests_mock,
        regions=[{"nom": "Île-de-France", "code": "11"}],
        region_departments={"11": ["75", "77", "78", "91", "92", "93", "94", "95"]},
    )

    assert search_locations("ile de france") == [
        {
            "kind": REGION,
            "label": "Île-de-France (région)",
            "name": "Île-de-France",
            "code": "11",
            "departments": ["75", "77", "78", "91", "92", "93", "94", "95"],
        }
    ]


def test_a_region_whose_departments_cannot_be_fetched_is_still_suggested_but_empty(requests_mock):
    """`region_departments` échoue en silence (voir plus haut) : la région reste
    proposable. Elle sera cependant inexploitable par une source qui a besoin
    des départements — la panne est reportée, pas absorbée."""
    register_geo(requests_mock, regions=[{"nom": "Île-de-France", "code": "11"}])
    requests_mock.get(f"{REGIONS_API}/11/departements", status_code=500)

    suggestion = search_locations("ile de france")[0]

    assert suggestion["kind"] == REGION
    assert suggestion["departments"] == []


@pytest.mark.parametrize(
    ("region", "description"),
    [
        pytest.param({"nom": "Sans code"}, "code absent", id="sans-code"),
        pytest.param({"code": "11"}, "nom absent", id="sans-nom"),
        pytest.param({"nom": "", "code": "11"}, "nom vide", id="nom-vide"),
        pytest.param({"nom": "Bretagne", "code": ""}, "code vide", id="code-vide"),
    ],
)
def test_an_incomplete_region_is_skipped(requests_mock, region, description):
    """Sans code, le périmètre est inexploitable ; sans nom, la suggestion est
    illisible. Les deux sont requis."""
    register_geo(requests_mock, regions=[region])

    assert search_locations("quelque chose") == [], description


def test_a_department_is_suggested_with_a_label_that_says_it_covers_everything(requests_mock):
    """Le libellé doit lever l'ambiguïté avec la commune homonyme : « Gironde »
    seul ne dirait pas si c'est le département ou Gironde-sur-Dropt."""
    register_geo(requests_mock, departments=[{"nom": "Gironde", "code": "33"}])

    assert search_locations("gironde") == [
        {
            "kind": DEPARTMENT,
            "label": "Gironde (33) — tout le département",
            "name": "Gironde",
            "code": "33",
        }
    ]


@pytest.mark.parametrize(
    ("department", "description"),
    [
        pytest.param({"nom": "Sans code"}, "code absent", id="sans-code"),
        pytest.param({"code": "33"}, "nom absent", id="sans-nom"),
        pytest.param({"nom": "", "code": "33"}, "nom vide", id="nom-vide"),
        pytest.param({"nom": "Gironde", "code": ""}, "code vide", id="code-vide"),
    ],
)
def test_an_incomplete_department_is_skipped(requests_mock, department, description):
    register_geo(requests_mock, departments=[department])

    assert search_locations("quelque chose") == [], description


def test_wide_areas_always_come_before_communes(requests_mock):
    """« gironde » doit proposer le département AVANT ses communes homonymes
    (Gironde-sur-Dropt, Castres-Gironde...) : une seule commune un peu peuplée
    suffirait sinon à noyer la seule suggestion utile."""
    register_geo(
        requests_mock,
        regions=[{"nom": "Nouvelle-Aquitaine", "code": "75"}],
        region_departments={"75": ["33", "40"]},
        departments=[{"nom": "Gironde", "code": "33"}],
        communes=[commune("Gironde-sur-Dropt", "33190", ["33190"])],
    )

    assert kinds(search_locations("gironde")) == [REGION, DEPARTMENT, CITY]


def test_the_four_perimeter_levels_are_returned_from_widest_to_narrowest(requests_mock):
    """L'ordre de la liste EST l'information : du plus large au plus précis,
    comme annoncé par le docstring du module."""
    register_geo(
        requests_mock,
        regions=[{"nom": "Nouvelle-Aquitaine", "code": "75"}],
        region_departments={"75": ["33"]},
        departments=[{"nom": "Gironde", "code": "33"}],
        communes=[commune("Bordeaux", "33063", ["33000", "33200"])],
    )

    assert kinds(search_locations("bordeaux gironde")) == [REGION, DEPARTMENT, WHOLE_CITY, CITY, CITY]
    assert LOCATION_KINDS == (REGION, DEPARTMENT, WHOLE_CITY, CITY)


# ===========================================================================
# search_locations — départements redondants
# ===========================================================================


def test_a_department_covering_exactly_a_suggested_city_is_dropped(requests_mock):
    """Paris est à la fois une commune (INSEE 75056) et un département (75) sur
    le même territoire : les deux entrées s'afficheraient côte à côte avec un
    libellé quasi identique, sans moyen de deviner laquelle choisir. On garde la
    ville, qui porte les codes postaux et fonctionne pour les deux sources."""
    register_geo(
        requests_mock,
        departments=[{"nom": "Paris", "code": "75"}],
        communes=[commune("Paris", "75056", ["75001", "75002"])],
    )

    suggestions = search_locations("paris")

    assert DEPARTMENT not in kinds(suggestions)
    assert suggestions[0]["kind"] == WHOLE_CITY


@pytest.mark.parametrize(
    ("department_name", "city_name"),
    [
        pytest.param("Paris", "Paris", id="casse-identique"),
        pytest.param("PARIS", "paris", id="departement-en-majuscules"),
        pytest.param("paris", "Paris", id="departement-en-minuscules"),
        pytest.param("PaRiS", "pArIs", id="casse-melangee"),
    ],
)
def test_the_homonymy_is_detected_case_insensitively(requests_mock, department_name, city_name):
    """`casefold()` des deux côtés : l'API n'a aucune raison de garantir la même
    casse pour un département et pour une commune."""
    register_geo(
        requests_mock,
        departments=[{"nom": department_name, "code": "75"}],
        communes=[commune(city_name, "75056", ["75001", "75002"])],
    )

    assert DEPARTMENT not in kinds(search_locations("paris"))


def test_only_a_whole_city_entry_can_absorb_a_department_never_a_single_postal_code(requests_mock):
    """La comparaison ne regarde que les entrées `whole_city`. Une commune à un
    seul code postal n'en produit pas, donc son département homonyme SURVIT —
    et c'est voulu : le département de la Gironde couvre 534 communes, il n'est
    pas redondant avec la seule commune de Gironde-sur-Dropt."""
    register_geo(
        requests_mock,
        departments=[{"nom": "Gironde-sur-Dropt", "code": "33"}],
        communes=[commune("Gironde-sur-Dropt", "33190", ["33190"])],
    )

    assert kinds(search_locations("gironde")) == [DEPARTMENT, CITY]


def test_a_department_with_a_different_name_is_kept_alongside_the_city(requests_mock):
    register_geo(
        requests_mock,
        departments=[{"nom": "Gironde", "code": "33"}],
        communes=[commune("Bordeaux", "33063", ["33000", "33200"])],
    )

    assert kinds(search_locations("bordeaux")) == [DEPARTMENT, WHOLE_CITY, CITY, CITY]


def test_a_region_homonymous_with_a_city_is_not_dropped(requests_mock):
    """Le filtre ne concerne QUE les départements. Une région et une commune de
    même nom (« Corse ») restent tous deux proposés, à juste titre : une région
    ne se confond pas avec une commune."""
    register_geo(
        requests_mock,
        regions=[{"nom": "Corse", "code": "94"}],
        region_departments={"94": ["2A", "2B"]},
        communes=[commune("Corse", "20000", ["20000", "20090"])],
    )

    assert kinds(search_locations("corse")) == [REGION, WHOLE_CITY, CITY, CITY]


# ===========================================================================
# search_locations — dédoublonnage et troncature
# ===========================================================================


def test_the_same_commune_returned_twice_yields_a_single_suggestion(requests_mock):
    """L'API a déjà renvoyé des doublons (communes-actuelles et communes
    déléguées portant les mêmes valeurs) : le dédoublonnage se fait sur
    l'identité du périmètre, pas sur le libellé."""
    duplicated = commune("Nantes", "44109", ["44000"], lon=-1.5603, lat=47.2382)
    register_geo(requests_mock, communes=[duplicated, dict(duplicated)])

    assert len(search_locations("nantes")) == 1


def test_a_region_and_a_department_sharing_a_code_are_both_kept(requests_mock):
    """La clé de dédoublonnage inclut le `kind`, et il le faut : le code « 75 »
    désigne le département de Paris ET la région Nouvelle-Aquitaine. Les
    confondre ferait disparaître l'un des deux de l'autocomplete."""
    register_geo(
        requests_mock,
        regions=[{"nom": "Nouvelle-Aquitaine", "code": "75"}],
        region_departments={"75": ["33"]},
        departments=[{"nom": "Paris", "code": "75"}],
    )

    suggestions = search_locations("75")

    assert kinds(suggestions) == [REGION, DEPARTMENT]
    assert [s["code"] for s in suggestions] == ["75", "75"]


def test_two_communes_sharing_an_insee_code_collapse_at_the_whole_city_level(requests_mock):
    """Clé de dédoublonnage d'une `whole_city` : `inseeCode` (elle n'a ni `code`
    ni `postalCode`). Deux agrégats du même code INSEE ne donnent qu'une entrée
    « toute la ville », mais leurs codes postaux distincts restent proposés."""
    register_geo(
        requests_mock,
        communes=[
            commune("Paris", "75056", ["75001", "75002"]),
            commune("Paris", "75056", ["75003", "75004"]),
        ],
    )

    suggestions = search_locations("paris")

    assert kinds(suggestions) == [WHOLE_CITY, CITY, CITY, CITY, CITY]
    assert [s["postalCode"] for s in suggestions if s["kind"] == CITY] == ["75001", "75002", "75003", "75004"]


def test_the_limit_is_applied_after_deduplication_not_before(requests_mock):
    """Ordre des opérations, observable et important : avec 3 entrées dont 2
    identiques et `limit=2`, tronquer d'abord ne laisserait qu'UNE suggestion
    utile. Dédoublonner d'abord en laisse bien deux — l'utilisateur reçoit le
    nombre de choix qu'il a demandé, pas moins."""
    nantes = commune("Nantes", "44109", ["44000"])
    register_geo(requests_mock, communes=[nantes, dict(nantes), commune("Rennes", "35238", ["35000"])])

    suggestions = search_locations("n", limit=2) if False else search_locations("nantes rennes", limit=2)

    assert [s["postalCode"] for s in suggestions] == ["44000", "35000"]


@pytest.mark.parametrize(
    ("limit", "expected_count"),
    [
        pytest.param(1, 1, id="une-seule-suggestion"),
        pytest.param(3, 3, id="troncature-au-milieu"),
        pytest.param(10, 10, id="pile-le-nombre-disponible"),
        pytest.param(20, 10, id="limite-par-defaut-au-dessus-du-disponible"),
        pytest.param(0, 0, id="limite-zero-renvoie-tout-sauf-rien"),
    ],
)
def test_the_limit_caps_the_number_of_suggestions(requests_mock, limit, expected_count):
    """Paris seul produit 21 entrées : sans plafond, l'autocomplete serait
    illisible. 9 codes postaux + l'entrée « toute la ville » = 10 disponibles."""
    register_geo(requests_mock, communes=[commune("Paris", "75056", [f"7500{i}" for i in range(1, 10)])])

    assert len(search_locations("paris", limit=limit)) == expected_count


def test_the_widest_perimeters_are_the_ones_that_survive_a_tight_limit(requests_mock):
    """Conséquence de l'ordre : la troncature sacrifie les codes postaux
    individuels, jamais la région ni le département. C'est ce qui garantit qu'un
    périmètre large reste atteignable même sur une requête très ambiguë."""
    register_geo(
        requests_mock,
        regions=[{"nom": "Nouvelle-Aquitaine", "code": "75"}],
        region_departments={"75": ["33"]},
        departments=[{"nom": "Gironde", "code": "33"}],
        communes=[commune("Bordeaux", "33063", ["33000", "33100", "33200", "33300", "33800"])],
    )

    assert kinds(search_locations("bordeaux", limit=3)) == [REGION, DEPARTMENT, WHOLE_CITY]


# ===========================================================================
# search_locations — tolérance aux pannes partielles
# ===========================================================================


@pytest.mark.parametrize(
    "broken_endpoint",
    [
        pytest.param(REGIONS_API, id="regions-en-panne"),
        pytest.param(DEPARTEMENTS_API, id="departements-en-panne"),
        pytest.param(COMMUNES_API, id="communes-en-panne"),
    ],
)
def test_one_broken_level_does_not_erase_the_suggestions_of_the_others(requests_mock, broken_endpoint):
    """Chaque niveau a son propre try/except, et c'est tout l'intérêt : un
    endpoint indisponible dégrade l'autocomplete au lieu de le vider. Un
    try/except unique autour des trois perdrait les suggestions déjà collectées.
    """
    register_geo(
        requests_mock,
        regions=[{"nom": "Nouvelle-Aquitaine", "code": "75"}],
        region_departments={"75": ["33"]},
        departments=[{"nom": "Gironde", "code": "33"}],
        communes=[commune("Bordeaux", "33063", ["33000"])],
    )
    requests_mock.get(broken_endpoint, exc=requests.ConnectionError)

    suggestions = search_locations("bordeaux")

    assert suggestions != [], "les deux niveaux restants doivent répondre"
    assert len(suggestions) == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"status_code": 500}, id="http-500"),
        pytest.param({"status_code": 429}, id="http-429-trop-de-requetes"),
        pytest.param({"exc": requests.ConnectionError}, id="erreur-de-connexion"),
        pytest.param({"exc": requests.Timeout}, id="timeout"),
        pytest.param({"text": "<html>maintenance</html>"}, id="corps-non-json"),
    ],
)
def test_a_fully_unavailable_geo_api_returns_an_empty_list_not_an_error(requests_mock, kwargs):
    """L'autocomplete se contente de ne rien proposer : l'utilisateur peut
    toujours saisir sa localisation à la main, alors qu'un 500 bloquerait la
    création de recherche."""
    for endpoint in (REGIONS_API, DEPARTEMENTS_API, COMMUNES_API):
        requests_mock.get(endpoint, **kwargs)

    assert search_locations("paris") == []


def test_an_api_answering_an_object_instead_of_a_list_is_treated_as_empty(requests_mock):
    """`_query` normalise : `data if isinstance(data, list) else []`. Un
    changement de forme de l'API (enveloppe `{"results": [...]}`) ne fait donc
    pas planter, il ne propose simplement rien."""
    for endpoint in (REGIONS_API, DEPARTEMENTS_API, COMMUNES_API):
        requests_mock.get(endpoint, json={"results": [{"nom": "Paris", "code": "75"}]})

    assert search_locations("paris") == []


# ===========================================================================
# Interaction avec les caches de module
# ===========================================================================


def test_the_departments_of_a_region_are_fetched_once_across_several_autocompletes(requests_mock):
    """Chaque frappe de l'autocomplete rappelle `search_locations`, qui rappelle
    `region_departments` : sans le cache, taper « ile-de-france » émettrait une
    requête de départements par caractère."""
    register_geo(
        requests_mock,
        regions=[{"nom": "Île-de-France", "code": "11"}],
        region_departments={"11": ["75", "92"]},
    )

    for query in ("il", "ile", "ile-", "ile-de-france"):
        assert search_locations(query)[0]["departments"] == ["75", "92"]

    departments_calls = [r for r in requests_mock.request_history if r.path.endswith("/11/departements")]
    assert len(departments_calls) == 1
