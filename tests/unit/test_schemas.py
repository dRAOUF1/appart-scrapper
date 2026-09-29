"""Tests de `core/schemas.py` — la validation des payloads utilisateur.

Ce module est la frontière entre le monde extérieur (formulaire web, API REST)
et le vocabulaire canonique. Deux responsabilités, et seulement deux :

1. refuser les types absurdes AVANT normalisation, avec un message français
   affiché tel quel au client (contrat de chaîne) ;
2. ne jamais laisser sortir autre chose que du canonique — tout passe par
   `normalize_criteria`.

Ce qui relève de la normalisation elle-même (alias de l'ancien vocabulaire,
tri des `rooms`, `sourceOverrides`...) est testé dans test_criteria.py et n'est
pas rejoué ici : seul le fait *que* la sortie soit normalisée est vérifié.
"""

from __future__ import annotations

import pytest

from core.schemas import SearchCriteria, validate_criteria, validate_scrape_interval
from tests.helpers.factories import make_city_location, make_criteria

# ---------------------------------------------------------------------------
# validate_criteria : entrées dégénérées
# ---------------------------------------------------------------------------


def test_none_criteria_become_an_empty_dict_rather_than_an_error():
    """Une recherche peut être créée sans critères (ils seront ajoutés ensuite) :
    `None` n'est pas une erreur de saisie, c'est l'absence de saisie."""
    assert validate_criteria(None) == {}


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not-a-dict", id="chaine"),
        pytest.param("", id="chaine-vide"),
        pytest.param([], id="liste-vide"),
        pytest.param([{"city": "Paris"}], id="liste-de-dicts"),
        pytest.param(42, id="entier"),
        pytest.param(0, id="zero"),
        pytest.param(True, id="booleen"),
        pytest.param(("city", "Paris"), id="tuple"),
    ],
)
def test_anything_that_is_not_a_mapping_is_refused_with_the_exact_client_message(payload):
    """Le message part tel quel dans la réponse HTTP : c'est un contrat."""
    with pytest.raises(ValueError, match=r"^criteria doit être un objet JSON$"):
        validate_criteria(payload)


def test_an_empty_dict_is_accepted_and_normalizes_to_an_empty_dict():
    """`{}` est une saisie valide mais vide — à distinguer d'un type invalide."""
    assert validate_criteria({}) == {}


@pytest.mark.parametrize("field", ["priceMin", "priceMax", "surfaceMin", "surfaceMax", "spaceMin", "spaceMax"])
def test_numeric_bounds_cannot_be_negative(field):
    with pytest.raises(ValueError, match="Critères invalides"):
        validate_criteria({field: -1})


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"priceMin": 1200, "priceMax": 800}, "borne minimale de prix"),
        ({"surfaceMin": 80, "surfaceMax": 40}, "borne minimale de surface"),
        ({"rooms": [0]}, "nombre de pièces"),
        ({"bedrooms": [-1]}, "nombre de chambres"),
    ],
)
def test_incoherent_ranges_and_counts_are_rejected(payload, message):
    with pytest.raises(ValueError, match=message):
        validate_criteria(payload)


# ---------------------------------------------------------------------------
# validate_criteria : erreurs de type sur les champs connus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected_message_fragment"),
    [
        pytest.param({"priceMax": "not-a-number"}, "valid integer", id="prix-non-numerique"),
        pytest.param({"priceMax": 1500.5}, "fractional part", id="prix-float-fractionnaire"),
        pytest.param({"surfaceMin": "beaucoup"}, "valid integer", id="surface-non-numerique"),
        pytest.param({"placeIds": "AD08FR12345"}, "valid list", id="placeids-chaine-nue"),
        pytest.param({"locations": "paris"}, "valid list", id="locations-chaine-nue"),
        pytest.param({"locations": [["Paris"]]}, "valid dictionary", id="locations-liste-de-listes"),
        pytest.param({"rooms": "abc"}, "valid list", id="rooms-chaine-nue"),
        pytest.param({"propertyTypes": [42]}, "valid string", id="property-types-non-str"),
        pytest.param({"transaction": ["rent"]}, "valid string", id="transaction-en-liste"),
        pytest.param({"sourceOverrides": "seloger"}, "valid dictionary", id="source-overrides-chaine"),
    ],
)
def test_a_badly_typed_known_field_is_refused_before_normalization(payload, expected_message_fragment):
    """Sans ce garde-fou, `normalize_criteria` écarterait silencieusement la
    valeur mal typée et l'utilisateur croirait son critère enregistré."""
    with pytest.raises(ValueError, match="Critères invalides: "):
        validate_criteria(payload)

    with pytest.raises(ValueError, match=expected_message_fragment):
        validate_criteria(payload)


def test_only_the_first_validation_error_is_reported():
    """Le message est fait pour un bandeau d'erreur, pas pour un rapport :
    `e.errors()[0]['msg']` — une seule phrase, même si trois champs sont
    fautifs. Fige le choix, qui est visible par l'utilisateur."""
    with pytest.raises(ValueError, match=r"^Critères invalides: ") as excinfo:
        validate_criteria({"priceMax": "abc", "priceMin": "xyz", "surfaceMax": "zzz"})

    message = str(excinfo.value)

    assert message == "Critères invalides: Input should be a valid integer, unable to parse string as an integer"
    # Une seule occurrence : les deux autres erreurs ne sont pas concaténées.
    assert message.count("Input should be") == 1


def test_the_original_validation_error_is_kept_as_the_cause():
    """`raise ... from e` : le détail pydantic complet reste dans la trace pour
    le diagnostic serveur, sans polluer le message client."""
    from pydantic import ValidationError

    with pytest.raises(ValueError, match=r"^Critères invalides: ") as excinfo:
        validate_criteria({"priceMax": "abc"})

    assert isinstance(excinfo.value.__cause__, ValidationError)


# ---------------------------------------------------------------------------
# validate_criteria : coercitions tolérées
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(1500, 1500, id="int"),
        pytest.param("1500", 1500, id="chaine-numerique-coercee-par-pydantic"),
        pytest.param(1500.0, 1500, id="float-entier"),
    ],
)
def test_pydantic_coerces_a_numeric_price_that_has_no_fractional_part(raw, expected):
    """Le formulaire web envoie tout en chaînes : refuser `"1500"` casserait la
    création de recherche depuis l'interface."""
    assert validate_criteria({"priceMax": raw})["priceMax"] == expected


# ---------------------------------------------------------------------------
# validate_criteria : la sortie est toujours canonique
# ---------------------------------------------------------------------------


def test_canonical_criteria_survive_validation_unchanged():
    criteria = make_criteria()

    assert validate_criteria(criteria) == criteria


def test_the_output_always_goes_through_normalize_criteria():
    """Ce qui entre dans l'ancien vocabulaire ressort en canonique : c'est ce
    qui est ÉCRIT en base pour toute recherche créée ou modifiée. Le détail des
    traductions appartient à test_criteria.py — ici on vérifie seulement que la
    normalisation est bien appliquée en sortie."""
    result = validate_criteria(
        {"city": "Poitiers", "postalCode": "86000", "distributionTypes": ["Sale"], "spaceMin": 40}
    )

    assert result == {
        "locations": [{"kind": "city", "city": "Poitiers", "postalCode": "86000"}],
        "transaction": "buy",
        "surfaceMin": 40,
    }
    # Aucune clé de l'ancien vocabulaire ne subsiste.
    assert not {"city", "postalCode", "distributionTypes", "spaceMin"} & set(result)


def test_validation_is_idempotent_over_its_own_output():
    """Une recherche rééditée repasse par ici : valider deux fois ne doit pas
    faire dériver les critères."""
    once = validate_criteria({"city": "Poitiers", "postalCode": "86000", "estateTypes": ["House"]})

    assert validate_criteria(once) == once


def test_validation_does_not_mutate_the_payload_it_was_given():
    """Les routes réutilisent le payload après validation (log, réponse) : le
    modifier en place produirait des incohérences difficiles à voir."""
    payload = {"city": "Poitiers", "postalCode": "86000", "spaceMin": 40}
    before = dict(payload)

    validate_criteria(payload)

    assert payload == before


# ---------------------------------------------------------------------------
# validate_criteria : extra="allow"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exotic_key",
    [
        pytest.param("couleurDesMurs", id="cle-inventee"),
        pytest.param("__proto__", id="cle-piegeuse"),
        pytest.param("criteria", id="cle-homonyme-du-champ-parent"),
        pytest.param("model_config", id="cle-homonyme-d-un-attribut-pydantic"),
    ],
)
def test_an_unknown_key_neither_crashes_nor_reaches_the_stored_criteria(exotic_key):
    """`extra="allow"` est nécessaire (les clés propres à une source passent
    par là), mais rien d'inconnu ne doit finir en base : `normalize_criteria`
    est la liste blanche finale."""
    result = validate_criteria({exotic_key: "n'importe quoi", "locations": [make_city_location()]})

    assert exotic_key not in result
    assert set(result) == {"locations"}


def test_source_specific_legacy_keys_are_preserved_as_source_overrides():
    """`placeIds` n'est pas du canonique mais ne doit pas être perdu : c'est le
    repli manuel de SeLoger quand la résolution automatique échoue. Il traverse
    donc la validation grâce à `extra="allow"`."""
    result = validate_criteria({"placeIds": ["AD08FR31096"], "locationsInBuildingExcluded": ["Ground"]})

    assert result["sourceOverrides"]["seloger"] == {
        "placeIds": ["AD08FR31096"],
        "locationsInBuildingExcluded": ["Ground"],
    }


def test_the_model_declares_extra_allow_so_a_new_source_key_needs_no_schema_change():
    """Invariant explicite : ajouter une source ne doit pas demander de toucher
    ce schéma (voir le docstring de SearchCriteria)."""
    assert SearchCriteria.model_config["extra"] == "allow"


# ---------------------------------------------------------------------------
# validate_scrape_interval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(10, 10, id="int"),
        pytest.param("5", 5, id="chaine-numerique"),
        pytest.param(" 7 ", 7, id="chaine-avec-espaces"),
        pytest.param(5.9, 5, id="float-tronque-vers-le-bas"),
    ],
)
def test_a_parsable_interval_is_returned_as_an_int(raw, expected):
    assert validate_scrape_interval(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(None, id="none"),
        pytest.param("abc", id="chaine-non-numerique"),
        pytest.param("", id="chaine-vide"),
        pytest.param("5 minutes", id="chaine-avec-unite"),
        pytest.param("5.5", id="float-en-chaine"),
        pytest.param([], id="liste"),
        pytest.param([5], id="liste-a-un-element"),
        pytest.param({}, id="dict"),
    ],
)
def test_an_unparsable_interval_is_refused_with_the_exact_client_message(raw):
    with pytest.raises(ValueError, match=r"^scrape_interval doit être un entier$"):
        validate_scrape_interval(raw)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1, id="borne-basse-incluse"),
        pytest.param(2, id="juste-au-dessus-de-la-borne-basse"),
        pytest.param(720, id="milieu-de-plage"),
        pytest.param(1439, id="juste-en-dessous-de-la-borne-haute"),
        pytest.param(1440, id="borne-haute-incluse-24h"),
    ],
)
def test_both_interval_bounds_are_inclusive(value):
    """1 minute et 1440 minutes (24 h) sont des réglages valides : les bornes
    sont inclusives des deux côtés."""
    assert validate_scrape_interval(value) == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0, id="zero-juste-sous-la-borne-basse"),
        pytest.param(-1, id="negatif"),
        pytest.param(-1440, id="tres-negatif"),
        pytest.param(1441, id="juste-au-dessus-de-la-borne-haute"),
        pytest.param(999999, id="tres-grand"),
    ],
)
def test_an_out_of_range_interval_is_refused_and_the_message_states_the_bounds(value):
    """Le message doit dire quelles bornes respecter, sinon l'utilisateur
    corrige à l'aveugle."""
    with pytest.raises(ValueError, match=r"^scrape_interval doit être entre 1 et 1440 minutes$"):
        validate_scrape_interval(value)


@pytest.mark.parametrize(
    ("value", "minimum", "maximum", "valid"),
    [
        pytest.param(5, 5, 10, True, id="borne-basse-personnalisee-incluse"),
        pytest.param(10, 5, 10, True, id="borne-haute-personnalisee-incluse"),
        pytest.param(4, 5, 10, False, id="sous-la-borne-basse-personnalisee"),
        pytest.param(11, 5, 10, False, id="au-dessus-de-la-borne-haute-personnalisee"),
    ],
)
def test_the_bounds_are_overridable_and_reported_in_the_message(value, minimum, maximum, valid):
    if valid:
        assert validate_scrape_interval(value, minimum=minimum, maximum=maximum) == value
        return
    with pytest.raises(ValueError, match=rf"entre {minimum} et {maximum} minutes"):
        validate_scrape_interval(value, minimum=minimum, maximum=maximum)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(True, 1, id="true-vaut-1-minute"),
        pytest.param(False, None, id="false-vaut-0-donc-hors-bornes"),
    ],
)
def test_a_boolean_interval_is_silently_read_as_its_integer_value(value, expected):
    """# BUG : divergence avec `core.criteria._to_int`, qui REFUSE explicitement
    les booléens (`isinstance(value, bool)` -> None) pour éviter qu'un `True`
    parasite devienne un critère de 1 €. Ici `int(True)` passe : un payload
    JSON `{"scrape_interval": true}` est accepté et programme un scrape toutes
    les minutes — 1440 scrapes par jour pour une recherche, sans que
    l'utilisateur ait demandé quoi que ce soit. `False` n'échoue que par
    accident, parce que 0 sort des bornes.

    Le comportement ACTUEL est figé ici ; le correctif serait d'aligner sur
    `_to_int` (rejeter `bool` avant l'appel à `int()`).
    """
    if expected is None:
        with pytest.raises(ValueError, match="entre 1 et 1440 minutes"):
            validate_scrape_interval(value)
        return
    assert validate_scrape_interval(value) == expected
