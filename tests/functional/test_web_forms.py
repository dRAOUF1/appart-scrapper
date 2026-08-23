"""Tests des helpers de parsing de formulaire de `routes/web.py`.

Ces fonctions sont pures : elles transforment ce que le navigateur a envoyé en
critères canoniques, sans toucher à la base ni au réseau. C'est la frontière
entre une saisie utilisateur libre et le vocabulaire interne — donc l'endroit
où une donnée mal comprise se propage silencieusement jusqu'aux URLs des
sources.

Elles vivent dans tests/functional/ parce qu'elles appartiennent à la couche
web (un `MultiDict` Flask est leur entrée naturelle), mais elles ne démarrent
aucune application.
"""

from __future__ import annotations

import json

import pytest
from werkzeug.datastructures import MultiDict

from routes.web import (
    _form_list,
    _location_from_free_text,
    _parse_listing_filters,
    _parse_locations_from_form,
    _parse_search_criteria_from_form,
    _parse_source_overrides_from_form,
    _validate_sources_criteria,
    _validation_error_message,
)

# ---------------------------------------------------------------------------
# _form_list
# ---------------------------------------------------------------------------

class TestFormList:
    """Le formulaire envoie des champs répétés (une ligne de périmètre = une
    valeur). Flask les expose via `MultiDict.getlist`, mais les tests et les
    appels internes passent des dicts simples : les deux doivent marcher."""

    def test_a_multidict_yields_every_repeated_value(self):
        form = MultiDict([("rooms", "2"), ("rooms", "3"), ("rooms", "4")])

        assert _form_list(form, "rooms") == ["2", "3", "4"]

    def test_a_plain_dict_with_a_list_value_is_returned_as_is(self):
        assert _form_list({"rooms": ["2", "3"]}, "rooms") == ["2", "3"]

    def test_a_plain_dict_with_a_scalar_value_is_wrapped(self):
        """Sans cet emballage, une chaîne serait itérée caractère par caractère
        et « 12 » deviendrait deux valeurs."""
        assert _form_list({"rooms": "12"}, "rooms") == ["12"]

    @pytest.mark.parametrize("form", [MultiDict(), {}], ids=["multidict", "dict"])
    def test_a_missing_key_yields_an_empty_list(self, form):
        assert _form_list(form, "absent") == []


# ---------------------------------------------------------------------------
# _location_from_free_text
# ---------------------------------------------------------------------------

class TestLocationFromFreeText:
    """Filet de sécurité pour une ligne remplie à la main, sans passer par les
    suggestions de l'autocomplete."""

    @pytest.mark.parametrize(
        ("text", "city"),
        [
            pytest.param("Poitiers 86000", "Poitiers", id="ville-espace-code"),
            pytest.param("Poitiers (86000)", "Poitiers", id="code-entre-parentheses"),
            pytest.param("86000 Poitiers", "Poitiers", id="code-en-premier"),
            pytest.param("Poitiers, 86000", "Poitiers", id="separe-par-une-virgule"),
            pytest.param("Le Kremlin-Bicêtre 94270", "Le Kremlin-Bicêtre", id="tiret-conserve-dans-le-nom"),
            pytest.param("  Poitiers   86000  ", "Poitiers", id="espaces-superflus"),
        ],
    )
    def test_a_city_and_a_postal_code_are_extracted(self, text, city):
        assert _location_from_free_text(text) == {
            "kind": "city",
            "city": city,
            "postalCode": _expected_postal(text),
        }

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("Poitiers", id="sans-code-postal"),
            pytest.param("", id="chaine-vide"),
            pytest.param("   ", id="espaces-seuls"),
            pytest.param("un texte sans chiffres", id="texte-libre"),
            pytest.param("8600", id="code-a-4-chiffres"),
        ],
    )
    def test_text_without_a_usable_postal_code_yields_nothing(self, text):
        """Sans code postal, il n'y a rien à chercher — et surtout pas de
        commune à deviner : plusieurs peuvent porter le même nom."""
        assert _location_from_free_text(text) is None

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("86000", id="code-seul"),
            pytest.param("(86000)", id="code-seul-parenthese"),
            pytest.param(" 86000 ", id="code-seul-espaces"),
        ],
    )
    def test_a_postal_code_without_a_city_yields_nothing(self, text):
        """La ville disparaît une fois le code retiré : la ligne est vide de
        sens, mieux vaut l'écarter que de créer un périmètre sans nom."""
        assert _location_from_free_text(text) is None

    def test_the_resulting_location_carries_no_insee_code(self):
        """Une saisie libre ne donne jamais de code INSEE. Les sources qui en
        ont besoin le signalent elles-mêmes (SeLogerParser.cannot_search_reason)
        au lieu de partir sur une résolution approximative."""
        location = _location_from_free_text("Poitiers 86000")

        assert "inseeCode" not in location

    def test_a_six_digit_number_is_not_a_postal_code(self):
        """`\\b(\\d{5})\\b` est ancré : 860000 ne contient pas un code postal
        suivi d'un zéro."""
        assert _location_from_free_text("Ville 860000") is None


def _expected_postal(text: str) -> str:
    """Le code postal attendu pour les cas ci-dessus (5 chiffres consécutifs)."""
    import re

    return re.search(r"\b(\d{5})\b", text).group(1)


# ---------------------------------------------------------------------------
# _parse_locations_from_form
# ---------------------------------------------------------------------------

PARIS_PAYLOAD = {
    "kind": "city",
    "city": "Paris",
    "postalCode": "75013",
    "inseeCode": "75113",
    "label": "Paris 13e (75013)",
}

REGION_PAYLOAD = {
    "kind": "region",
    "code": "75",
    "name": "Nouvelle-Aquitaine",
    "departments": ["33", "40"],
    "label": "Nouvelle-Aquitaine (région)",
}


def _form(payloads: list[str], typed: list[str]) -> MultiDict:
    items = [("location_payload", p) for p in payloads]
    items += [("location_city", t) for t in typed]
    return MultiDict(items)


class TestParseLocationsFromForm:
    def test_a_payload_is_taken_as_a_canonical_location(self):
        """L'autocomplete écrit dans le champ caché une entrée déjà au format
        canonique : elle est reprise telle quelle, sans réinterprétation."""
        form = _form([json.dumps(PARIS_PAYLOAD)], ["Paris 13e (75013)"])

        assert _parse_locations_from_form(form) == [
            {"kind": "city", "city": "Paris", "postalCode": "75013", "inseeCode": "75113"}
        ]

    def test_the_display_label_is_dropped(self):
        """`label` n'est qu'un texte d'affichage. La normalisation l'écarterait
        de toute façon, mais autant ne pas le transporter jusque-là."""
        form = _form([json.dumps(REGION_PAYLOAD)], ["Nouvelle-Aquitaine (région)"])

        assert _parse_locations_from_form(form) == [
            {"kind": "region", "code": "75", "name": "Nouvelle-Aquitaine", "departments": ["33", "40"]}
        ]

    def test_a_line_without_payload_falls_back_to_the_typed_text(self):
        form = _form([""], ["Poitiers 86000"])

        assert _parse_locations_from_form(form) == [
            {"kind": "city", "city": "Poitiers", "postalCode": "86000"}
        ]

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("{pas du json", id="json-malforme"),
            pytest.param("[]", id="json-valide-mais-liste"),
            pytest.param('"une chaine"', id="json-valide-mais-chaine"),
            pytest.param("null", id="json-null"),
        ],
    )
    def test_an_unusable_payload_falls_back_to_the_typed_text(self, payload):
        """Le payload vient du JavaScript : s'il est corrompu, la saisie visible
        de l'utilisateur reste la source de vérité, plutôt que de perdre la
        ligne."""
        form = _form([payload], ["Poitiers 86000"])

        assert _parse_locations_from_form(form) == [
            {"kind": "city", "city": "Poitiers", "postalCode": "86000"}
        ]

    def test_a_line_with_neither_payload_nor_usable_text_is_dropped(self):
        form = _form(["", ""], ["", "Poitiers"])

        assert _parse_locations_from_form(form) == []

    def test_several_lines_keep_their_order(self):
        form = _form(
            [json.dumps(PARIS_PAYLOAD), "", json.dumps(REGION_PAYLOAD)],
            ["Paris 13e (75013)", "Poitiers 86000", "Nouvelle-Aquitaine (région)"],
        )

        assert [loc.get("city") or loc.get("name") for loc in _parse_locations_from_form(form)] == [
            "Paris",
            "Poitiers",
            "Nouvelle-Aquitaine",
        ]

    def test_more_typed_lines_than_payloads_are_all_read(self):
        """Les deux listes sont désalignées dès que le navigateur n'envoie pas
        de champ caché pour une ligne : la boucle va jusqu'au plus long des
        deux, sinon les dernières lignes disparaîtraient sans un mot."""
        form = _form([json.dumps(PARIS_PAYLOAD)], ["Paris 13e (75013)", "Poitiers 86000"])

        assert len(_parse_locations_from_form(form)) == 2

    def test_more_payloads_than_typed_lines_are_all_read(self):
        form = _form([json.dumps(PARIS_PAYLOAD), json.dumps(REGION_PAYLOAD)], ["Paris 13e (75013)"])

        assert len(_parse_locations_from_form(form)) == 2

    def test_an_empty_form_yields_no_location(self):
        assert _parse_locations_from_form(MultiDict()) == []


# ---------------------------------------------------------------------------
# _parse_search_criteria_from_form
# ---------------------------------------------------------------------------

class TestParseSearchCriteriaFromForm:
    def test_the_output_is_always_normalized(self):
        """Le formulaire envoie tout en chaînes. La sortie doit être du
        canonique typé, prêt à stocker — c'est `normalize_criteria` qui le
        garantit, et ce test qui empêche qu'on l'oublie."""
        form = MultiDict([
            ("location_payload", json.dumps(PARIS_PAYLOAD)),
            ("transaction", "rent"),
            ("property_types", "apartment"),
            ("price_min", "800"),
            ("price_max", "1500"),
            ("surface_min", "40"),
            ("rooms", "2"),
            ("rooms", "3"),
        ])

        criteria = _parse_search_criteria_from_form(form)

        assert criteria["priceMin"] == 800
        assert criteria["priceMax"] == 1500
        assert criteria["surfaceMin"] == 40
        assert criteria["rooms"] == [2, 3]
        assert criteria["transaction"] == "rent"
        assert criteria["propertyTypes"] == ["apartment"]

    def test_the_transaction_defaults_to_rent(self):
        """Le formulaire a toujours un bouton radio coché, mais un POST forgé
        ou un champ renommé ne doit pas produire une recherche sans
        transaction."""
        assert _parse_search_criteria_from_form(MultiDict())["transaction"] == "rent"

    @pytest.mark.parametrize("field", ["price_min", "price_max", "surface_min", "surface_max"])
    def test_a_blank_numeric_field_is_omitted_not_zeroed(self, field):
        """Un champ vide veut dire « pas de contrainte ». Le transformer en 0
        donnerait « prix maximum 0 € » et zéro résultat."""
        criteria = _parse_search_criteria_from_form(MultiDict([(field, "   ")]))

        assert not any(key.startswith(("price", "surface")) for key in criteria)

    def test_an_unparsable_numeric_value_is_dropped_by_normalization(self):
        """« abc » n'est pas un prix : la normalisation l'écarte plutôt que de
        laisser passer une chaîne dans une comparaison numérique."""
        criteria = _parse_search_criteria_from_form(MultiDict([("price_max", "abc")]))

        assert "priceMax" not in criteria

    def test_locations_are_absent_when_no_line_is_usable(self):
        """La clé n'est pas posée à vide : `locations: []` et l'absence de clé
        ne veulent pas dire la même chose pour les parsers."""
        assert "locations" not in _parse_search_criteria_from_form(MultiDict())


# ---------------------------------------------------------------------------
# _parse_source_overrides_from_form
# ---------------------------------------------------------------------------

class TestParseSourceOverridesFromForm:
    def test_a_seloger_place_id_typed_by_hand_lands_in_source_overrides(self):
        """La saisie libre d'une source ne se mélange jamais aux critères : elle
        est rangée sous `sourceOverrides`, seule échappatoire assumée au
        vocabulaire canonique."""
        form = MultiDict([("override_seloger", "AD08FR31096")])

        assert _parse_source_overrides_from_form(form) == {"seloger": {"placeIds": ["AD08FR31096"]}}

    @pytest.mark.parametrize("raw", ["", "   "], ids=["vide", "espaces"])
    def test_a_blank_override_is_ignored(self, raw):
        assert _parse_source_overrides_from_form(MultiDict([("override_seloger", raw)])) == {}

    def test_a_source_without_manual_override_is_skipped(self):
        """Laforêt ne déclare pas de saisie libre : un champ `override_laforet`
        forgé dans le POST ne doit rien produire."""
        form = MultiDict([("override_laforet", "n'importe quoi")])

        assert "laforet" not in _parse_source_overrides_from_form(form)

    def test_an_override_the_parser_rejects_is_not_stored(self):
        """`parse_manual_override` renvoie {} quand il ne reconnaît rien : on
        ne stocke pas une surcharge vide, qui masquerait la résolution
        automatique sans rien apporter."""
        form = MultiDict([("override_seloger", ",,,")])

        assert _parse_source_overrides_from_form(form) == {}


# ---------------------------------------------------------------------------
# _validate_sources_criteria / _validation_error_message
# ---------------------------------------------------------------------------

class TestValidateSourcesCriteria:
    def test_a_valid_criteria_set_passes_for_every_source(self):
        criteria = {"locations": [{"kind": "city", "city": "Paris", "postalCode": "75013",
                                   "inseeCode": "75113"}]}

        results = _validate_sources_criteria(["seloger", "laforet"], criteria)

        assert [r["ok"] for r in results] == [True, True]
        assert [r["id"] for r in results] == ["seloger", "laforet"]

    def test_an_unknown_source_is_reported_without_raising(self):
        """Une source disparue du registre (renommée, retirée) ne doit pas faire
        planter la page de création : elle est signalée comme invalide."""
        results = _validate_sources_criteria(["leboncoin"], {})

        assert results == [{"id": "leboncoin", "name": "leboncoin", "ok": False, "reason": "Source inconnue"}]

    def test_the_reason_comes_from_the_source_itself(self):
        """Pas de « critères invalides » générique : chaque source dit ce qui
        lui manque, ce qui est la seule information actionnable."""
        results = _validate_sources_criteria(["laforet"], {})

        assert results[0]["ok"] is False
        assert results[0]["reason"]
        assert results[0]["name"] == "Laforêt"


class TestValidationErrorMessage:
    def test_only_failing_sources_appear(self):
        results = [
            {"id": "a", "name": "Alpha", "ok": True, "reason": ""},
            {"id": "b", "name": "Bêta", "ok": False, "reason": "pas de lieu"},
        ]

        message = _validation_error_message(results)

        assert "Alpha" not in message
        assert "Bêta" in message

    def test_a_reason_that_already_names_its_source_is_not_prefixed(self):
        """Évite le « SeLoger : SeLoger ne peut pas… » : certaines raisons
        (l'aide de SeLoger sur le Place ID) nomment déjà leur source."""
        results = [{"id": "seloger", "name": "SeLoger", "ok": False,
                    "reason": "SeLoger ne peut pas chercher sans code INSEE"}]

        assert _validation_error_message(results) == "SeLoger ne peut pas chercher sans code INSEE"

    def test_several_failures_are_joined(self):
        results = [
            {"id": "a", "name": "Alpha", "ok": False, "reason": "raison A"},
            {"id": "b", "name": "Bêta", "ok": False, "reason": "raison B"},
        ]

        message = _validation_error_message(results)

        assert "raison A" in message
        assert "raison B" in message
        assert " / " in message


# ---------------------------------------------------------------------------
# _parse_listing_filters — le parseur de filtres de la page annonces
# ---------------------------------------------------------------------------

FILTER_QUERY_STRINGS = [
    pytest.param({}, id="aucun-filtre"),
    pytest.param({"q": "loft"}, id="recherche-plein-texte"),
    pytest.param({"price_min": "800", "price_max": "1500"}, id="fourchette-de-prix"),
    pytest.param({"surface_min": "40", "surface_max": "90"}, id="fourchette-de-surface"),
    pytest.param({"rooms_min": "2", "rooms_max": "4"}, id="fourchette-de-pieces"),
    pytest.param({"city": "Paris", "district": "13e", "zip_code": "75013"}, id="localisation"),
    pytest.param({"property_type": "apartment", "agency": "Foncia"}, id="type-et-agence"),
    pytest.param({"epc": "C", "ges": "B"}, id="diagnostics"),
    pytest.param({"is_private": "true"}, id="particulier"),
    pytest.param({"is_new": "false"}, id="pas-neuf"),
    pytest.param({"price_min": "abc"}, id="valeur-illisible"),
    pytest.param({"q": "'; DROP TABLE listings; --"}, id="charge-hostile"),
]


class TestParseListingFilters:
    @pytest.mark.parametrize("args", FILTER_QUERY_STRINGS)
    def test_any_query_string_parses_into_a_dict_without_raising(self, args):
        """`_parse_listing_filters` traduit la query string en filtres pour le
        repository. Elle n'existe plus qu'en UN exemplaire (routes/web.py)
        depuis la suppression de l'API (#30) : la liste de charges historiques
        (vides, illisibles, hostiles) doit continuer de produire un dict,
        jamais une exception."""
        result = _parse_listing_filters(MultiDict(args))

        assert isinstance(result, dict)

    def test_an_empty_query_string_yields_no_filter(self):
        assert _parse_listing_filters(MultiDict()) == {}

    def test_a_hostile_value_is_carried_as_data_not_as_sql(self):
        """La charge reste une valeur : c'est `_build_filter_clauses` qui la
        met en paramètre. Ce test fige le fait qu'elle traverse intacte, sans
        être « nettoyée » d'une manière qui donnerait un faux sentiment de
        sécurité."""
        payload = "'; DROP TABLE listings; --"

        assert _parse_listing_filters(MultiDict({"q": payload}))["q"] == payload
