"""Tests unitaires de parsers/seloger.py.

Ce module ne parle jamais au vrai SeLoger : il fige la *traduction* entre le
vocabulaire canonique (core.criteria) et celui du site, et les invariants qui
ont chacun coûté un incident réel —

* `scrape()` LÈVE quand aucun placeId n'a pu être déterminé : une recherche
  SeLoger sans `locations=` n'est pas une recherche vide, c'est une recherche
  sur la France entière (même raison pour `build_search_url()`, qui renvoie
  None plutôt qu'un lien national d'apparence légitime) ;
* `has_valid_criteria()` ne fait AUCUN appel réseau : créer une recherche ne
  doit pas dépendre de la disponibilité de SeLoger ;
* le cache de placeId est lu depuis le `storage` injecté, jamais depuis
  `flask.current_app` — le scraping tourne sur un thread de fond (voir
  TestNoFlaskDependency) ;
* une localisation qui ne se résout pas est simplement omise : mieux vaut
  chercher sur les villes qui ont fonctionné que d'échouer en entier.

Les tests de régression de tests/_legacy/test_seloger_parser.py sont tous
repris ici, réorganisés par fonction et paramétrés.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from parsers.seloger import SeLogerParser, _dict_to_listing
from tests.helpers.factories import (
    make_city_location,
    make_criteria,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)
from tests.helpers.fakes import fake_storage

# --- Périmètres réutilisés -------------------------------------------------
# Ne jamais muter ces dicts : ils sont partagés par tout le module.
PARIS_15 = make_city_location("Paris", "75015", "75115")
LYON_7 = make_city_location("Lyon", "69007", "69387")
# Identifiable (code INSEE présent) mais que l'autocomplete SeLoger ne sait
# pas résoudre : c'est le cas de dégradation partielle.
NOWHERE = make_city_location("Nawak", "99999", "99999")
# Ville tapée à la main, jamais passée par l'autocomplete : aucun code INSEE,
# donc rien à quoi rattacher un placeId.
TYPED_BY_HAND = {"kind": "city", "city": "Paris", "postalCode": "75015"}

GIRONDE = make_department_location("33", "Gironde")
IDF = make_region_location("11", "Île-de-France", ("75", "77", "78", "91", "92", "93", "94", "95"))
PARIS_WHOLE = make_whole_city_location("Paris", ("75001", "75015"), "75056")

# Forme exacte d'un élément renvoyé par scraper.seloger.scrape() — recopiée
# depuis la construction de `listings` dans scraper/seloger.py, avec des
# valeurs plausibles. Sert de référence unique pour _dict_to_listing().
SELOGER_DETAIL = {
    "id": "203456789",
    "legacyId": "198765432",
    "title": "Appartement 3 pièces 65 m²",
    "headline": "Beau 3 pièces rénové",
    "description": "Au calme, dernier étage avec ascenseur.",
    "price": "1 850 €/mois",
    "priceValue": 1850.0,
    "priceDetails": "Charges comprises",
    "surface": 65,
    "rooms": 3,
    "propertyType": "Appartement",
    "city": "Paris",
    "district": "Paris 15e",
    "zipCode": "75015",
    "url": "https://www.seloger.com/annonces/locations/appartement/paris-15eme-75/203456789.htm",
    "photos": [
        {"url": "https://v.seloger.com/s/width/800/1.jpg", "alt": "Séjour", "key": "k1"},
        {"url": "https://v.seloger.com/s/width/800/2.jpg", "alt": "Cuisine", "key": "k2"},
    ],
    "agency": "Agence du 15e",
    "isPrivate": False,
    "phone": ["0102030405"],
    "epc": "C",
    "ges": "B",
    "isNew": True,
    "isExclusive": False,
    "has3DVisit": True,
    "creationDate": "2026-07-01",
    "updateDate": "2026-07-20",
}


def manual(place_ids: list[str]) -> dict:
    """Des critères ne portant qu'un placeId saisi à la main."""
    return {"sourceOverrides": {"seloger": {"placeIds": place_ids}}}


def resolving_parser() -> SeLogerParser:
    """Un parser doté d'un storage : la résolution automatique s'appuie sur le
    cache en base, donc sur le storage injecté (jamais sur flask.current_app)."""
    return SeLogerParser(storage=fake_storage())


@pytest.fixture
def logged():
    """Les messages loguru émis pendant le test, sous forme (niveau, message).

    `caplog` ne voit pas loguru : il faut brancher un sink. Celui-ci est retiré
    à la fin du test (et le socle en attraperait la fuite de toute façon).
    """
    from loguru import logger

    records: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
    )
    yield records
    logger.remove(sink_id)


# ---------------------------------------------------------------------------
# _dict_to_listing : dict du scraper -> Listing commun
# ---------------------------------------------------------------------------

class TestDictToListing:
    def test_maps_the_whole_scraper_payload(self):
        """Le contrat complet, sur un dict tel que scraper.seloger le produit."""
        listing = _dict_to_listing(SELOGER_DETAIL)

        assert listing.listing_id == "sl_203456789"
        assert listing.legacy_id == "198765432"
        assert listing.source == "seloger"
        assert listing.url == SELOGER_DETAIL["url"]
        assert listing.title == "Appartement 3 pièces 65 m²"
        assert listing.headline == "Beau 3 pièces rénové"
        assert listing.price == "1 850 €/mois"
        assert listing.price_value == 1850.0
        assert listing.price_details == "Charges comprises"
        assert listing.surface == "65"
        assert listing.rooms == "3"
        assert listing.property_type == "Appartement"
        assert listing.city == "Paris"
        assert listing.district == "Paris 15e"
        assert listing.zip_code == "75015"
        assert listing.agency == "Agence du 15e"
        assert listing.epc == "C"
        assert listing.ges == "B"
        assert listing.is_new is True
        assert listing.is_exclusive is False
        assert listing.has_3d_visit is True
        assert listing.is_private is False
        # Issue #12 : la date brute SeLoger (« YYYY-MM-DD ») est normalisée
        # en ISO-8601 UTC canonique par parsers/_dates.py.
        assert listing.creation_date == "2026-07-01T00:00:00+00:00"
        assert listing.update_date == "2026-07-20"

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ({"id": "203456789"}, "sl_203456789"),
            # Le préfixe est ce qui garantit qu'aucun identifiant ne peut
            # collisionner avec ceux d'une autre source (lf_ chez Laforêt).
            ({"id": 203456789}, "sl_203456789"),
            # Sans id, l'identifiant se réduit au préfixe : deux annonces sans
            # id seraient alors confondues. Ne se produit pas via
            # scraper.seloger (l'id est la clé du dict de départ), mais rien ne
            # le garantit ici.
            ({}, "sl_"),
            ({"id": ""}, "sl_"),
        ],
    )
    def test_listing_id_is_always_prefixed(self, data, expected):
        assert _dict_to_listing(data).listing_id == expected

    @pytest.mark.parametrize(
        ("photos", "expected"),
        [
            # Forme normale : un dict avec "url".
            ([{"url": "https://cdn/1.jpg", "alt": "", "key": ""}], "https://cdn/1.jpg"),
            # "url" prime sur "source" quand les deux sont là.
            ([{"url": "https://cdn/1.jpg", "source": "https://cdn/vieux.jpg"}], "https://cdn/1.jpg"),
            # Ancienne forme du scraper : la clé s'appelait "source".
            ([{"source": "https://cdn/vieux.jpg"}], "https://cdn/vieux.jpg"),
            # Dict sans aucune des deux clés : pas de vignette, pas d'exception.
            ([{"alt": "sans url"}], ""),
            # Photo donnée directement en chaîne.
            (["https://cdn/plain.jpg"], "https://cdn/plain.jpg"),
            # Ni dict ni chaîne : ignoré.
            ([42], ""),
            ([None], ""),
            # Seule la première photo sert de vignette.
            ([{"url": "https://cdn/1.jpg"}, {"url": "https://cdn/2.jpg"}], "https://cdn/1.jpg"),
            ([], ""),
            (None, ""),
            ("pas-une-liste", ""),
        ],
    )
    def test_thumbnail_is_the_first_photo_whatever_its_shape(self, photos, expected):
        assert _dict_to_listing({"id": "1", "photos": photos}).image_url == expected

    @pytest.mark.parametrize(
        ("photos", "expected_json"),
        [
            ([], "[]"),
            ([{"url": "u", "alt": "a", "key": "k"}], '[{"url": "u", "alt": "a", "key": "k"}]'),
            # BUG : une valeur non-liste est sérialisée telle quelle. `photos`
            # est une colonne JSONB dont tous les consommateurs attendent un
            # tableau — "null" ou '"x"' y passeraient sans erreur SQL puis
            # casseraient toute itération à la lecture. Non atteignable via
            # scraper.seloger aujourd'hui (il construit toujours une liste),
            # mais rien dans _dict_to_listing ne l'empêche.
            (None, "null"),
            ("x", '"x"'),
        ],
    )
    def test_photos_are_reserialised_as_json(self, photos, expected_json):
        assert _dict_to_listing({"id": "1", "photos": photos}).photos == expected_json

    def test_photos_json_round_trips(self):
        listing = _dict_to_listing(SELOGER_DETAIL)
        assert json.loads(listing.photos) == SELOGER_DETAIL["photos"]
        assert listing.image_url == SELOGER_DETAIL["photos"][0]["url"]

    @pytest.mark.parametrize(
        ("phone", "expected"),
        [
            (["0102030405"], '["0102030405"]'),
            (["0102030405", "0607080910"], '["0102030405", "0607080910"]'),
            ([], "[]"),
            # Clé absente : le défaut du `.get` est une liste vide.
            (None, "null"),  # BUG : même problème que `photos` ci-dessus.
        ],
    )
    def test_phone_is_reserialised_as_json(self, phone, expected):
        assert _dict_to_listing({"id": "1", "phone": phone}).phone == expected

    def test_absent_photos_and_phone_keys_give_empty_json_arrays(self):
        listing = _dict_to_listing({"id": "1"})
        assert listing.photos == "[]"
        assert listing.phone == "[]"

    @pytest.mark.parametrize("field", ["surface", "rooms"])
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # ZÉRO DOIT SURVIVRE : le test `is not None` (et non la vérité de la
            # valeur) est ce qui distingue « 0 pièce » de « inconnu ». Un studio
            # sans pièce déclarée ou une surface de 0 m² sont des données, pas
            # des absences.
            (0, "0"),
            (65, "65"),
            (65.5, "65.5"),
            ("65", "65"),
            (None, ""),
        ],
    )
    def test_surface_and_rooms_are_stringified_only_when_present(self, field, value, expected):
        assert getattr(_dict_to_listing({"id": "1", field: value}), field) == expected

    @pytest.mark.parametrize("field", ["surface", "rooms"])
    def test_missing_surface_and_rooms_keys_are_empty(self, field):
        assert getattr(_dict_to_listing({"id": "1"}), field) == ""

    def test_description_is_truncated_to_300_characters(self):
        """La colonne n'est pas faite pour des annonces entières, et la
        description longue n'est jamais affichée en entier."""
        listing = _dict_to_listing({"id": "1", "description": "a" * 500})
        assert len(listing.description) == 300
        assert listing.description == "a" * 300

    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            ("Courte", "Courte"),
            ("a" * 300, "a" * 300),
            ("a" * 301, "a" * 300),
            ("", ""),
            (None, ""),
        ],
    )
    def test_description_edge_cases(self, description, expected):
        assert _dict_to_listing({"id": "1", "description": description}).description == expected

    @pytest.mark.parametrize(
        ("city", "district", "expected_location"),
        [
            # La ville d'abord, le quartier en repli : `location` est l'étiquette
            # affichée, elle ne doit jamais être vide quand l'un des deux existe.
            ("Paris", "Paris 15e", "Paris"),
            ("", "Paris 15e", "Paris 15e"),
            (None, "Paris 15e", "Paris 15e"),
            ("Paris", "", "Paris"),
            ("", "", ""),
        ],
    )
    def test_location_is_the_city_or_the_district(self, city, district, expected_location):
        listing = _dict_to_listing({"id": "1", "city": city, "district": district})
        assert listing.location == expected_location

    @pytest.mark.parametrize(
        ("field", "key"),
        [
            ("url", "url"),
            ("title", "title"),
            ("price", "price"),
            ("price_details", "priceDetails"),
            ("city", "city"),
            ("district", "district"),
            ("zip_code", "zipCode"),
            ("property_type", "propertyType"),
            ("agency", "agency"),
            ("epc", "epc"),
            ("ges", "ges"),
            # Issue #12 : creation_date n'est plus dans cette liste — une clé
            # None est absorbée par normaliser_creation_date() (sentinelle
            # « unknown »), elle ne fuit plus en None. Voir
            # test_parsers_dates.py pour le contrat dédié.
            ("update_date", "updateDate"),
            ("headline", "headline"),
        ],
    )
    def test_a_key_present_but_null_leaks_none_into_a_string_field(self, field, key):
        """BUG : `data.get(key, "")` ne protège que de la clé ABSENTE, pas d'une
        clé présente valant None — et c'est exactement ce que produit
        scraper.seloger, qui remplit ces clés avec des `.get()` sur le JSON du
        site (`"price": price_info.get("formatted")`, `"city":
        location.get("city")`...). Une annonce dont SeLoger n'a pas rempli le
        champ arrive donc en base avec None là où le modèle déclare `str`.

        Ce test fige le comportement ACTUEL : les champs deviennent None. Le
        correctif serait `data.get(key) or ""`.
        """
        assert getattr(_dict_to_listing({"id": "1", key: None}), field) is None

    def test_a_null_legacy_id_becomes_the_string_none(self):
        """BUG : `str(data.get("legacyId", ""))` transforme un legacyId absent du
        JSON SeLoger (`metadata.get("legacyId")` -> None) en la CHAÎNE "None",
        stockée telle quelle. Le correctif serait `str(... or "")`.
        """
        assert _dict_to_listing({"id": "1", "legacyId": None}).legacy_id == "None"
        # Clé absente : là, le défaut du `.get` joue et le résultat est correct.
        assert _dict_to_listing({"id": "1"}).legacy_id == ""

    @pytest.mark.parametrize("field", ["is_private", "is_new", "is_exclusive", "has_3d_visit"])
    def test_booleans_default_to_false(self, field):
        assert getattr(_dict_to_listing({"id": "1"}), field) is False

    def test_price_value_stays_none_when_unknown(self):
        """Contrairement aux chaînes, `price_value` est typé `float | None` :
        None y est la bonne valeur, pas une dégradation."""
        assert _dict_to_listing({"id": "1"}).price_value is None
        assert _dict_to_listing({"id": "1", "priceValue": 0}).price_value == 0


# ---------------------------------------------------------------------------
# parse_manual_override : la saisie libre de l'utilisateur
# ---------------------------------------------------------------------------

class TestParseManualOverride:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", {}),
            ("   ", {}),
            (None, {}),
            ("\n\t", {}),
            # Un seul placeId.
            ("AD08FR31096", {"placeIds": ["AD08FR31096"]}),
            ("  AD08FR31096  ", {"placeIds": ["AD08FR31096"]}),
            # Plusieurs, séparés par des virgules, chacun détouré.
            ("AD08FR31096, AD08FR36603", {"placeIds": ["AD08FR31096", "AD08FR36603"]}),
            ("AD08FR31096 ,AD08FR36603", {"placeIds": ["AD08FR31096", "AD08FR36603"]}),
            # Les fragments vides sont rejetés, pas gardés comme placeId vide.
            ("AD08FR31096,,   ,AD08FR36603", {"placeIds": ["AD08FR31096", "AD08FR36603"]}),
            ("AD08FR31096,", {"placeIds": ["AD08FR31096"]}),
            # Que des séparateurs : plus rien à garder, donc {} et non
            # {"placeIds": []} — l'appelant ne doit pas croire à une surcharge.
            (",", {}),
            (" , , ", {}),
        ],
    )
    def test_comma_separated_place_ids(self, value, expected):
        assert SeLogerParser().parse_manual_override(value) == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://www.seloger.com/classified-search?locations=AD08FR31096",
                {"placeIds": ["AD08FR31096"]},
            ),
            # Une vraie URL SeLoger joint les placeIds par des virgules dans une
            # seule occurrence de `locations=`.
            (
                "https://www.seloger.com/classified-search?locations=AD08FR31096,AD08FR36603",
                {"placeIds": ["AD08FR31096", "AD08FR36603"]},
            ),
            # Aucun `locations=` : rien d'exploitable.
            ("https://www.seloger.com/", {}),
            ("http://www.seloger.com/classified-search?priceMax=2000", {}),
        ],
    )
    def test_pasted_search_urls(self, url, expected):
        assert SeLogerParser().parse_manual_override(url) == expected

    def test_only_the_place_ids_of_a_pasted_url_are_kept(self):
        """Le reste des critères vient du formulaire : une URL collée ne doit pas
        écraser en douce le budget ou la surface que l'utilisateur a saisis."""
        override = SeLogerParser().parse_manual_override(
            "https://www.seloger.com/classified-search"
            "?locations=AD08FR31096&priceMax=2000&spaceMin=40&rooms=2,3"
        )
        assert override == {"placeIds": ["AD08FR31096"]}

    def test_url_parsing_is_delegated_to_the_scraper(self):
        """Un seul endroit sait lire une URL SeLoger : scraper.seloger."""
        with patch(
            "scraper.seloger.parse_search_url",
            return_value={"placeIds": ["AD08FR1"], "priceMax": 999},
        ) as mock_parse:
            override = SeLogerParser().parse_manual_override("https://www.seloger.com/x")

        mock_parse.assert_called_once_with("https://www.seloger.com/x")
        assert override == {"placeIds": ["AD08FR1"]}

    def test_a_url_whose_place_ids_are_empty_yields_no_override(self):
        with patch("scraper.seloger.parse_search_url", return_value={"placeIds": []}):
            assert SeLogerParser().parse_manual_override("https://www.seloger.com/x") == {}
        with patch("scraper.seloger.parse_search_url", return_value={}):
            assert SeLogerParser().parse_manual_override("https://www.seloger.com/x") == {}

    def test_the_http_prefix_is_tested_after_stripping(self):
        with patch("scraper.seloger.parse_search_url", return_value={"placeIds": ["AD1"]}) as mock_parse:
            assert SeLogerParser().parse_manual_override("  https://www.seloger.com/x  ") == {
                "placeIds": ["AD1"]
            }
        mock_parse.assert_called_once_with("https://www.seloger.com/x")

    def test_a_url_without_its_scheme_is_taken_for_a_place_id(self):
        """Divergence assumée : la détection repose sur le préfixe "http". Une
        URL collée sans son schéma devient un placeId bidon, qui ne sera rejeté
        que par SeLoger au moment du scrape."""
        assert SeLogerParser().parse_manual_override("www.seloger.com/classified-search?locations=X") == {
            "placeIds": ["www.seloger.com/classified-search?locations=X"]
        }


# ---------------------------------------------------------------------------
# remember_manual_override : capitaliser la saisie manuelle
# ---------------------------------------------------------------------------

class TestRememberManualOverride:
    def test_banks_the_place_id_against_the_injected_repo(self):
        storage = fake_storage()
        parser = SeLogerParser(storage=storage)
        criteria = {**manual(["AD08FR31096"]), "locations": [PARIS_15]}

        with patch("services.seloger_geocode.remember_manual_place_id") as mock_remember:
            assert parser.remember_manual_override(criteria) is None

        mock_remember.assert_called_once_with(criteria, repo=storage.seloger_geo)

    @pytest.mark.parametrize("storage", [None, object()], ids=["sans_storage", "storage_sans_repo_geo"])
    def test_without_a_repo_nothing_is_attempted(self, storage):
        parser = SeLogerParser(storage=storage)
        with patch("services.seloger_geocode.remember_manual_place_id") as mock_remember:
            assert parser.remember_manual_override(make_criteria()) is None
        mock_remember.assert_not_called()

    def test_a_failure_is_logged_but_never_raised(self, logged):
        """Ce n'est qu'une optimisation : elle ne doit jamais faire échouer
        l'enregistrement d'une recherche."""
        parser = SeLogerParser(storage=fake_storage())

        with patch(
            "services.seloger_geocode.remember_manual_place_id",
            side_effect=RuntimeError("banque indisponible"),
        ):
            assert parser.remember_manual_override(make_criteria()) is None

        assert any(
            level == "DEBUG" and "non mémorisé" in message and "banque indisponible" in message
            for level, message in logged
        )


# ---------------------------------------------------------------------------
# _place_ids : le cœur de la localisation SeLoger
# ---------------------------------------------------------------------------

class TestPlaceIds:
    def test_a_manual_place_id_short_circuits_every_resolution(self):
        """Priorité absolue à la saisie manuelle : aucun appel de résolution,
        donc aucun appel réseau ni lecture de cache."""
        storage = fake_storage()
        parser = SeLogerParser(storage=storage)

        with patch("services.seloger_geocode.resolve_place_id") as mock_resolve:
            place_ids = parser._place_ids({**manual(["AD08FR12345"]), "locations": [PARIS_15, LYON_7]})

        assert place_ids == ["AD08FR12345"]
        mock_resolve.assert_not_called()
        storage.seloger_geo.get_cached.assert_not_called()

    def test_the_manual_list_is_copied_not_aliased(self):
        """Les critères sont partagés entre les sources d'une recherche : la
        liste rendue ne doit pas être celle stockée dans les critères."""
        criteria = manual(["AD08FR12345"])
        place_ids = SeLogerParser()._place_ids(criteria)

        place_ids.append("AD08FR99999")
        assert criteria["sourceOverrides"]["seloger"]["placeIds"] == ["AD08FR12345"]

    @pytest.mark.parametrize(
        "criteria",
        [
            {},
            {"locations": []},
            {"priceMax": 1500},
            # Localisation incomplète : écartée par normalize_locations.
            {"locations": [{"city": "Paris"}]},
            # Surcharge vide : ne compte pas comme une saisie manuelle.
            {"sourceOverrides": {"seloger": {"placeIds": []}}},
        ],
        ids=["vide", "liste_vide", "sans_localisation", "localisation_incomplete", "surcharge_vide"],
    )
    def test_no_location_means_no_place_id_and_no_call(self, criteria):
        storage = fake_storage()
        parser = SeLogerParser(storage=storage)

        with patch("services.seloger_geocode.resolve_place_id") as mock_resolve:
            assert parser._place_ids(criteria) == []

        mock_resolve.assert_not_called()
        storage.seloger_geo.get_cached.assert_not_called()

    def test_without_a_repo_the_impossibility_is_logged(self, logged):
        """Sans storage il n'y a pas de cache, donc pas de résolution possible :
        le dire au lieu de partir sur une recherche non localisée."""
        parser = SeLogerParser()

        with patch("services.seloger_geocode.resolve_place_id") as mock_resolve:
            assert parser._place_ids({"locations": [PARIS_15]}) == []

        mock_resolve.assert_not_called()
        assert any(
            level == "WARNING" and "Aucun storage fourni au parser" in message
            for level, message in logged
        )

    def test_a_storage_without_the_geo_repository_is_treated_as_absent(self):
        parser = SeLogerParser(storage=object())
        assert parser._geo_repo() is None
        assert parser._place_ids({"locations": [PARIS_15]}) == []

    def test_the_repo_comes_from_the_injected_storage(self):
        storage = fake_storage()
        assert SeLogerParser(storage=storage)._geo_repo() is storage.seloger_geo
        assert SeLogerParser()._geo_repo() is None

    def test_one_resolution_per_perimeter_in_order(self):
        parser = resolving_parser()
        with patch(
            "services.seloger_geocode.resolve_place_id",
            side_effect=["AD08FR31096", "AD08FR99999"],
        ) as mock_resolve:
            place_ids = parser._place_ids({"locations": [PARIS_15, LYON_7]})

        assert place_ids == ["AD08FR31096", "AD08FR99999"]
        assert mock_resolve.call_count == 2
        assert [call.args[0] for call in mock_resolve.call_args_list] == [PARIS_15, LYON_7]

    def test_duplicates_are_collapsed_keeping_the_first_position(self):
        """Deux arrondissements d'une même ville peuvent résoudre vers le même
        placeId : le répéter dans l'URL ne sert à rien."""
        parser = resolving_parser()
        with patch(
            "services.seloger_geocode.resolve_place_id",
            side_effect=["AD08FR31096", "AD08FR36603", "AD08FR31096"],
        ):
            place_ids = parser._place_ids({"locations": [PARIS_15, LYON_7, dict(PARIS_15)]})

        assert place_ids == ["AD08FR31096", "AD08FR36603"]

    @pytest.mark.parametrize(
        ("resolved", "expected"),
        [
            # Dégradation partielle VOULUE : la localisation non résolue est
            # simplement omise, on cherche sur celles qui ont fonctionné.
            (["AD08FR31096", None], ["AD08FR31096"]),
            ([None, "AD08FR36603"], ["AD08FR36603"]),
            ([None, None], []),
            (["AD08FR31096", ""], ["AD08FR31096"]),
        ],
    )
    def test_an_unresolved_perimeter_is_omitted_not_fatal(self, resolved, expected):
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", side_effect=resolved):
            assert parser._place_ids({"locations": [PARIS_15, NOWHERE]}) == expected

    @pytest.mark.parametrize(
        "location",
        [PARIS_15, PARIS_WHOLE, GIRONDE, IDF],
        ids=["code_postal", "ville_entiere", "departement", "region"],
    )
    def test_one_place_id_covers_a_whole_perimeter_at_any_level(self, location):
        """SeLoger a un identifiant par niveau et un seul suffit à le couvrir
        entièrement — vérifié en live : AD04FR5 (Île-de-France) rend des annonces
        réparties sur les 8 départements. Pas de développement en communes."""
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value="AD0xFR1") as mock_resolve:
            assert parser._place_ids({"locations": [location]}) == ["AD0xFR1"]
        assert mock_resolve.call_count == 1


# ---------------------------------------------------------------------------
# to_native : canonique -> vocabulaire SeLoger
# ---------------------------------------------------------------------------

class TestToNative:
    def test_translates_the_whole_canonical_vocabulary(self):
        criteria = {
            **manual(["AD08FR31096"]),
            "transaction": "buy",
            "propertyTypes": ["house", "land"],
            "priceMin": 100000,
            "priceMax": 500000,
            "surfaceMin": 40,
            "surfaceMax": 120,
            "rooms": [2, 3],
            "bedrooms": [1],
        }

        native = SeLogerParser().to_native(criteria)

        assert native == {
            "placeIds": ["AD08FR31096"],
            "distributionTypes": ["Sale"],
            "estateTypes": ["House", "Land"],
            "priceMin": 100000,
            "priceMax": 500000,
            # SeLoger nomme la surface « space »…
            "spaceMin": 40,
            "spaceMax": 120,
            # … et attend des CHAÎNES dans son query string.
            "rooms": ["2", "3"],
            "bedrooms": ["1"],
        }
        # Aucun terme canonique ne fuit dans le natif.
        for canonical_key in ("transaction", "propertyTypes", "surfaceMin", "surfaceMax", "locations"):
            assert canonical_key not in native

    @pytest.mark.parametrize(
        ("transaction", "expected"),
        [
            ("rent", ["Rent"]),
            ("buy", ["Sale"]),
            # Hors vocabulaire ou absent : la clé n'est pas inventée, SeLoger
            # rendra alors location + achat comme sur son formulaire nu.
            (None, None),
            ("", None),
            ("troc", None),
            ("Rent", None),
        ],
    )
    def test_transaction_translation(self, transaction, expected):
        native = SeLogerParser().to_native({**manual(["X"]), "transaction": transaction})
        assert native.get("distributionTypes") == expected

    @pytest.mark.parametrize(
        ("property_types", "expected"),
        [
            (["apartment"], ["Apartment"]),
            (["house"], ["House"]),
            (["parking"], ["Parking"]),
            (["land"], ["Land"]),
            # L'ordre demandé est conservé.
            (["land", "apartment"], ["Land", "Apartment"]),
            (["apartment", "house", "parking", "land"], ["Apartment", "House", "Parking", "Land"]),
            # Les types inconnus sont filtrés, sans exception.
            (["apartment", "yacht"], ["Apartment"]),
            (["yacht"], None),
            ([], None),
            (None, None),
        ],
    )
    def test_estate_types_are_filtered_on_the_known_vocabulary(self, property_types, expected):
        native = SeLogerParser().to_native({**manual(["X"]), "propertyTypes": property_types})
        assert native.get("estateTypes") == expected

    @pytest.mark.parametrize(
        ("canonical_key", "native_key"),
        [
            ("priceMin", "priceMin"),
            ("priceMax", "priceMax"),
            ("surfaceMin", "spaceMin"),
            ("surfaceMax", "spaceMax"),
        ],
    )
    @pytest.mark.parametrize("value", [0, 1, 1500])
    def test_bounds_are_forwarded_including_zero(self, canonical_key, native_key, value):
        """`is not None` et non la vérité de la valeur : un prix minimum de 0 est
        une borne explicite (« gratuit compris »), pas une absence de critère."""
        native = SeLogerParser().to_native({**manual(["X"]), canonical_key: value})
        assert native[native_key] == value

    @pytest.mark.parametrize(
        ("canonical_key", "native_key"),
        [
            ("priceMin", "priceMin"),
            ("priceMax", "priceMax"),
            ("surfaceMin", "spaceMin"),
            ("surfaceMax", "spaceMax"),
        ],
    )
    def test_absent_bounds_are_not_invented(self, canonical_key, native_key):
        native = SeLogerParser().to_native({**manual(["X"]), canonical_key: None})
        assert native_key not in native
        assert native_key not in SeLogerParser().to_native(manual(["X"]))

    @pytest.mark.parametrize("key", ["rooms", "bedrooms"])
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ([2, 3], ["2", "3"]),
            (["2", "3"], ["2", "3"]),
            ([5], ["5"]),
            # Une liste non vide reste transmise, même si elle contient 0.
            ([0], ["0"]),
            ([], None),
            (None, None),
        ],
    )
    def test_room_counts_become_strings(self, key, value, expected):
        native = SeLogerParser().to_native({**manual(["X"]), key: value})
        assert native.get(key) == expected

    def test_manual_place_ids_are_never_overridden(self):
        criteria = {**manual(["AD08FR12345"]), "locations": [PARIS_15]}
        with patch("services.seloger_geocode.resolve_place_id") as mock_resolve:
            native = SeLogerParser().to_native(criteria)

        assert native["placeIds"] == ["AD08FR12345"]
        mock_resolve.assert_not_called()

    def test_place_ids_are_resolved_from_the_locations(self):
        parser = resolving_parser()
        with patch(
            "services.seloger_geocode.resolve_place_id",
            side_effect=["AD08FR31096", "AD08FR99999"],
        ):
            native = parser.to_native({"locations": [PARIS_15, LYON_7]})
        assert native["placeIds"] == ["AD08FR31096", "AD08FR99999"]

    def test_no_place_ids_key_at_all_when_nothing_resolves(self):
        """Surtout pas `placeIds: []` : c'est `not native.get("placeIds")` que
        scrape() et build_search_url() consultent, la clé doit rester absente."""
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value=None):
            native = parser.to_native({"locations": [NOWHERE]})
        assert "placeIds" not in native

    def test_a_location_typed_by_hand_still_resolves_by_postal_code(self):
        """# BUG corrigé : sans code INSEE, la localisation était traitée
        comme non identifiable et ne déclenchait AUCUN appel réseau, alors
        que `_find_city_place_id` résout déjà par le seul code postal (voir
        services.seloger_geocode.area_cache_key). Elle doit désormais
        déclencher la même résolution qu'une localisation choisie dans les
        suggestions."""
        parser = resolving_parser()
        with patch("services.seloger_geocode._resolve_uncached", return_value="AD09FR40") as mock_lookup:
            native = parser.to_native({"locations": [TYPED_BY_HAND]})
        assert native["placeIds"] == ["AD09FR40"]
        mock_lookup.assert_called_once_with(TYPED_BY_HAND)

    def test_levels_can_be_mixed_in_one_search(self):
        parser = resolving_parser()
        with patch(
            "services.seloger_geocode.resolve_place_id",
            side_effect=["AD06FR34", "POCOFR4809"],
        ):
            native = parser.to_native({"locations": [GIRONDE, PARIS_15]})
        assert native["placeIds"] == ["AD06FR34", "POCOFR4809"]

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            # Une URL SeLoger collée peut porter des paramètres propres au site.
            ({"locationsInBuildingExcluded": ["Ground"]}, {"locationsInBuildingExcluded": ["Ground"]}),
            ({"order": "PriceAsc"}, {"order": "PriceAsc"}),
            # Recopiés TELS QUELS, sans traduction : ils sont déjà en natif.
            ({"rooms": ["4"]}, {"rooms": ["4"]}),
            # Les valeurs vides ne sont pas recopiées.
            ({"locationsInBuildingExcluded": []}, {}),
            ({"order": ""}, {}),
            ({"order": None}, {}),
        ],
    )
    def test_other_manual_overrides_are_forwarded_verbatim(self, overrides, expected):
        criteria = {"sourceOverrides": {"seloger": {"placeIds": ["AD08FR31096"], **overrides}}}
        native = SeLogerParser().to_native(criteria)

        for key, value in expected.items():
            assert native[key] == value
        for key in set(overrides) - set(expected):
            assert key not in native
        # `placeIds` n'est jamais recopié par cette boucle : il a déjà été posé
        # par _place_ids, et le recopier écraserait une résolution automatique.
        assert native["placeIds"] == ["AD08FR31096"]

    def test_an_override_never_overwrites_a_translated_key(self):
        """Les surcharges sont appliquées EN DERNIER : une clé native déjà
        traduite est écrasée. Comportement assumé (l'utilisateur a raison sur sa
        propre source), figé ici pour qu'il reste conscient."""
        criteria = {
            "transaction": "rent",
            "sourceOverrides": {"seloger": {
                "placeIds": ["AD08FR31096"],
                "distributionTypes": ["Sale"],
            }},
        }
        assert SeLogerParser().to_native(criteria)["distributionTypes"] == ["Sale"]

    def test_the_overrides_of_another_source_are_ignored(self):
        criteria = {
            **manual(["AD08FR31096"]),
            "sourceOverrides": {
                "seloger": {"placeIds": ["AD08FR31096"]},
                "laforet": {"cities": ["33063"]},
            },
        }
        assert "cities" not in SeLogerParser().to_native(criteria)

    def test_does_not_mutate_the_input(self):
        """Les critères sont partagés entre toutes les sources d'une recherche
        pendant un scrape."""
        parser = resolving_parser()
        criteria = {
            "locations": [dict(PARIS_15)],
            "transaction": "rent",
            "propertyTypes": ["apartment"],
            "rooms": [2, 3],
        }
        snapshot = json.loads(json.dumps(criteria))

        with patch("services.seloger_geocode.resolve_place_id", return_value="AD08FR31096"):
            parser.to_native(criteria)

        assert criteria == snapshot

    def test_empty_criteria_translate_to_an_empty_native_dict(self):
        assert SeLogerParser().to_native({}) == {}


# ---------------------------------------------------------------------------
# scrape
# ---------------------------------------------------------------------------

class TestScrape:
    ERROR_MATCH = r"Aucun lieu SeLoger n'a pu être déterminé"

    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}, {"priceMax": 1500}, {"locations": [TYPED_BY_HAND]}],
        ids=["vide", "liste_vide", "sans_localisation", "sans_code_insee"],
    )
    def test_no_place_id_raises_instead_of_searching_all_of_france(self, criteria):
        """L'INVARIANT CRITIQUE : une recherche SeLoger sans `locations=` n'est
        pas une recherche vide, c'est une recherche sur la France entière. Elle
        doit échouer bruyamment pour que ScrapeService la signale."""
        parser = resolving_parser()
        with patch("scraper.seloger.scrape") as mock_scrape:
            with pytest.raises(ValueError, match=self.ERROR_MATCH):
                parser.scrape(criteria)
        mock_scrape.assert_not_called()

    def test_an_unresolvable_location_raises_too(self):
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value=None):
            with pytest.raises(ValueError, match=self.ERROR_MATCH):
                parser.scrape({"locations": [NOWHERE]})

    def test_without_storage_no_place_id_is_invented(self):
        with pytest.raises(ValueError, match=self.ERROR_MATCH):
            SeLogerParser().scrape({"locations": [PARIS_15]})

    def test_a_manual_place_id_needs_no_storage_at_all(self):
        """Le repli manuel doit rester utilisable même sans base."""
        with patch("scraper.seloger.scrape", return_value=[]) as mock_scrape:
            assert SeLogerParser().scrape(manual(["AD08FR31096"])) == []
        assert mock_scrape.call_args[0][0]["placeIds"] == ["AD08FR31096"]

    def test_scrapes_with_an_auto_resolved_place_id(self):
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value="AD08FR31096"):
            with patch("scraper.seloger.scrape", return_value=[]) as mock_scrape:
                parser.scrape({"locations": [PARIS_15]})
        assert mock_scrape.call_args[0][0]["placeIds"] == ["AD08FR31096"]

    def test_the_scraper_only_ever_sees_native_criteria(self):
        criteria = {
            **manual(["AD08FR31096"]),
            "transaction": "rent",
            "surfaceMin": 30,
            "propertyTypes": ["apartment"],
        }
        with patch("scraper.seloger.scrape", return_value=[]) as mock_scrape:
            SeLogerParser().scrape(criteria)

        native = mock_scrape.call_args[0][0]
        assert native["distributionTypes"] == ["Rent"]
        assert native["spaceMin"] == 30
        assert native["estateTypes"] == ["Apartment"]
        assert "transaction" not in native
        assert "surfaceMin" not in native
        assert "propertyTypes" not in native

    def test_results_are_converted_and_deduplicated_in_order(self):
        """Le même identifiant peut revenir d'une page à l'autre : la première
        occurrence gagne, l'ordre du site est préservé."""
        detailed = [
            {"id": "1", "title": "un"},
            {"id": "2", "title": "deux"},
            {"id": "1", "title": "un (doublon)"},
            {"id": "3", "title": "trois"},
            {"id": "2", "title": "deux (doublon)"},
        ]
        with patch("scraper.seloger.scrape", return_value=detailed):
            listings = SeLogerParser().scrape(manual(["AD08FR31096"]))

        assert [li.listing_id for li in listings] == ["sl_1", "sl_2", "sl_3"]
        assert [li.title for li in listings] == ["un", "deux", "trois"]

    def test_an_empty_result_is_a_legitimate_empty_list(self):
        with patch("scraper.seloger.scrape", return_value=[]):
            assert SeLogerParser().scrape(manual(["AD08FR31096"])) == []

    @pytest.mark.parametrize(
        "error",
        [
            ValueError("Ton IP est bloquée par DataDome"),
            ConnectionError("réseau coupé"),
            KeyError("pageProps"),
        ],
        ids=["anti_bot", "reseau", "format_inattendu"],
    )
    def test_scraper_errors_propagate_instead_of_becoming_an_empty_list(self, error):
        """Une liste vide serait indistinguable d'une recherche légitimement
        sans résultat : ScrapeService ne pourrait plus signaler l'échec."""
        with patch("scraper.seloger.scrape", side_effect=error):
            with pytest.raises(type(error)):
                SeLogerParser().scrape(manual(["AD08FR31096"]))

    def test_the_scraper_is_called_exactly_once(self):
        with patch("scraper.seloger.scrape", return_value=[]) as mock_scrape:
            SeLogerParser().scrape(manual(["AD08FR31096"]))
        assert mock_scrape.call_count == 1


# ---------------------------------------------------------------------------
# build_search_url
# ---------------------------------------------------------------------------

class TestBuildSearchUrl:
    """Régression : build_search_url() transmettait les critères stockés
    directement à scraper.seloger.build_search_url() sans résoudre les placeIds.
    Comme la résolution n'est pas persistée dans la recherche, une recherche
    créée via l'autocomplete (seulement `locations`) produisait en silence une
    URL sans aucun `locations=` : une recherche SeLoger nationale, présentée à
    l'utilisateur comme « l'URL de sa recherche »."""

    def test_resolves_place_ids_before_building_the_url(self):
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value="AD08FR31096"):
            url = parser.build_search_url({"locations": [PARIS_15]})
        assert url is not None
        assert "locations=AD08FR31096" in url

    def test_manual_place_ids_work_without_any_resolution_call(self):
        with patch("services.seloger_geocode.resolve_place_id") as mock_resolve:
            url = SeLogerParser().build_search_url(manual(["AD08FR31096"]))
        assert "locations=AD08FR31096" in url
        mock_resolve.assert_not_called()

    def test_the_order_is_forced_to_the_most_recent_first(self):
        """DateDesc : le lien montré doit refléter ce que le scraper lit, et le
        scraper ne lit que les annonces les plus récentes."""
        url = SeLogerParser().build_search_url(manual(["AD08FR31096"]))
        assert "order=DateDesc" in url

    def test_the_url_carries_the_translated_criteria(self):
        criteria = {
            **manual(["AD08FR31096"]),
            "transaction": "buy",
            "propertyTypes": ["house"],
            "priceMax": 500000,
            "surfaceMin": 40,
        }
        url = SeLogerParser().build_search_url(criteria)

        assert "distributionTypes=Sale" in url
        assert "estateTypes=House" in url
        assert "priceMax=500000" in url
        assert "spaceMin=40" in url
        assert "surfaceMin" not in url

    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}, {"locations": [TYPED_BY_HAND]}],
        ids=["vide", "liste_vide", "sans_code_insee"],
    )
    def test_none_rather_than_an_unscoped_nationwide_url(self, criteria):
        assert resolving_parser().build_search_url(criteria) is None

    def test_none_when_the_resolution_finds_nothing(self):
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value=None):
            assert parser.build_search_url({"locations": [NOWHERE]}) is None

    def test_build_search_urls_is_empty_not_a_list_holding_none(self):
        """L'implémentation par défaut de BaseParser enveloppe
        build_search_url() : elle doit revenir vide, pas `[None]`."""
        parser = resolving_parser()
        with patch("services.seloger_geocode.resolve_place_id", return_value=None):
            assert parser.build_search_urls({"locations": [NOWHERE]}) == []

    def test_build_search_urls_wraps_the_single_url(self):
        urls = SeLogerParser().build_search_urls(manual(["AD08FR31096"]))
        assert len(urls) == 1
        assert "locations=AD08FR31096" in urls[0]

    def test_several_place_ids_are_all_in_the_url(self):
        """Joints par une virgule dans une seule occurrence de `locations=` :
        c'est le seul format que SeLoger honore pour toutes les villes
        demandées (des occurrences répétées ne gardent que la première)."""
        url = SeLogerParser().build_search_url(manual(["AD08FR31096", "AD08FR36603"]))
        assert "locations=AD08FR31096%2CAD08FR36603" in url


# ---------------------------------------------------------------------------
# has_valid_criteria : validation SANS réseau
# ---------------------------------------------------------------------------

class TestHasValidCriteria:
    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            # Un placeId manuel suffit à lui seul, sans aucune localisation.
            (manual(["AD08FR31096"]), True),
            ({"sourceOverrides": {"seloger": {"placeIds": []}}}, False),
            # Périmètres issus de l'autocomplete : tous identifiables.
            ({"locations": [PARIS_15]}, True),
            ({"locations": [PARIS_WHOLE]}, True),
            ({"locations": [GIRONDE]}, True),
            ({"locations": [IDF]}, True),
            ({"locations": [PARIS_15, LYON_7]}, True),
            # Un seul périmètre identifiable suffit (`any`).
            ({"locations": [TYPED_BY_HAND, PARIS_15]}, True),
            # Ville tapée à la main, ou vieux format à plat (recherche créée
            # avant l'autocomplete unifié) : pas de code INSEE, mais
            # ville + code postal suffisent (voir area_cache_key) — # BUG
            # corrigé : ces deux cas rendaient auparavant False.
            ({"locations": [TYPED_BY_HAND]}, True),
            ({"city": "Paris", "postalCode": "75015"}, True),
            # Périmètre large sans son code : inexploitable.
            ({"locations": [{"kind": "department", "name": "Gironde"}]}, False),
            ({"locations": []}, False),
            ({}, False),
            ({"priceMax": 1500}, False),
        ],
    )
    def test_an_identifiable_perimeter_is_enough(self, criteria, expected):
        assert SeLogerParser().has_valid_criteria(criteria) is expected

    @pytest.mark.parametrize(
        "criteria",
        [
            manual(["AD08FR31096"]),
            {"locations": [PARIS_15]},
            {"locations": [TYPED_BY_HAND]},
            {"locations": [GIRONDE]},
            {},
        ],
        ids=["manuel", "code_postal", "sans_insee", "departement", "vide"],
    )
    def test_validation_never_touches_the_network_nor_the_cache(self, criteria):
        """La résolution est tentée au moment du SCRAPE, pas ici : créer une
        recherche ne doit pas dépendre d'un appel à SeLoger. Ce test échoue si
        quelqu'un « améliore » has_valid_criteria en y résolvant le placeId.
        """
        storage = fake_storage()
        parser = SeLogerParser(storage=storage)

        with patch("services.seloger_geocode._query_autocomplete") as mock_http:
            with patch("services.seloger_geocode.resolve_place_id") as mock_resolve:
                parser.has_valid_criteria(criteria)

        mock_http.assert_not_called()
        mock_resolve.assert_not_called()
        storage.seloger_geo.get_cached.assert_not_called()
        storage.seloger_geo.set_cached.assert_not_called()


# ---------------------------------------------------------------------------
# cannot_search_reason : le message vu par l'utilisateur
# ---------------------------------------------------------------------------

class TestCannotSearchReason:
    def test_none_when_the_search_is_usable(self):
        assert SeLogerParser().cannot_search_reason({"locations": [PARIS_15]}) is None
        assert SeLogerParser().cannot_search_reason(manual(["AD08FR31096"])) is None

    def test_a_location_without_an_insee_code_is_usable_not_rejected(self):
        """# BUG corrigé : ce message ("pas de code INSEE") s'affichait pour
        toute localisation tapée à la main ou héritée d'une recherche créée
        avant l'autocomplete unifié, alors que ville + code postal suffisent
        à résoudre le placeId (voir services.seloger_geocode.area_cache_key)."""
        assert SeLogerParser().cannot_search_reason({"locations": [TYPED_BY_HAND]}) is None

    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}, {"priceMax": 1500}],
        ids=["vide", "liste_vide", "sans_localisation"],
    )
    def test_no_location_at_all_gets_the_generic_message(self, criteria):
        """Distinct du précédent : sans aucune localisation, parler de code INSEE
        n'aiderait pas — il faut d'abord en choisir une."""
        assert SeLogerParser().cannot_search_reason(criteria) == (
            "aucune localisation exploitable (ville + code postal requis)"
        )

    def test_seloger_supports_every_property_type_and_transaction(self):
        """Les quatre types de bien et les deux transactions : rien à refuser."""
        criteria = {
            "locations": [PARIS_15],
            "transaction": "buy",
            "propertyTypes": ["apartment", "house", "parking", "land"],
        }
        assert SeLogerParser().cannot_search_reason(criteria) is None
        assert SeLogerParser().unsupported_criteria(criteria) == []

    def test_the_capability_message_would_still_be_reachable(self, monkeypatch):
        """SeLoger ne déclare aujourd'hui aucune limite, mais le second volet du
        contrat hérité de BaseParser doit rester branché — ce test le prouve en
        restreignant temporairement les capacités déclarées."""
        monkeypatch.setattr(SeLogerParser, "SUPPORTED_PROPERTY_TYPES", ("apartment",))
        criteria = {"locations": [PARIS_15], "propertyTypes": ["parking"]}

        assert SeLogerParser().cannot_search_reason(criteria) == (
            "SeLoger ne référence pas les biens de type « Parking »"
        )

    def test_the_location_is_checked_before_the_capabilities(self, monkeypatch):
        monkeypatch.setattr(SeLogerParser, "SUPPORTED_PROPERTY_TYPES", ("apartment",))
        assert SeLogerParser().cannot_search_reason({"propertyTypes": ["parking"]}) == (
            "aucune localisation exploitable (ville + code postal requis)"
        )


# ---------------------------------------------------------------------------
# parse() : interface héritée
# ---------------------------------------------------------------------------

class TestLegacyParse:
    def test_parse_from_html_is_no_longer_supported(self):
        """SeLoger ne se parse plus depuis du HTML : les données sont dans le
        JSON embarqué, et le point d'entrée est scrape(criteria)."""
        with pytest.raises(NotImplementedError, match=r"Utilisez scrape\(criteria\)"):
            SeLogerParser().parse("<html></html>")


# ---------------------------------------------------------------------------
# Aucune dépendance à Flask
# ---------------------------------------------------------------------------

class TestNoFlaskDependency:
    """Régression : le parser lisait son cache d'identifiants de lieu via
    `flask.current_app`. Or le scraping tourne sur un thread de fond, sans
    contexte d'application (ScrapeService est soumis à un ThreadPoolExecutor,
    voir core.scrape_control) — tous les scrapes automatiques de SeLoger
    échouaient donc sur « Working outside of application context », y compris
    ceux du scheduler.

    Ces tests tournent volontairement hors de toute app Flask : ils échouent si
    une dépendance à current_app est réintroduite.
    """

    @staticmethod
    def _storage_with_cache(place_id: str) -> MagicMock:
        storage = fake_storage()
        storage.seloger_geo.get_cached.return_value = {"place_id": place_id, "resolved_at": None}
        return storage

    def test_scrape_works_without_an_app_context(self):
        parser = SeLogerParser(storage=self._storage_with_cache("AD08FR31096"))
        with patch("scraper.seloger.scrape", return_value=[]) as mock_scrape:
            parser.scrape({"locations": [PARIS_15]})
        assert mock_scrape.call_args[0][0]["placeIds"] == ["AD08FR31096"]

    def test_the_resolution_is_read_from_the_injected_storage(self):
        storage = self._storage_with_cache("AD08FR31096")
        parser = SeLogerParser(storage=storage)

        assert parser.to_native({"locations": [PARIS_15]})["placeIds"] == ["AD08FR31096"]
        # La clé de cache d'une commune est son code INSEE nu (convention
        # d'avant les périmètres larges, conservée pour ne pas invalider le
        # cache existant — voir services.seloger_geocode.area_cache_key).
        storage.seloger_geo.get_cached.assert_called_once_with("75115")

    def test_no_module_reads_the_flask_application_context(self):
        import ast
        import inspect

        import parsers.seloger

        # Analyse de l'AST plutôt que du texte : les modules *documentent* en
        # commentaire pourquoi ils n'utilisent pas current_app.
        tree = ast.parse(inspect.getsource(parsers.seloger))

        assert [
            node for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "flask"
        ] == [], "parsers/seloger.py ne doit rien importer de flask"
        assert not [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "current_app"
        ], "parsers/seloger.py ne doit pas lire le contexte d'application Flask"
