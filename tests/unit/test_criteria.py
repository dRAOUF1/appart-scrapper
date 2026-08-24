"""Tests de `core/criteria.py` — le vocabulaire canonique et sa normalisation.

Ce module est 100 % pur : aucune dépendance réseau, base ou horloge. C'est le
contrat entre le front, la base et les parsers, donc chaque règle est figée
ici plutôt que déduite du comportement d'une source.
"""

from __future__ import annotations

import pytest

from core.criteria import (
    APARTMENT,
    BUY,
    HOUSE,
    LAND,
    PARKING,
    RENT,
    has_transit,
    location_label,
    location_postal_prefixes,
    matches_locations,
    normalize_criteria,
    normalize_locations,
    normalize_transit,
    source_overrides,
    with_source_override,
)
from tests.helpers.factories import (
    make_city_location,
    make_criteria,
    make_department_location,
    make_region_location,
    make_transit_selection,
    make_whole_city_location,
)

# ---------------------------------------------------------------------------
# normalize_criteria : entrées dégénérées
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [None, {}, "not-a-dict", [], 0, ["locations"], 42],
)
def test_normalize_criteria_returns_an_empty_dict_for_anything_that_is_not_a_populated_mapping(payload):
    """Aucune entrée illisible ne doit produire de critères partiels : une
    recherche sans critère exploitable vaut mieux qu'une recherche fantaisiste."""
    assert normalize_criteria(payload) == {}


def test_normalize_criteria_omits_absent_keys_instead_of_defaulting_them():
    """Contrat du docstring : « les clés absentes sont omises, jamais devinées ».
    Un `priceMax: 0` ou un `transaction: "rent"` inventé changerait la recherche."""
    result = normalize_criteria({"locations": [make_city_location()]})

    assert set(result) == {"locations"}


# ---------------------------------------------------------------------------
# normalize_criteria : idempotence (invariant du docstring)
# ---------------------------------------------------------------------------

_IDEMPOTENCE_PAYLOADS = [
    pytest.param(make_criteria(), id="canonique-minimal"),
    pytest.param(
        {
            "locations": [make_city_location(city="Paris", postal_code="75015", insee="75115")],
            "transaction": RENT,
            "propertyTypes": [APARTMENT],
            "priceMin": 500,
            "priceMax": 2000,
            "surfaceMin": 20,
            "surfaceMax": 80,
            "rooms": [1, 2],
            "bedrooms": [1],
        },
        id="canonique-complet",
    ),
    pytest.param(
        {
            "city": "Poitiers",
            "postalCode": "86000",
            "distributionTypes": ["Sale"],
            "estateTypes": ["House"],
            "spaceMin": 40,
            "rooms": ["2", "3"],
        },
        id="ancien-vocabulaire-seloger",
    ),
    pytest.param(
        {"placeIds": ["AD08FR31096"], "locationsInBuildingExcluded": ["Ground"]},
        id="cles-propres-a-seloger",
    ),
    pytest.param(
        {
            "locations": [
                make_region_location(code="11", name="Île-de-France", departments=("75", "92")),
                make_department_location(),
                make_whole_city_location(city="Bordeaux", postal_codes=("33800", "33000"), insee="33063"),
                make_city_location(),
            ]
        },
        id="quatre-niveaux-de-perimetre",
    ),
    pytest.param({"order": "DateDesc", "transaction": "barter", "priceMax": "beaucoup"}, id="tout-a-jeter"),
]


@pytest.mark.parametrize("payload", _IDEMPOTENCE_PAYLOADS)
def test_normalizing_already_normalized_criteria_changes_nothing(payload):
    """`normalize(normalize(x)) == normalize(x)`.

    Les recherches en base ne sont pas migrées : elles sont normalisées à
    chaque lecture. Une normalisation non idempotente ferait donc dériver les
    critères d'une recherche à chaque scrape."""
    once = normalize_criteria(payload)

    assert normalize_criteria(once) == once


def test_canonical_criteria_pass_through_untouched():
    criteria = {
        "locations": [{"kind": "city", "city": "Paris", "postalCode": "75015", "inseeCode": "75115"}],
        "transaction": RENT,
        "propertyTypes": [APARTMENT],
        "priceMin": 500,
        "priceMax": 2000,
        "surfaceMin": 20,
        "surfaceMax": 80,
        "rooms": [1, 2],
        "bedrooms": [1],
    }

    assert normalize_criteria(criteria) == criteria


# ---------------------------------------------------------------------------
# Entiers : bornes prix / surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(12, 12, id="int"),
        pytest.param("12", 12, id="chaine-numerique"),
        pytest.param(" 12 ", 12, id="chaine-avec-espaces"),
        pytest.param(12.9, 12, id="float-tronque"),
    ],
)
def test_price_bounds_accept_anything_int_can_parse(raw, expected):
    assert normalize_criteria({"priceMax": raw})["priceMax"] == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("abc", id="chaine-non-numerique"),
        pytest.param("", id="chaine-vide"),
        pytest.param(None, id="none"),
        pytest.param([], id="liste"),
        pytest.param({}, id="dict"),
        pytest.param("12.5", id="float-en-chaine"),
    ],
)
def test_unparsable_price_bounds_are_omitted_rather_than_zeroed(raw):
    """Un critère illisible est ignoré : le mettre à 0 filtrerait tout."""
    assert "priceMax" not in normalize_criteria({"priceMax": raw, "priceMin": 500})


@pytest.mark.parametrize("key", ["priceMin", "priceMax", "surfaceMin", "surfaceMax"])
@pytest.mark.parametrize("value", [True, False])
def test_booleans_are_rejected_as_numeric_criteria(key, value):
    """`_to_int` refuse explicitement `bool` : sans ça, `True` deviendrait 1 et
    un `priceMax` coché par erreur limiterait la recherche à 1 €."""
    assert key not in normalize_criteria({key: value})


def test_booleans_are_also_rejected_inside_room_counts():
    assert "rooms" not in normalize_criteria({"rooms": [True, False]})


@pytest.mark.parametrize(
    ("legacy_key", "canonical_key"),
    [("spaceMin", "surfaceMin"), ("spaceMax", "surfaceMax")],
)
def test_legacy_space_bounds_are_renamed_to_surface(legacy_key, canonical_key):
    result = normalize_criteria({legacy_key: 30})

    assert result[canonical_key] == 30
    assert legacy_key not in result


def test_the_canonical_surface_key_wins_over_its_legacy_alias():
    result = normalize_criteria({"surfaceMin": 30, "spaceMin": 90})

    assert result["surfaceMin"] == 30


def test_price_bounds_have_no_legacy_alias():
    """`priceMin`/`priceMax` portaient déjà ce nom dans l'ancien format : rien à
    traduire, et surtout rien d'autre à accepter."""
    assert normalize_criteria({"priceMinimum": 500}) == {}


# ---------------------------------------------------------------------------
# transaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("rent", RENT, id="canonique"),
        pytest.param("Rent", RENT, id="casse-seloger"),
        pytest.param(" RENT ", RENT, id="majuscules-et-espaces"),
        pytest.param("buy", BUY, id="canonique-achat"),
        pytest.param("sale", BUY, id="alias-seloger-sale"),
        pytest.param("SALE", BUY, id="alias-seloger-majuscule"),
    ],
)
def test_transaction_is_read_case_insensitively(raw, expected):
    assert normalize_criteria({"transaction": raw})["transaction"] == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("barter", id="valeur-inconnue"),
        pytest.param("", id="chaine-vide"),
        pytest.param(123, id="non-str"),
        pytest.param(["rent"], id="liste-sur-la-cle-canonique"),
        pytest.param({"kind": "rent"}, id="dict"),
    ],
)
def test_an_unrecognized_transaction_is_dropped_never_guessed(raw):
    assert "transaction" not in normalize_criteria({"transaction": raw})


@pytest.mark.parametrize(
    ("distribution_types", "expected"),
    [
        pytest.param(["Rent"], RENT, id="location"),
        pytest.param(["Sale"], BUY, id="achat"),
        pytest.param("Rent", RENT, id="valeur-nue-hors-liste"),
    ],
)
def test_legacy_distribution_types_become_transaction(distribution_types, expected):
    assert normalize_criteria({"distributionTypes": distribution_types})["transaction"] == expected


def test_only_the_first_legacy_distribution_type_is_kept():
    """L'ancien format était une liste parce que SeLoger l'accepte, mais une
    recherche ne mélange jamais location et achat : la seconde valeur est du
    bruit, pas un second critère."""
    assert normalize_criteria({"distributionTypes": ["Sale", "Rent"]})["transaction"] == BUY


def test_the_canonical_transaction_wins_over_the_legacy_list():
    result = normalize_criteria({"transaction": "rent", "distributionTypes": ["Sale"]})

    assert result["transaction"] == RENT


# ---------------------------------------------------------------------------
# propertyTypes
# ---------------------------------------------------------------------------


def test_all_four_legacy_estate_types_are_translated_in_order():
    result = normalize_criteria({"estateTypes": ["Apartment", "House", "Parking", "Land"]})

    assert result["propertyTypes"] == [APARTMENT, HOUSE, PARKING, LAND]


def test_property_types_are_deduplicated_while_preserving_the_order_asked():
    """L'ordre est celui de la saisie, pas un tri : c'est ce que le front
    réaffiche, et un tri alphabétique réordonnerait les cases à cocher."""
    result = normalize_criteria({"propertyTypes": ["house", "Apartment", "HOUSE", "apartment", "land"]})

    assert result["propertyTypes"] == [HOUSE, APARTMENT, LAND]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(["castle"], id="type-inconnu"),
        pytest.param([None, 42, {"kind": "apartment"}], id="valeurs-non-str"),
        pytest.param([], id="liste-vide"),
        pytest.param([""], id="chaine-vide"),
    ],
)
def test_unknown_property_types_leave_the_key_absent(raw):
    assert "propertyTypes" not in normalize_criteria({"propertyTypes": raw})


def test_unknown_property_types_are_dropped_but_the_known_ones_survive():
    result = normalize_criteria({"propertyTypes": ["castle", "Apartment"]})

    assert result["propertyTypes"] == [APARTMENT]


def test_the_canonical_property_types_win_over_the_legacy_estate_types():
    result = normalize_criteria({"propertyTypes": ["house"], "estateTypes": ["Apartment"]})

    assert result["propertyTypes"] == [HOUSE]


def test_legacy_estate_types_are_used_when_the_canonical_list_yields_nothing():
    """`_as_list(propertyTypes) or _as_list(estateTypes)` : une liste canonique
    vide n'empêche pas de lire l'ancienne clé."""
    result = normalize_criteria({"propertyTypes": [], "estateTypes": ["House"]})

    assert result["propertyTypes"] == [HOUSE]


# ---------------------------------------------------------------------------
# rooms / bedrooms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["rooms", "bedrooms"])
def test_counts_are_parsed_deduplicated_and_sorted(key):
    """L'ancien format les stockait en chaînes (cases à cocher du formulaire) ;
    le canonique les veut en entiers pour que les sources comparent sans
    reconvertir."""
    result = normalize_criteria({key: ["1", "2", "2", 3, "3"]})

    assert result[key] == [1, 2, 3]


@pytest.mark.parametrize("key", ["rooms", "bedrooms"])
def test_unparsable_counts_are_skipped_without_dropping_the_valid_ones(key):
    result = normalize_criteria({key: ["2", "beaucoup", None, "4"]})

    assert result[key] == [2, 4]


@pytest.mark.parametrize("key", ["rooms", "bedrooms"])
@pytest.mark.parametrize("raw", [[], None, ["abc"], "abc"])
def test_an_empty_count_list_leaves_the_key_absent(key, raw):
    assert key not in normalize_criteria({key: raw})


@pytest.mark.parametrize("key", ["rooms", "bedrooms"])
def test_a_bare_count_is_accepted_outside_a_list(key):
    assert normalize_criteria({key: "3"})[key] == [3]


# ---------------------------------------------------------------------------
# normalize_locations : niveau `city`
# ---------------------------------------------------------------------------


def test_a_location_without_a_kind_is_treated_as_a_single_postal_code():
    """C'est le format d'avant les périmètres larges : les recherches déjà en
    base doivent continuer de tourner sans migration."""
    locations = normalize_locations({"locations": [{"city": "Lyon", "postalCode": "69007"}]})

    assert locations == [{"kind": "city", "city": "Lyon", "postalCode": "69007"}]


def test_the_flat_legacy_city_postal_code_pair_becomes_a_location():
    result = normalize_criteria({"city": "Poitiers", "postalCode": "86000"})

    assert result["locations"] == [{"kind": "city", "city": "Poitiers", "postalCode": "86000"}]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"city": "Poitiers"}, id="ville-sans-code-postal"),
        pytest.param({"postalCode": "86000"}, id="code-postal-sans-ville"),
        pytest.param({}, id="ni-l-un-ni-l-autre"),
    ],
)
def test_an_incomplete_flat_legacy_pair_produces_no_location(payload):
    assert "locations" not in normalize_criteria(payload)


def test_the_locations_list_wins_over_the_flat_pair_instead_of_adding_to_it():
    """Le couple à plat n'était qu'un miroir de la première localisation (voir
    routes/web.py) — il ne doit pas en ajouter une seconde, dupliquée."""
    result = normalize_criteria(
        {
            "locations": [
                {"city": "Paris", "postalCode": "75015"},
                {"city": "Paris", "postalCode": "75014"},
            ],
            "city": "Paris",
            "postalCode": "75015",
        }
    )

    assert [loc["postalCode"] for loc in result["locations"]] == ["75015", "75014"]


def test_the_flat_pair_is_only_a_fallback_for_an_empty_locations_list():
    result = normalize_criteria({"locations": [], "city": "Poitiers", "postalCode": "86000"})

    assert result["locations"] == [{"kind": "city", "city": "Poitiers", "postalCode": "86000"}]


@pytest.mark.parametrize(
    "location",
    [
        pytest.param({"city": "Paris"}, id="sans-code-postal"),
        pytest.param({"postalCode": "75015"}, id="sans-ville"),
        pytest.param({"city": "  ", "postalCode": "75015"}, id="ville-en-blanc"),
        pytest.param({"city": "Paris", "postalCode": "   "}, id="code-postal-en-blanc"),
        pytest.param({"city": "Paris", "postalCode": None}, id="code-postal-none"),
        pytest.param("not-a-dict", id="entree-non-dict"),
        pytest.param(None, id="entree-none"),
        pytest.param(42, id="entree-numerique"),
    ],
)
def test_an_unusable_location_entry_is_dropped_without_taking_the_others_down(location):
    result = normalize_criteria({"locations": [location, {"city": "Lyon", "postalCode": "69007"}]})

    assert result["locations"] == [{"kind": "city", "city": "Lyon", "postalCode": "69007"}]


def test_city_and_postal_code_are_stripped_and_the_postal_code_stringified():
    """Un code postal arrivant en entier depuis un JSON mal typé doit rester
    comparable par `startswith` (voir matches_locations)."""
    result = normalize_criteria({"locations": [{"city": " Paris ", "postalCode": 75015}]})

    assert result["locations"] == [{"kind": "city", "city": "Paris", "postalCode": "75015"}]


@pytest.mark.parametrize("kind_written_as", ["city", "CITY", " City ", "WHOLE_CITY", "Region", "DEPARTMENT"])
def test_the_kind_is_read_case_insensitively_and_stripped(kind_written_as):
    location = {
        "kind": kind_written_as,
        "city": "Bordeaux",
        "postalCode": "33000",
        "postalCodes": ["33000", "33800"],
        "code": "33",
        "name": "Gironde",
    }

    normalized = normalize_locations({"locations": [location]})

    assert normalized[0]["kind"] == kind_written_as.strip().lower()


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("planet", id="niveau-inconnu"),
        pytest.param("commune", id="synonyme-non-declare"),
        pytest.param("cities", id="pluriel"),
    ],
)
def test_an_unknown_kind_is_dropped_and_never_falls_back_to_city(kind):
    """Deviner « city » pour un niveau inconnu lancerait une recherche sur un
    périmètre que l'utilisateur n'a pas demandé."""
    payload = {"locations": [{"kind": kind, "city": "Mars", "postalCode": "00000"}]}

    assert normalize_criteria(payload) == {}


@pytest.mark.parametrize(("field", "value"), [("inseeCode", "75115"), ("lat", 48.8), ("lon", 2.3)])
def test_optional_geo_fields_are_preserved_on_a_city_when_present(field, value):
    """`inseeCode` est ce qui permet à chaque source de retrouver son propre
    identifiant de lieu : le perdre en route rend la recherche inexploitable
    pour SeLoger (voir services/seloger_geocode)."""
    location = {"city": "Paris", "postalCode": "75015", field: value}

    assert normalize_locations({"locations": [location]})[0][field] == value


@pytest.mark.parametrize("field", ["inseeCode", "lat", "lon"])
def test_optional_geo_fields_set_to_none_are_omitted_not_stored_as_none(field):
    location = {"city": "Paris", "postalCode": "75015", field: None}

    assert field not in normalize_locations({"locations": [location]})[0]


@pytest.mark.parametrize("field", ["lat", "lon"])
def test_a_zero_coordinate_is_kept_because_the_test_is_on_none_not_on_truthiness(field):
    """Le méridien de Greenwich passe par la France : `lon = 0.0` est une
    coordonnée valide, pas une absence de coordonnée."""
    location = {"city": "Saint-Cyprien", "postalCode": "24220", field: 0.0}

    assert normalize_locations({"locations": [location]})[0][field] == 0.0


def test_a_city_carries_no_field_it_was_not_given():
    location = make_city_location()

    assert set(normalize_locations({"locations": [location]})[0]) == {
        "kind",
        "city",
        "postalCode",
        "inseeCode",
    }


# ---------------------------------------------------------------------------
# normalize_locations : niveau `whole_city`
# ---------------------------------------------------------------------------


def test_a_whole_city_keeps_all_its_postal_codes_deduplicated_and_sorted():
    result = normalize_criteria(
        {
            "locations": [
                {
                    "kind": "whole_city",
                    "city": "Bordeaux",
                    "inseeCode": "33063",
                    "postalCodes": ["33800", "33000", "33000"],
                }
            ]
        }
    )

    assert result["locations"] == [
        {
            "kind": "whole_city",
            "city": "Bordeaux",
            "postalCodes": ["33000", "33800"],
            "inseeCode": "33063",
        }
    ]


def test_whole_city_postal_codes_are_stripped_and_stringified():
    location = {"kind": "whole_city", "city": "Bordeaux", "postalCodes": [" 33800 ", 33000]}

    assert normalize_locations({"locations": [location]})[0]["postalCodes"] == ["33000", "33800"]


@pytest.mark.parametrize(
    "location",
    [
        pytest.param({"kind": "whole_city", "city": "Bordeaux", "inseeCode": "33063"}, id="sans-codes-postaux"),
        pytest.param({"kind": "whole_city", "city": "Bordeaux", "postalCodes": []}, id="liste-vide"),
        pytest.param({"kind": "whole_city", "city": "Bordeaux", "postalCodes": ["", "  "]}, id="codes-en-blanc"),
        pytest.param({"kind": "whole_city", "postalCodes": ["33000"]}, id="sans-ville"),
        pytest.param({"kind": "whole_city", "city": " ", "postalCodes": ["33000"]}, id="ville-en-blanc"),
    ],
)
def test_a_whole_city_needs_a_name_and_at_least_one_postal_code(location):
    """Sans code postal, le contrôle local de périmètre ne pourrait rien
    vérifier — la localisation est donc écartée plutôt qu'acceptée à vide."""
    assert normalize_criteria({"locations": [location]}) == {}


@pytest.mark.parametrize(("field", "value"), [("inseeCode", "33063"), ("lat", 44.84), ("lon", -0.57)])
def test_optional_geo_fields_are_preserved_on_a_whole_city(field, value):
    location = {"kind": "whole_city", "city": "Bordeaux", "postalCodes": ["33000"], field: value}

    assert normalize_locations({"locations": [location]})[0][field] == value


@pytest.mark.parametrize("field", ["inseeCode", "lat", "lon"])
def test_optional_geo_fields_set_to_none_are_omitted_on_a_whole_city(field):
    location = {"kind": "whole_city", "city": "Bordeaux", "postalCodes": ["33000"], field: None}

    assert field not in normalize_locations({"locations": [location]})[0]


def test_a_single_postal_code_whole_city_stays_a_whole_city():
    """Le niveau demandé n'est jamais rétrogradé : `whole_city` et `city` se
    traduisent différemment chez les sources (AD06 vs AD08 chez SeLoger)."""
    location = make_whole_city_location()

    assert normalize_locations({"locations": [location]})[0]["kind"] == "whole_city"


# ---------------------------------------------------------------------------
# normalize_locations : niveaux `department` et `region`
# ---------------------------------------------------------------------------


def test_a_department_keeps_its_code_and_name():
    result = normalize_criteria({"locations": [{"kind": "department", "name": "Gironde", "code": "33"}]})

    assert result["locations"] == [{"kind": "department", "code": "33", "name": "Gironde"}]


def test_a_region_keeps_its_code_name_and_memorized_departments():
    """Les départements sont mémorisés à la saisie pour ne pas réinterroger
    l'API géo à chaque scrape (voir core.geocode.region_departments)."""
    result = normalize_criteria(
        {
            "locations": [
                {
                    "kind": "region",
                    "name": "Île-de-France",
                    "code": "11",
                    "departments": ["75", "77", "78", "91", "92", "93", "94", "95"],
                }
            ]
        }
    )

    assert result["locations"] == [
        {
            "kind": "region",
            "code": "11",
            "name": "Île-de-France",
            "departments": ["75", "77", "78", "91", "92", "93", "94", "95"],
        }
    ]


@pytest.mark.parametrize("kind", ["region", "department"])
@pytest.mark.parametrize(
    "code",
    [
        pytest.param(None, id="code-none"),
        pytest.param("", id="code-vide"),
        pytest.param("   ", id="code-en-blanc"),
    ],
)
def test_a_wide_area_without_a_code_is_dropped(kind, code):
    """Sans code, le périmètre est inexploitable : aucune source ne saurait le
    traduire, et un périmètre vide vaudrait « toute la France »."""
    assert normalize_criteria({"locations": [{"kind": kind, "name": "Nulle part", "code": code}]}) == {}


@pytest.mark.parametrize("kind", ["region", "department"])
def test_a_wide_area_code_is_stripped_and_stringified(kind):
    location = {"kind": kind, "name": "Gironde", "code": 33}

    assert normalize_locations({"locations": [location]})[0]["code"] == "33"


@pytest.mark.parametrize("kind", ["region", "department"])
@pytest.mark.parametrize("name", [None, "", "   "])
def test_a_wide_area_without_a_name_keeps_only_its_code(kind, name):
    normalized = normalize_locations({"locations": [{"kind": kind, "code": "33", "name": name}]})

    assert normalized == [{"kind": kind, "code": "33"}]


def test_region_departments_are_stripped_and_the_blank_ones_removed():
    location = {"kind": "region", "code": "11", "departments": [" 75 ", "", "  ", 92]}

    assert normalize_locations({"locations": [location]})[0]["departments"] == ["75", "92"]


def test_a_none_department_survives_normalization_as_the_string_none():
    # BUG : le filtre est `if str(d).strip()`, or `str(None)` vaut "None" —
    # une chaîne non vide. Un département `None` traverse donc la
    # normalisation et devient le code de département "None", qui partira
    # tel quel dans les URLs des sources (`filter[departments][]=None` pour
    # Laforêt). Le filtre devrait écarter les valeurs non-scalaires avant
    # de les convertir. Test figeant le comportement ACTUEL.
    location = {"kind": "region", "code": "11", "departments": ["75", None]}

    assert normalize_locations({"locations": [location]})[0]["departments"] == ["75", "None"]


@pytest.mark.parametrize(
    "departments",
    [
        pytest.param(None, id="absents"),
        pytest.param([], id="liste-vide"),
        pytest.param(["", "  "], id="tous-en-blanc"),
    ],
)
def test_a_region_whose_departments_are_all_unusable_keeps_no_departments_key(departments):
    """Une clé `departments: []` serait pire que son absence : une source
    pourrait la lire comme « aucun département », donc aucun résultat."""
    normalized = normalize_locations({"locations": [{"kind": "region", "code": "11", "departments": departments}]})

    assert normalized == [{"kind": "region", "code": "11"}]


def test_a_department_never_carries_a_departments_key():
    location = {"kind": "department", "code": "33", "departments": ["33", "40"]}

    assert "departments" not in normalize_locations({"locations": [location]})[0]


def test_the_four_perimeter_levels_coexist_in_the_order_given():
    result = normalize_criteria(
        {
            "locations": [
                {"kind": "region", "name": "Île-de-France", "code": "11", "departments": ["75"]},
                {"kind": "department", "name": "Gironde", "code": "33"},
                {"kind": "whole_city", "city": "Bordeaux", "postalCodes": ["33000", "33800"]},
                {"city": "Poitiers", "postalCode": "86000"},
            ]
        }
    )

    assert [loc["kind"] for loc in result["locations"]] == ["region", "department", "whole_city", "city"]


# ---------------------------------------------------------------------------
# location_postal_prefixes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        pytest.param({"kind": "city", "city": "Bordeaux", "postalCode": "33000"}, ["33000"], id="city"),
        pytest.param({"city": "Bordeaux", "postalCode": "33000"}, ["33000"], id="city-implicite-sans-kind"),
        pytest.param(
            {"kind": "whole_city", "city": "Bordeaux", "postalCodes": ["33000", "33800"]},
            ["33000", "33800"],
            id="whole_city",
        ),
        pytest.param({"kind": "department", "code": "33"}, ["33"], id="department"),
        pytest.param({"kind": "department", "code": "971"}, ["971"], id="department-outre-mer"),
        pytest.param({"kind": "department", "code": "2A"}, ["20"], id="department-corse"),
        pytest.param({"kind": "region", "departments": ["75", "92", "2B"]}, ["75", "92", "20"], id="region"),
    ],
)
def test_postal_prefixes_cover_the_perimeter_of_each_level(location, expected):
    assert location_postal_prefixes(location) == expected


@pytest.mark.parametrize(
    "location",
    [
        pytest.param({"kind": "city", "city": "Paris"}, id="city-sans-code-postal"),
        pytest.param({"kind": "city", "city": "Paris", "postalCode": ""}, id="city-code-postal-vide"),
        pytest.param({"kind": "whole_city", "city": "Paris"}, id="whole_city-sans-codes"),
        pytest.param({"kind": "whole_city", "city": "Paris", "postalCodes": []}, id="whole_city-liste-vide"),
        pytest.param({"kind": "department"}, id="department-sans-code"),
        pytest.param({"kind": "department", "code": ""}, id="department-code-vide"),
        pytest.param({"kind": "region"}, id="region-sans-departements"),
        pytest.param({"kind": "region", "departments": []}, id="region-liste-vide"),
        pytest.param({"kind": "region", "departments": ["", None]}, id="region-departements-vides"),
        pytest.param({"kind": "planet", "code": "42"}, id="niveau-inconnu"),
        pytest.param({}, id="dict-vide"),
    ],
)
def test_an_incomplete_location_yields_no_prefix_at_all(location):
    """INVARIANT DE SÉCURITÉ : un préfixe vide signifierait « tout code postal
    accepté », soit exactement l'inverse du garde-fou. `[]` fait échouer
    fermé (voir matches_locations), `[""]` ferait échouer ouvert."""
    prefixes = location_postal_prefixes(location)

    assert prefixes == []


@pytest.mark.parametrize("payload", _IDEMPOTENCE_PAYLOADS)
def test_normalized_locations_never_produce_an_empty_prefix(payload):
    """Le même invariant, vérifié sur la sortie réelle de la normalisation :
    c'est celle que les parsers passent à matches_locations."""
    for location in normalize_criteria(payload).get("locations", []):
        prefixes = location_postal_prefixes(location)
        assert prefixes, f"aucun préfixe pour {location}"
        assert all(prefixes), f"préfixe vide dans {prefixes} pour {location}"


# ---------------------------------------------------------------------------
# location_label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        pytest.param(
            {"kind": "region", "name": "Île-de-France", "code": "11"},
            "Île-de-France (région)",
            id="region-nommee",
        ),
        pytest.param({"kind": "region", "code": "11"}, "11 (région)", id="region-sans-nom-retombe-sur-le-code"),
        pytest.param(
            {"kind": "department", "name": "Gironde", "code": "33"},
            "Gironde (33) — tout le département",
            id="department",
        ),
        pytest.param(
            {"kind": "department", "code": "33"},
            "33 (33) — tout le département",
            id="department-sans-nom-retombe-sur-le-code",
        ),
        pytest.param({"kind": "department", "name": "Gironde"}, "Gironde", id="department-sans-code"),
        pytest.param(
            {"kind": "whole_city", "city": "Bordeaux", "postalCodes": ["33000", "33800"]},
            "Bordeaux — toute la ville (2 codes postaux)",
            id="whole_city",
        ),
        pytest.param(
            {"kind": "whole_city", "city": "Bordeaux"},
            "Bordeaux — toute la ville (0 codes postaux)",
            id="whole_city-sans-codes",
        ),
        pytest.param({"kind": "city", "city": "Paris", "postalCode": "75013"}, "Paris (75013)", id="city"),
        pytest.param({"city": "Paris", "postalCode": "75013"}, "Paris (75013)", id="city-implicite-sans-kind"),
    ],
)
def test_a_location_is_labelled_the_same_way_everywhere_it_is_shown(location, expected):
    """Même formulation dans les suggestions de l'autocomplete, le champ du
    formulaire et les étiquettes des cartes de recherche (voir main.py, qui
    l'expose comme filtre Jinja)."""
    assert location_label(location) == expected


def test_the_label_of_an_unknown_kind_falls_back_to_the_city_form():
    """`location_label` n'a pas de branche « inconnu » : elle finit sur la forme
    ville, sans lever — un libellé bizarre vaut mieux qu'une page en erreur."""
    assert location_label({"kind": "planet", "city": "Mars"}) == "Mars (None)"


# ---------------------------------------------------------------------------
# matches_locations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("postal_code", "expected"),
    [("33000", True), ("33800", False), ("3300", False), ("330000", True)],
)
def test_a_city_perimeter_matches_its_postal_code_by_prefix(postal_code, expected):
    """La comparaison est un `startswith`, pas une égalité : c'est ce qui fait
    fonctionner les niveaux larges avec le même code."""
    locations = [{"kind": "city", "city": "Bordeaux", "postalCode": "33000"}]

    assert matches_locations(postal_code, locations) is expected


@pytest.mark.parametrize(("postal_code", "expected"), [("33000", True), ("33800", True), ("33300", False)])
def test_a_whole_city_matches_any_of_its_postal_codes(postal_code, expected):
    locations = [{"kind": "whole_city", "city": "Bordeaux", "postalCodes": ["33000", "33800"]}]

    assert matches_locations(postal_code, locations) is expected


@pytest.mark.parametrize(("postal_code", "expected"), [("33000", True), ("33640", True), ("75015", False)])
def test_a_department_matches_its_whole_postal_range(postal_code, expected):
    locations = [{"kind": "department", "name": "Gironde", "code": "33"}]

    assert matches_locations(postal_code, locations) is expected


@pytest.mark.parametrize(("postal_code", "expected"), [("75015", True), ("93200", True), ("33000", False)])
def test_a_region_matches_every_one_of_its_departments(postal_code, expected):
    locations = [{"kind": "region", "name": "Île-de-France", "code": "11", "departments": ["75", "92", "93"]}]

    assert matches_locations(postal_code, locations) is expected


@pytest.mark.parametrize("corsican_department", ["2A", "2B", "2a"])
@pytest.mark.parametrize("postal_code", ["20000", "20200", "20600"])
def test_both_corsican_departments_match_every_20xxx_postal_code(corsican_department, postal_code):
    """Comportement documenté et assumé : aucun code postal corse ne commence
    par « 2A » ou « 2B » (la Corse est en 20xxx), donc le filtrage LOCAL par
    préfixe ne distingue pas les deux départements — une recherche en
    Corse-du-Sud laissera passer une annonce de Haute-Corse. Ce sont les
    sources qui filtrent correctement, sur le code du département ; sans cet
    aplatissement, une recherche corse ne ramènerait rien du tout."""
    locations = [{"kind": "department", "name": "Corse", "code": corsican_department}]

    assert matches_locations(postal_code, locations) is True


@pytest.mark.parametrize(
    "postal_code",
    [
        pytest.param("", id="chaine-vide"),
        pytest.param(None, id="none"),
    ],
)
def test_a_listing_without_a_readable_postal_code_never_matches(postal_code):
    """On échoue FERMÉ sur la localisation, contrairement au prix et à la
    surface : une annonce qu'on ne sait pas situer n'a pas à être remontée.
    C'est exactement comme ça qu'une carte de remplissage sans code postal
    était autrefois notifiée comme une fausse annonce (voir
    parsers/laforet._passes_filters)."""
    locations = [{"kind": "department", "code": "33"}, {"kind": "city", "city": "X", "postalCode": "33000"}]

    assert matches_locations(postal_code, locations) is False


def test_an_empty_perimeter_list_matches_nothing():
    assert matches_locations("33000", []) is False


def test_the_first_matching_perimeter_is_enough():
    locations = [
        {"kind": "department", "code": "75"},
        {"kind": "department", "code": "33"},
    ]

    assert matches_locations("33000", locations) is True


def test_a_perimeter_with_no_usable_prefix_does_not_open_the_gate_for_the_others():
    """Corollaire de l'invariant de préfixe : une localisation incomplète dans
    la liste ne doit pas transformer le filtre en passe-partout."""
    locations = [{"kind": "city", "city": "Paris"}, {"kind": "department", "code": "33"}]

    assert matches_locations("75015", locations) is False


# ---------------------------------------------------------------------------
# sourceOverrides
# ---------------------------------------------------------------------------


def test_legacy_seloger_only_keys_move_into_source_overrides():
    """`placeIds` n'a rien à faire au premier niveau du canonique : c'est une
    valeur propre à SeLoger, pas un critère de recherche."""
    result = normalize_criteria(
        {"placeIds": ["AD08FR31096"], "locationsInBuildingExcluded": ["Ground"]}
    )

    assert "placeIds" not in result
    assert "locationsInBuildingExcluded" not in result
    assert result["sourceOverrides"] == {
        "seloger": {"placeIds": ["AD08FR31096"], "locationsInBuildingExcluded": ["Ground"]}
    }


@pytest.mark.parametrize("key", ["placeIds", "locationsInBuildingExcluded"])
@pytest.mark.parametrize("empty", [None, [], "", {}])
def test_an_empty_legacy_seloger_key_creates_no_override(key, empty):
    assert "sourceOverrides" not in normalize_criteria({key: empty})


def test_legacy_seloger_keys_are_merged_into_an_existing_seloger_override():
    result = normalize_criteria(
        {
            "sourceOverrides": {"seloger": {"placeIds": ["OLD"]}, "laforet": {"foo": "bar"}},
            "locationsInBuildingExcluded": ["Ground"],
        }
    )

    assert result["sourceOverrides"] == {
        "seloger": {"placeIds": ["OLD"], "locationsInBuildingExcluded": ["Ground"]},
        "laforet": {"foo": "bar"},
    }


def test_a_legacy_top_level_key_overwrites_the_same_key_already_in_the_override():
    result = normalize_criteria(
        {"sourceOverrides": {"seloger": {"placeIds": ["OLD"]}}, "placeIds": ["NEW"]}
    )

    assert result["sourceOverrides"]["seloger"]["placeIds"] == ["NEW"]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("nope", id="non-dict"),
        pytest.param({}, id="dict-vide"),
        pytest.param({"seloger": {}}, id="surcharge-vide"),
        pytest.param({"seloger": "nope"}, id="surcharge-non-dict"),
        pytest.param({"seloger": None}, id="surcharge-none"),
    ],
)
def test_malformed_source_overrides_are_dropped_at_normalization(raw):
    assert "sourceOverrides" not in normalize_criteria({"sourceOverrides": raw, "priceMax": 900})


def test_normalization_copies_the_override_dicts_instead_of_aliasing_the_input():
    """Les critères normalisés sont réutilisés à chaque scrape : partager les
    dicts imbriqués avec le payload d'entrée laisserait une traduction de
    source les modifier à distance."""
    payload = {"sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096"]}}}

    result = normalize_criteria(payload)
    result["sourceOverrides"]["seloger"]["placeIds"].append("MUTATED")

    assert result["sourceOverrides"]["seloger"] is not payload["sourceOverrides"]["seloger"]


def test_the_legacy_order_key_is_dropped():
    """`order: DateDesc` était un détail SeLoger ; un tracker veut toujours les
    annonces les plus récentes, la source le fixe elle-même."""
    result = normalize_criteria({"order": "DateDesc", "priceMax": 900})

    assert result == {"priceMax": 900}


def test_source_overrides_reads_only_the_source_asked_for():
    criteria = {"sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096"]}}}

    assert source_overrides(criteria, "seloger") == {"placeIds": ["AD08FR31096"]}
    assert source_overrides(criteria, "laforet") == {}


@pytest.mark.parametrize(
    "criteria",
    [
        pytest.param({}, id="aucune-surcharge"),
        pytest.param({"sourceOverrides": None}, id="none"),
        pytest.param({"sourceOverrides": "nope"}, id="non-dict"),
        pytest.param({"sourceOverrides": {"seloger": "nope"}}, id="surcharge-non-dict"),
        pytest.param({"sourceOverrides": {"seloger": None}}, id="surcharge-none"),
        pytest.param({"sourceOverrides": ["seloger"]}, id="liste"),
    ],
)
def test_source_overrides_returns_an_empty_dict_on_anything_malformed(criteria):
    """Un parser ne doit jamais avoir à se défendre : il reçoit toujours un
    dict, même quand la base contient n'importe quoi."""
    assert source_overrides(criteria, "seloger") == {}


def test_with_source_override_does_not_mutate_the_original_criteria():
    """Les critères sont partagés entre toutes les sources d'une recherche
    pendant un scrape : la traduction de l'une ne doit jamais fuiter dans
    celle d'une autre."""
    criteria = {"sourceOverrides": {"laforet": {"foo": "bar"}}}

    updated = with_source_override(criteria, "seloger", {"placeIds": ["X"]})

    assert criteria == {"sourceOverrides": {"laforet": {"foo": "bar"}}}
    assert updated["sourceOverrides"] == {"laforet": {"foo": "bar"}, "seloger": {"placeIds": ["X"]}}


def test_with_source_override_deep_copies_the_other_sources_overrides():
    """Le test d'aliasing : muter la copie ne doit rien changer à l'original,
    même dans les dicts imbriqués des AUTRES sources."""
    criteria = {"sourceOverrides": {"laforet": {"foo": "bar"}}}

    updated = with_source_override(criteria, "seloger", {"placeIds": ["X"]})
    updated["sourceOverrides"]["laforet"]["foo"] = "MUTATED"

    assert criteria["sourceOverrides"]["laforet"]["foo"] == "bar"


def test_with_source_override_leaves_the_rest_of_the_criteria_alone():
    criteria = make_criteria()

    updated = with_source_override(criteria, "seloger", {"placeIds": ["X"]})

    assert {k: v for k, v in updated.items() if k != "sourceOverrides"} == criteria


def test_with_source_override_updates_an_existing_override_of_the_same_source():
    criteria = {"sourceOverrides": {"seloger": {"placeIds": ["OLD"], "keepMe": 1}}}

    updated = with_source_override(criteria, "seloger", {"placeIds": ["NEW"]})

    assert updated["sourceOverrides"]["seloger"] == {"placeIds": ["NEW"], "keepMe": 1}
    assert criteria["sourceOverrides"]["seloger"]["placeIds"] == ["OLD"]


@pytest.mark.parametrize("values", [{}, None])
def test_with_source_override_adds_nothing_when_there_is_nothing_to_add(values):
    assert with_source_override({"priceMax": 900}, "seloger", values) == {"priceMax": 900}


def test_with_empty_values_the_returned_copy_still_shares_the_existing_overrides():
    # BUG : sur le chemin « rien à ajouter », `with_source_override` renvoie un
    # dict de premier niveau copié mais dont `sourceOverrides` est l'OBJET de
    # l'appelant (core/criteria.py:399-407 : la copie profonde calculée dans
    # `overrides` n'est réaffectée que si `values` est non vide). La fonction ne
    # mute rien elle-même, donc aucun bug n'est visible aujourd'hui, mais la
    # promesse « la traduction d'une source ne fuite pas dans celle d'une
    # autre » ne tient que par chance. Correct : réaffecter `overrides` dans
    # tous les cas où `criteria` en contenait.
    criteria = {"sourceOverrides": {"laforet": {"foo": "bar"}}}

    updated = with_source_override(criteria, "seloger", {})

    assert updated["sourceOverrides"] is criteria["sourceOverrides"]


# ---------------------------------------------------------------------------
# transit : sélections de transports en commun (issue #28)
# ---------------------------------------------------------------------------


class TestNormalizeTransit:
    """La clé `transit` est du vocabulaire NEUTRE : aucun parser ne la lit.
    Sa normalisation doit être aussi prévisible que celle des locations —
    entrée illisible écartée, jamais devinée, rayon borné à la liste du
    formulaire."""

    @pytest.mark.parametrize("payload", [None, {}, "x", 42])
    def test_an_absent_or_unreadable_transit_reads_as_empty(self, payload):
        """« absence → [] » au sens lecture (normalize_transit) ; la clé
        n'est pas pour autant INVENTÉE dans les critères normalisés."""
        assert normalize_transit(payload) == []
        assert normalize_criteria({"locations": [make_city_location()], "transit": payload}) == {
            "locations": [make_city_location()]
        }

    def test_a_valid_selection_is_normalized_field_by_field(self):
        result = normalize_criteria({
            "transit": [{
                "mode": "metro",
                "line_id": " IDFM:C01388 ",
                "stop_ids": ["STIF:StopArea:SP:43135:", "OTHER:STOP"],
                "radius_m": 500,
            }],
        })

        assert result["transit"] == [{
            "mode": "metro",
            "line_id": "IDFM:C01388",
            "stop_ids": ["OTHER:STOP", "STIF:StopArea:SP:43135:"],
            "radius_m": 500,
        }]

    @pytest.mark.parametrize(
        ("raw_radius", "expected"),
        [(None, 1000), ("", 1000), ("1000", 1000), (750, 1000), (2000, 2000), ("500", 500)],
        ids=["absent", "vide", "chaine", "hors-liste", "borne-haute", "borne-basse"],
    )
    def test_the_radius_falls_back_to_the_default_when_not_in_the_offered_list(self, raw_radius, expected):
        selection = normalize_transit({"transit": [{"line_id": "L", "radius_m": raw_radius}]})
        assert selection[0]["radius_m"] == expected

    def test_stop_ids_are_coerced_deduplicated_and_sorted(self):
        selection = normalize_transit({
            "transit": [{"line_id": "L", "stop_ids": ["B", "A", "A", "", None, 3]},
        ]})

        assert selection[0]["stop_ids"] == ["3", "A", "B"]

    def test_empty_stop_ids_means_whole_line_and_omits_the_key(self):
        for stop_ids in ([], ["", "  "]):
            selection = normalize_transit({"transit": [{"line_id": "L", "stop_ids": stop_ids}]})
            assert selection[0] == {"line_id": "L", "radius_m": 1000}

    def test_an_unknown_mode_keeps_the_selection_but_omits_the_key(self):
        """Un mode illisible n'invalide pas la ligne (l'expansion ne s'en sert
        pas) mais il n'est jamais deviné non plus."""
        selection = normalize_transit({"transit": [{"line_id": "L", "mode": "funiculaire"}]})
        assert selection == [{"line_id": "L", "radius_m": 1000}]

    def test_an_entry_without_line_id_is_dropped_never_guessed(self):
        assert normalize_transit({"transit": [{"mode": "metro", "radius_m": 500}]}) == []

    @pytest.mark.parametrize("entry", ["metro", 42, [], None])
    def test_non_mapping_entries_are_dropped(self, entry):
        assert normalize_transit({"transit": [entry]}) == []

    def test_two_entries_on_the_same_line_are_deduplicated_first_wins(self):
        selections = normalize_transit({"transit": [
            {"line_id": "L1", "radius_m": 500},
            {"line_id": " L1 ", "stop_ids": ["S"], "radius_m": 2000},
        ]})

        assert selections == [{"line_id": "L1", "radius_m": 500}]

    def test_several_lines_are_all_kept_multi_selection(self):
        selections = normalize_transit({"transit": [
            {"line_id": "L1"}, {"line_id": "L2"}, {"line_id": "L3"},
        ]})

        assert [s["line_id"] for s in selections] == ["L1", "L2", "L3"]

    def test_normalization_is_idempotent(self):
        once = normalize_criteria({"transit": [make_transit_selection()]})
        assert normalize_criteria(once) == once

    def test_unknown_keys_are_stripped_from_the_canonical_entry(self):
        """line_label ne sert qu'à l'affichage front : le canonique ne le
        stocke pas."""
        selection = normalize_transit({"transit": [make_transit_selection(line_label="Métro 14")]})
        assert "line_label" not in selection[0]
        assert set(selection[0]) == {"line_id", "mode", "stop_ids", "radius_m"}

    def test_has_transit_accepts_a_transit_only_search(self):
        assert has_transit({"transit": [{"line_id": "L"}]}) is True
        assert has_transit({"transit": [{"radius_m": 500}]}) is False
        assert has_transit({}) is False

    def test_transit_lives_alongside_locations_without_touching_them(self):
        criteria = {"locations": [make_city_location()], "transit": [{"line_id": "L"}]}
        normalized = normalize_criteria(criteria)

        assert set(normalized) == {"locations", "transit"}
        assert normalized["locations"] == [make_city_location()]
