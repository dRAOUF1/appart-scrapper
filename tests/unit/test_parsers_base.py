"""Tests unitaires de parsers/base.py et parsers/__init__.py.

Ce module ne scrape rien : il fige le *contrat* que toute source doit respecter
et que le reste de l'application consomme sans jamais coder une source en dur —

* le registre (`ParserRegistry`) et son injection explicite de `storage` : le
  scraping tourne sur un thread de fond, hors contexte Flask ;
* le contrat JSON de `list_sources()`, exposé tel quel par /api/sources ;
* les messages français de `unsupported_criteria()` / `cannot_search_reason()`,
  affichés à l'utilisateur mot pour mot ;
* les valeurs par défaut dont hérite une source qui ne surcharge rien.

Les sous-classes de test sont fabriquées par `make_parser_class()` : leur simple
définition les enregistre dans le registre global (`__init_subclass__`), effet de
bord assumé que le socle annule entre deux tests — voir
`test_registry_holds_only_the_real_sources_at_the_start_of_a_test`.
"""

from __future__ import annotations

import inspect

import pytest

import parsers
from parsers.base import BaseParser, ParserRegistry, get_locations
from tests.helpers.factories import (
    make_city_location,
    make_criteria,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)
from tests.helpers.fakes import fake_storage

# Les seules sources réellement enregistrées par parsers/__init__.py.
REAL_SOURCES = {"seloger", "laforet", "bienici"}


def make_parser_class(source_id: str = "", **attributes) -> type[BaseParser]:
    """Fabrique une sous-classe de BaseParser prête à instancier.

    La définir suffit à l'enregistrer quand `source_id` n'est pas vide, c'est
    tout l'objet de `__init_subclass__`. `scrape` est fourni par défaut parce
    qu'il est abstrait : sans lui la classe n'est pas instanciable.
    """
    namespace: dict = {"SOURCE_ID": source_id, "scrape": lambda self, criteria: []}
    namespace.update(attributes)
    return type("GeneratedParser", (BaseParser,), namespace)


# ---------------------------------------------------------------------------
# ParserRegistry : enregistrement
# ---------------------------------------------------------------------------

class TestAutoRegistration:
    def test_registry_holds_only_the_real_sources_at_the_start_of_a_test(self):
        """Vérifie explicitement le nettoyage du socle.

        Définir une sous-classe de BaseParser l'inscrit *définitivement* dans
        `ParserRegistry._parsers`, qui est un dict de classe partagé par tout le
        process. Sans la fixture autouse `reset_global_state` de
        tests/conftest.py, les parsers bidons des tests ci-dessous fuiteraient
        dans tous les suivants (et dans les assertions sur `list_sources()`).
        Ce test échoue au moindre relâchement de ce garde-fou.
        """
        assert set(ParserRegistry._parsers) == REAL_SOURCES

    def test_defining_a_subclass_registers_it_and_pollutes_the_global_registry(self):
        """L'effet de bord d'import, constaté puis annulé."""
        snapshot = dict(ParserRegistry._parsers)

        generated = make_parser_class("pollueur")

        assert ParserRegistry._parsers["pollueur"] is generated
        assert "pollueur" not in snapshot

        # C'est exactement ce snapshot que le socle réinstalle après le test :
        # on rejoue ici sa restauration pour prouver qu'elle suffit.
        ParserRegistry._parsers.clear()
        ParserRegistry._parsers.update(snapshot)
        assert "pollueur" not in ParserRegistry._parsers

    def test_a_subclass_without_source_id_is_not_registered(self):
        before = dict(ParserRegistry._parsers)

        make_parser_class()

        assert ParserRegistry._parsers == before

    def test_register_ignores_an_empty_source_id(self):
        """`register` est aussi appelable en décorateur : un SOURCE_ID vide ne
        doit pas créer une entrée `""` dans le registre."""
        before = dict(ParserRegistry._parsers)
        anonymous = type("Anonymous", (object,), {"SOURCE_ID": ""})

        assert ParserRegistry.register(anonymous) is anonymous
        assert ParserRegistry._parsers == before
        assert "" not in ParserRegistry._parsers

    def test_register_returns_the_class_so_it_works_as_a_decorator(self):
        cls = make_parser_class("decorated")
        assert ParserRegistry.register(cls) is cls

    def test_registering_the_same_source_id_twice_keeps_the_last_one(self):
        make_parser_class("dupliquee")
        second = make_parser_class("dupliquee")
        assert ParserRegistry._parsers["dupliquee"] is second


# ---------------------------------------------------------------------------
# ParserRegistry.get
# ---------------------------------------------------------------------------

class TestRegistryGet:
    def test_returns_an_instance_of_the_registered_class(self):
        generated = make_parser_class("gettable")
        instance = ParserRegistry.get("gettable")
        assert isinstance(instance, generated)

    def test_storage_is_injected_explicitly_and_defaults_to_none(self):
        """`storage` est passé en argument, jamais lu depuis `flask.current_app`
        (le scraping tourne hors contexte d'application)."""
        make_parser_class("with_storage")
        storage = fake_storage()

        assert ParserRegistry.get("with_storage", storage=storage).storage is storage
        assert ParserRegistry.get("with_storage").storage is None

    def test_unknown_source_lists_the_available_ones_sorted(self):
        """Ce message est affiché à l'utilisateur : les sources doivent être
        triées, pas rendues dans l'ordre d'import."""
        ParserRegistry._parsers.clear()
        make_parser_class("zebre")
        make_parser_class("abeille")
        make_parser_class("marmotte")

        # Le message est affiché tel quel à l'utilisateur : les sources y sont
        # listées, et triées par ordre alphabétique.
        with pytest.raises(ValueError, match=r"Sources disponibles : ") as excinfo:
            ParserRegistry.get("nawak")
        assert str(excinfo.value) == (
            "Source 'nawak' inconnue. Sources disponibles : abeille, marmotte, zebre"
        )

    def test_unknown_source_message_mentions_the_real_sources(self):
        with pytest.raises(ValueError, match=r"Sources disponibles : ") as excinfo:
            ParserRegistry.get("leboncoin")
        message = str(excinfo.value)
        assert "laforet" in message
        assert "seloger" in message

    @pytest.mark.parametrize("source", ["", None, "SELOGER"])
    def test_falsy_or_miscased_sources_are_rejected(self, source):
        """Aucun repli implicite : la casse compte, et `None` ne vaut pas
        « la source par défaut »."""
        with pytest.raises(ValueError, match="inconnue"):
            ParserRegistry.get(source)


# ---------------------------------------------------------------------------
# ParserRegistry.list_sources
# ---------------------------------------------------------------------------

class TestListSources:
    def test_sources_are_sorted_by_id(self):
        """Contrat de /api/sources : l'ordre est celui des identifiants, pour que
        la liste affichée soit stable d'un déploiement à l'autre."""
        ParserRegistry._parsers.clear()
        for source_id in ("zebre", "abeille", "marmotte"):
            make_parser_class(source_id)

        assert [s["id"] for s in ParserRegistry.list_sources()] == ["abeille", "marmotte", "zebre"]

    def test_exposes_the_full_json_contract_for_a_source(self):
        """La forme exacte consommée par le front — un champ retiré ici casse
        l'affichage sans autre avertissement."""
        make_parser_class(
            "listee",
            SOURCE_NAME="Listée",
            SOURCE_DESCRIPTION="Une description",
        )

        listed = [s for s in ParserRegistry.list_sources() if s["id"] == "listee"]
        assert listed == [{
            "id": "listee",
            "name": "Listée",
            "description": "Une description",
            "supported_transactions": ["rent", "buy"],
            "supported_property_types": ["apartment", "house", "parking", "land"],
            "manual_override_label": "",
            "manual_override_help": "",
            "url_note": "",
        }]

    def test_capabilities_are_serialised_as_lists_not_tuples(self):
        """Les tuples de classe ne sont pas sérialisables tels quels en JSON."""
        make_parser_class("tuples", SUPPORTED_TRANSACTIONS=("rent",), SUPPORTED_PROPERTY_TYPES=("apartment",))
        entry = next(s for s in ParserRegistry.list_sources() if s["id"] == "tuples")
        assert entry["supported_transactions"] == ["rent"]
        assert entry["supported_property_types"] == ["apartment"]
        assert isinstance(entry["supported_transactions"], list)

    @pytest.mark.parametrize("source_id", sorted(REAL_SOURCES))
    def test_the_real_sources_are_registered_by_importing_the_package(self, source_id):
        assert any(s["id"] == source_id for s in ParserRegistry.list_sources())

    def test_laforet_declares_its_property_type_limits(self):
        """Laforêt ne référence ni parking ni terrain : déclaré, donc dit à
        l'utilisateur avant le scrape au lieu d'échouer en cours de route."""
        laforet = next(s for s in ParserRegistry.list_sources() if s["id"] == "laforet")
        assert laforet["supported_property_types"] == ["apartment", "house"]
        assert laforet["supported_transactions"] == ["rent", "buy"]

    def test_only_sources_with_an_opaque_place_id_declare_a_manual_override_field(self):
        """SeLoger et bienici ne peuvent pas dériver leur identifiant de lieu
        opaque (placeId / zoneId) d'un code INSEE : ce sont les seules sources
        à proposer une saisie manuelle de repli. Laforêt n'en a pas besoin, son
        périmètre se dérive directement du code INSEE."""
        by_id = {s["id"]: s for s in ParserRegistry.list_sources()}
        assert by_id["seloger"]["manual_override_label"]
        assert by_id["seloger"]["manual_override_help"]
        assert by_id["bienici"]["manual_override_label"]
        assert by_id["bienici"]["manual_override_help"]
        assert by_id["laforet"]["manual_override_label"] == ""

    def test_only_laforet_declares_a_url_note(self):
        """URL_NOTE explique dans l'UI pourquoi le lien Laforêt montre plus large
        que la recherche (pas de surface maximale, pièces en minimum)."""
        by_id = {s["id"]: s for s in ParserRegistry.list_sources()}
        assert by_id["laforet"]["url_note"]
        assert by_id["seloger"]["url_note"] == ""


# ---------------------------------------------------------------------------
# Valeurs par défaut de BaseParser
# ---------------------------------------------------------------------------

class TestBaseParserDefaults:
    def test_scrape_is_abstract(self):
        with pytest.raises(TypeError, match="scrape"):
            type("Incomplete", (BaseParser,), {"SOURCE_ID": "incomplete"})()

    def test_parse_raises_not_implemented(self):
        parser = make_parser_class("no_parse")()
        with pytest.raises(NotImplementedError, match="parse\\(\\) not implemented for this source"):
            parser.parse("<html></html>")

    def test_build_search_url_returns_none(self):
        assert make_parser_class("no_url")().build_search_url(make_criteria()) is None

    def test_build_search_urls_wraps_build_search_url(self):
        parser = make_parser_class(
            "one_url",
            build_search_url=lambda self, criteria: "https://example.com/search",
        )()
        assert parser.build_search_urls({}) == ["https://example.com/search"]

    @pytest.mark.parametrize("returned", [None, ""])
    def test_build_search_urls_is_empty_rather_than_holding_a_falsy_url(self, returned):
        """Surtout pas `[None]` : les appelants itèrent sur cette liste."""
        parser = make_parser_class("falsy_url", build_search_url=lambda self, criteria: returned)()
        assert parser.build_search_urls({}) == []

    def test_to_native_returns_the_criteria_untouched(self):
        """Par défaut, une source lit le canonique tel quel."""
        parser = make_parser_class("passthrough")()
        criteria = make_criteria()
        assert parser.to_native(criteria) is criteria

    def test_parse_manual_override_is_empty(self):
        parser = make_parser_class("no_override")()
        assert parser.parse_manual_override("n'importe quoi") == {}

    def test_remember_manual_override_does_nothing(self):
        parser = make_parser_class("no_memory")()
        assert parser.remember_manual_override(make_criteria()) is None

    def test_a_new_source_covers_the_whole_canonical_vocabulary(self):
        """Une source n'a rien à déclarer pour fonctionner : elle est censée tout
        couvrir, et ne restreindre que si c'est réellement le cas."""
        parser = make_parser_class("everything")()
        assert parser.SUPPORTED_TRANSACTIONS == ("rent", "buy")
        assert parser.SUPPORTED_PROPERTY_TYPES == ("apartment", "house", "parking", "land")
        assert parser.MANUAL_OVERRIDE_LABEL == ""
        assert parser.MANUAL_OVERRIDE_HELP == ""
        assert parser.URL_NOTE == ""
        assert parser.SOURCE_NAME == ""
        assert parser.SOURCE_DESCRIPTION == ""

    def test_storage_is_stored_as_is(self):
        storage = fake_storage()
        assert make_parser_class("stores")(storage=storage).storage is storage
        assert make_parser_class("stores")().storage is None


class TestHasValidCriteriaDefault:
    """Le contrat universel de localisation : au moins un périmètre exploitable.
    Toute source qui ne surcharge rien en hérite."""

    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            ({"city": "Paris", "postalCode": "75018"}, True),
            ({"locations": [make_city_location()]}, True),
            ({"locations": [make_city_location(), make_city_location("Lyon", "69007", "69387")]}, True),
            ({"locations": [make_whole_city_location()]}, True),
            ({"locations": [make_department_location()]}, True),
            ({"locations": [make_region_location()]}, True),
            ({"city": "Paris"}, False),
            ({"postalCode": "75018"}, False),
            ({"locations": []}, False),
            ({"locations": [{"city": "Paris"}]}, False),
            ({"priceMax": 1500}, False),
            ({}, False),
        ],
    )
    def test_at_least_one_usable_location_is_required(self, criteria, expected):
        parser = make_parser_class("default_validity")()
        assert parser.has_valid_criteria(criteria) is expected


# ---------------------------------------------------------------------------
# unsupported_criteria : les messages vus par l'utilisateur
# ---------------------------------------------------------------------------

class TestUnsupportedCriteria:
    @pytest.mark.parametrize(
        ("transaction", "expected"),
        [
            ("buy", ["la transaction « Achat »"]),
            ("rent", []),
            (None, []),
            ("", []),
            # Valeur hors vocabulaire : le libellé retombe sur la valeur brute
            # plutôt que d'afficher un trou dans la phrase.
            ("troc", ["la transaction « troc »"]),
        ],
    )
    def test_transaction_message(self, transaction, expected):
        parser = make_parser_class("rent_only", SUPPORTED_TRANSACTIONS=("rent",))()
        assert parser.unsupported_criteria({"transaction": transaction}) == expected

    @pytest.mark.parametrize(
        ("property_types", "expected"),
        [
            (["parking"], ["les biens de type « Parking »"]),
            (["land"], ["les biens de type « Terrain »"]),
            (["apartment"], []),
            (["apartment", "house"], []),
            ([], []),
            (None, []),
            # Plusieurs types refusés : un message par type, dans l'ordre demandé.
            (["parking", "land"], ["les biens de type « Parking »", "les biens de type « Terrain »"]),
            (["apartment", "parking"], ["les biens de type « Parking »"]),
            # Hors vocabulaire : valeur brute.
            (["yacht"], ["les biens de type « yacht »"]),
        ],
    )
    def test_property_type_message(self, property_types, expected):
        parser = make_parser_class(
            "housing_only", SUPPORTED_PROPERTY_TYPES=("apartment", "house")
        )()
        assert parser.unsupported_criteria({"propertyTypes": property_types}) == expected

    def test_transaction_comes_before_property_types(self):
        parser = make_parser_class(
            "restricted",
            SUPPORTED_TRANSACTIONS=("rent",),
            SUPPORTED_PROPERTY_TYPES=("apartment",),
        )()
        assert parser.unsupported_criteria({
            "transaction": "buy",
            "propertyTypes": ["parking", "land"],
        }) == [
            "la transaction « Achat »",
            "les biens de type « Parking »",
            "les biens de type « Terrain »",
        ]

    def test_a_source_that_covers_everything_never_complains(self):
        parser = make_parser_class("everything_ok")()
        assert parser.unsupported_criteria(make_criteria(
            transaction="buy", propertyTypes=["apartment", "house", "parking", "land"]
        )) == []


# ---------------------------------------------------------------------------
# cannot_search_reason : point d'entrée unique de la décision « utilisable »
# ---------------------------------------------------------------------------

class TestCannotSearchReason:
    def test_none_when_everything_is_honourable(self):
        parser = make_parser_class("usable", SOURCE_NAME="Usable")()
        assert parser.cannot_search_reason(make_criteria()) is None

    def test_the_location_is_checked_before_the_capabilities(self):
        """L'ordre compte : sans localisation, c'est ça qu'il faut dire, même si
        les critères sont aussi hors capacités — sinon l'utilisateur corrige le
        type de bien et se heurte ensuite au vrai problème."""
        parser = make_parser_class(
            "picky", SOURCE_NAME="Picky", SUPPORTED_PROPERTY_TYPES=("house",)
        )()
        criteria = {"propertyTypes": ["parking"]}  # ni localisation ni type géré

        assert parser.cannot_search_reason(criteria) == (
            "aucune localisation exploitable (ville + code postal requis)"
        )

    def test_capability_message_uses_the_source_name_and_joins_with_ni(self):
        parser = make_parser_class(
            "limitee",
            SOURCE_NAME="Limitée",
            SUPPORTED_TRANSACTIONS=("rent",),
            SUPPORTED_PROPERTY_TYPES=("apartment",),
        )()
        criteria = make_criteria(transaction="buy", propertyTypes=["parking", "land"])

        assert parser.cannot_search_reason(criteria) == (
            "Limitée ne référence pas la transaction « Achat » "
            "ni les biens de type « Parking » ni les biens de type « Terrain »"
        )

    def test_a_single_unsupported_criterion_has_no_ni(self):
        parser = make_parser_class(
            "limitee", SOURCE_NAME="Limitée", SUPPORTED_PROPERTY_TYPES=("apartment",)
        )()
        assert parser.cannot_search_reason(make_criteria(propertyTypes=["parking"])) == (
            "Limitée ne référence pas les biens de type « Parking »"
        )

    def test_a_source_overriding_has_valid_criteria_drives_the_first_check(self):
        """`cannot_search_reason` consulte `has_valid_criteria`, jamais
        `get_locations` en direct : une source qui a son propre contrat de
        localisation (SeLoger et son placeId) reste maîtresse de la décision."""
        parser = make_parser_class(
            "always_valid",
            SOURCE_NAME="Always",
            has_valid_criteria=lambda self, criteria: True,
        )()
        assert parser.cannot_search_reason({}) is None


# ---------------------------------------------------------------------------
# get_locations
# ---------------------------------------------------------------------------

class TestGetLocations:
    """Le point de passage unique par lequel toute source lit les périmètres.
    Délègue à core.criteria.normalize_locations, donc comprend aussi l'ancien
    couple à plat city/postalCode."""

    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            # Ancien format à plat, encore en base.
            (
                {"city": "Paris", "postalCode": "75014"},
                [{"kind": "city", "city": "Paris", "postalCode": "75014"}],
            ),
            # Liste, le format canonique.
            (
                {"locations": [{"city": "Lyon", "postalCode": "69007"}]},
                [{"kind": "city", "city": "Lyon", "postalCode": "69007"}],
            ),
            # `locations` prime sur les clés à plat.
            (
                {
                    "city": "Paris", "postalCode": "75014",
                    "locations": [{"city": "Lyon", "postalCode": "69007"}],
                },
                [{"kind": "city", "city": "Lyon", "postalCode": "69007"}],
            ),
            # Les entrées incomplètes sont écartées, pas devinées.
            (
                {"locations": [
                    {"city": "Paris", "postalCode": "75014"},
                    {"city": "Lyon"},
                    {"postalCode": "13001"},
                ]},
                [{"kind": "city", "city": "Paris", "postalCode": "75014"}],
            ),
            ({}, []),
            ({"city": "Paris"}, []),
            ({"locations": []}, []),
        ],
    )
    def test_normalises_every_accepted_shape(self, criteria, expected):
        assert get_locations(criteria) == expected

    def test_the_insee_code_survives_normalisation(self):
        """C'est lui qui permet à chaque source de retrouver son propre
        identifiant de lieu : le perdre en route casse SeLoger et Laforêt."""
        location = make_city_location("Paris", "75013", "75113")
        assert get_locations({"locations": [location]}) == [location]

    def test_wide_perimeters_keep_their_own_shape(self):
        """Un département ou une région n'a ni ville ni code postal."""
        locations = [make_department_location(), make_region_location()]
        assert get_locations({"locations": locations}) == locations

    def test_whole_city_keeps_its_postal_codes(self):
        location = make_whole_city_location("Bordeaux", ("33000", "33800"), "33063")
        assert get_locations({"locations": [location]}) == [location]


# ---------------------------------------------------------------------------
# parsers/__init__.py
# ---------------------------------------------------------------------------

class TestPackageHelpers:
    def test_get_parser_delegates_to_the_registry_with_the_storage(self):
        storage = fake_storage()
        parser = parsers.get_parser("seloger", storage=storage)
        assert parser.SOURCE_ID == "seloger"
        assert parser.storage is storage

    def test_get_parser_without_storage(self):
        assert parsers.get_parser("laforet").storage is None

    def test_get_parser_propagates_the_unknown_source_error(self):
        with pytest.raises(ValueError, match="Sources disponibles"):
            parsers.get_parser("leboncoin")

    def test_list_sources_is_the_registry_listing(self):
        assert parsers.list_sources() == ParserRegistry.list_sources()

    def test_remember_manual_overrides_calls_every_source(self):
        calls: list[tuple[str, dict]] = []
        make_parser_class(
            "premiere",
            remember_manual_override=lambda self, criteria: calls.append(("premiere", criteria)),
        )
        make_parser_class(
            "seconde",
            remember_manual_override=lambda self, criteria: calls.append(("seconde", criteria)),
        )
        criteria = make_criteria()

        parsers.remember_manual_overrides(["premiere", "seconde"], criteria)

        assert calls == [("premiere", criteria), ("seconde", criteria)]

    def test_one_failing_source_never_prevents_the_others(self):
        """« N'échoue jamais » : ce n'est qu'une optimisation, elle ne doit pas
        faire échouer la création d'une recherche. Le `except Exception` est PAR
        SOURCE, donc une source qui lève ne coupe pas la boucle — et une source
        inconnue (ValueError du registre) non plus.
        """
        called: list[str] = []

        def boom(self, criteria):
            raise RuntimeError("banque de placeId indisponible")

        make_parser_class("explose", remember_manual_override=boom)
        make_parser_class("marche", remember_manual_override=lambda self, c: called.append("marche"))

        parsers.remember_manual_overrides(["explose", "inconnue", "marche"], make_criteria())

        assert called == ["marche"]

    def test_remember_manual_overrides_forwards_the_storage(self):
        seen: list[object] = []
        make_parser_class(
            "sonde_storage",
            remember_manual_override=lambda self, criteria: seen.append(self.storage),
        )
        storage = fake_storage()

        parsers.remember_manual_overrides(["sonde_storage"], make_criteria(), storage=storage)

        assert seen == [storage]

    def test_remember_manual_overrides_accepts_an_empty_source_list(self):
        assert parsers.remember_manual_overrides([], make_criteria()) is None

    def test_public_api(self):
        """Ce que le reste de l'application importe depuis `parsers`."""
        assert set(parsers.__all__) == {
            "BaseParser", "get_parser", "list_sources", "remember_manual_overrides",
            "SeLogerParser", "LaforetParser", "BienIciParser",
        }


class TestNoFlaskDependency:
    """Régression : `storage` était lu depuis `flask.current_app`. Or le scraping
    s'exécute sur un thread de fond (ScrapeService dans un ThreadPoolExecutor,
    voir core.scrape_control), hors de tout contexte d'application — tous les
    scrapes automatiques échouaient sur « Working outside of application
    context ». `storage` est désormais injecté, et doit le rester."""

    @pytest.mark.parametrize("module_name", ["parsers.base", "parsers"])
    def test_no_module_reads_the_flask_application_context(self, module_name):
        import ast
        import importlib

        # Analyse de l'AST plutôt que du texte : le module *documente* en
        # commentaire pourquoi il n'utilise pas current_app, et un simple
        # `"current_app" not in source` échouerait sur cette explication.
        source = inspect.getsource(importlib.import_module(module_name))
        tree = ast.parse(source)

        imported_from_flask = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "flask"
        ]
        assert imported_from_flask == [], "parsers/ ne doit rien importer de flask"

        assert not [
            node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "current_app"
        ], "parsers/ ne doit pas lire le contexte d'application Flask"
