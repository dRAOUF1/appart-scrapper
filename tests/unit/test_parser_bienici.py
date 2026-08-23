"""Tests unitaires de parsers/bienici.py.

Même structure que tests/unit/test_parser_seloger.py : bienici partage
exactement le même contrat de localisation (zoneId opaque, résolu via un
cache persistant en base, avec repli manuel) que SeLoger — seul le
vocabulaire natif change (filterType/propertyType/zoneIdsByTypes au lieu de
distributionTypes/estateTypes/placeIds).

Invariants qui comptent, comme pour SeLoger :

* `scrape()` LÈVE quand aucun zoneId n'a pu être déterminé : une recherche
  bienici sans zoneId n'est pas une recherche vide ;
* `has_valid_criteria()` ne fait AUCUN appel réseau ;
* le cache de zoneIds est lu depuis le `storage` injecté, jamais depuis
  `flask.current_app` ;
* une localisation qui ne se résout pas est simplement omise.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from parsers.bienici import BienIciParser, _dict_to_listing
from tests.helpers.factories import (
    make_city_location,
    make_criteria,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)
from tests.helpers.fakes import fake_storage

# --- Périmètres réutilisés -------------------------------------------------
PARIS_15 = make_city_location("Paris", "75015", "75115")
LYON_7 = make_city_location("Lyon", "69007", "69387")
NOWHERE = make_city_location("Nawak", "99999", "99999")
TYPED_BY_HAND = {"kind": "city", "city": "Paris", "postalCode": "75015"}

GIRONDE = make_department_location("33", "Gironde")
IDF = make_region_location("11", "Île-de-France", ("75", "77", "78", "91", "92", "93", "94", "95"))
PARIS_WHOLE = make_whole_city_location("Paris", ("75001", "75015"), "75056")

# Forme exacte d'une annonce renvoyée par scraper.bienici.scrape() — recopiée
# depuis la structure réelle observée le 26/07/2026 sur realEstateAds.json.
BIENICI_AD = {
    "id": "ag754691-538488038",
    "reference": "2079871",
    "title": "Bel appartement 3 pièces",
    "description": "Au calme, dernier étage avec ascenseur, proche métro.",
    "price": 1850,
    "transactionType": "rent",
    "surfaceArea": 65,
    "roomsQuantity": 3,
    "city": "Paris 15e",
    "postalCode": "75015",
    "propertyType": "flat",
    "district": {"name": "Paris 15e Arrondissement", "insee_code": "75115"},
    "photos": [
        {"url_photo": "https://images.playiad.com/1.jpg", "url": "https://file.bienici.com/photo/1.jpg"},
        {"url_photo": "https://images.playiad.com/2.jpg", "url": "https://file.bienici.com/photo/2.jpg"},
    ],
    "accountType": "agency",
    "accountDisplayName": "Agence du 15e",
    "energyClassification": "C",
    "greenhouseGazClassification": "B",
    "newProperty": True,
    "isBienIciExclusive": False,
    "with3dModel": True,
    "publicationDate": "2026-07-01T00:55:44.275Z",
    "modificationDate": "2026-07-02T05:15:36.301Z",
}


def manual(zone_ids: list[str]) -> dict:
    """Des critères ne portant qu'un zoneId saisi à la main."""
    return {"sourceOverrides": {"bienici": {"zoneIds": zone_ids}}}


def resolving_parser() -> BienIciParser:
    """Un parser doté d'un storage : la résolution automatique s'appuie sur
    le cache en base, donc sur le storage injecté (jamais sur
    flask.current_app)."""
    return BienIciParser(storage=fake_storage())


@pytest.fixture
def logged():
    from loguru import logger

    records: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
    )
    yield records
    logger.remove(sink_id)


# ---------------------------------------------------------------------------
# _dict_to_listing : dict de scraper.bienici -> Listing commun
# ---------------------------------------------------------------------------

class TestDictToListing:
    def test_maps_the_whole_scraper_payload(self):
        listing = _dict_to_listing(BIENICI_AD)

        assert listing.listing_id == "bi_ag754691-538488038"
        assert listing.source == "bienici"
        assert listing.url == "https://www.bienici.com/annonce/ag754691-538488038"
        assert listing.title == "Bel appartement 3 pièces"
        assert listing.price == "1 850 €/mois"
        assert listing.price_value == 1850.0
        assert listing.surface == "65"
        assert listing.rooms == "3"
        assert listing.property_type == "flat"
        assert listing.city == "Paris 15e"
        assert listing.district == "Paris 15e Arrondissement"
        assert listing.zip_code == "75015"
        assert listing.agency == "Agence du 15e"
        assert listing.legacy_id == "2079871"
        assert listing.epc == "C"
        assert listing.ges == "B"
        assert listing.is_new is True
        assert listing.is_exclusive is False
        assert listing.has_3d_visit is True
        assert listing.is_private is False
        # Issue #12 : ISO+Z avec millisecondes → ISO-8601 UTC canonique.
        assert listing.creation_date == "2026-07-01T00:55:44+00:00"
        assert listing.update_date == "2026-07-02T05:15:36.301Z"
        assert listing.description == BIENICI_AD["description"]

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ({"id": "ag1-1"}, "bi_ag1-1"),
            ({}, "bi_"),
            ({"id": ""}, "bi_"),
        ],
    )
    def test_listing_id_is_always_prefixed(self, data, expected):
        assert _dict_to_listing(data).listing_id == expected

    @pytest.mark.parametrize(
        ("ad_id", "expected_url"),
        [
            ("ag1-1", "https://www.bienici.com/annonce/ag1-1"),
            ("", ""),
        ],
    )
    def test_url_is_the_id_based_permalink_or_empty(self, ad_id, expected_url):
        assert _dict_to_listing({"id": ad_id}).url == expected_url

    @pytest.mark.parametrize(
        ("price", "transaction_type", "expected"),
        [
            (1850, "rent", "1 850 €/mois"),
            (250000, "buy", "250 000 €"),
            (0, "rent", "0 €/mois"),
            (None, "rent", ""),
        ],
    )
    def test_price_is_formatted_per_transaction_type(self, price, transaction_type, expected):
        listing = _dict_to_listing({"id": "1", "price": price, "transactionType": transaction_type})
        assert listing.price == expected

    def test_price_value_stays_none_when_unknown(self):
        assert _dict_to_listing({"id": "1"}).price_value is None
        assert _dict_to_listing({"id": "1", "price": 0}).price_value == 0.0

    @pytest.mark.parametrize(
        ("photos", "expected_url"),
        [
            ([{"url": "https://file.bienici.com/1.jpg", "url_photo": "https://raw/1.jpg"}], "https://file.bienici.com/1.jpg"),
            ([{"url_photo": "https://raw/1.jpg"}], "https://raw/1.jpg"),
            ([{"alt": "sans url"}], ""),
            ([], ""),
            (None, ""),
            ([{"url": "https://file.bienici.com/1.jpg"}, {"url": "https://file.bienici.com/2.jpg"}], "https://file.bienici.com/1.jpg"),
        ],
    )
    def test_thumbnail_is_the_first_photo_s_url(self, photos, expected_url):
        assert _dict_to_listing({"id": "1", "photos": photos}).image_url == expected_url

    def test_photos_are_reserialised_as_a_flat_url_list(self):
        listing = _dict_to_listing(BIENICI_AD)
        assert json.loads(listing.photos) == [
            {"url": "https://file.bienici.com/photo/1.jpg"},
            {"url": "https://file.bienici.com/photo/2.jpg"},
        ]

    def test_no_photos_gives_an_empty_json_array(self):
        assert _dict_to_listing({"id": "1"}).photos == "[]"

    @pytest.mark.parametrize("field", ["surface", "rooms"])
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, "0"), (65, "65"), (None, "")],
    )
    def test_surface_and_rooms_are_stringified_only_when_present(self, field, value, expected):
        key = {"surface": "surfaceArea", "rooms": "roomsQuantity"}[field]
        assert getattr(_dict_to_listing({"id": "1", key: value}), field) == expected

    def test_description_is_truncated_to_300_characters(self):
        listing = _dict_to_listing({"id": "1", "description": "a" * 500})
        assert len(listing.description) == 300

    @pytest.mark.parametrize(
        ("city", "district", "expected_location"),
        [
            ("Paris 15e", {"name": "Quartier X"}, "Paris 15e"),
            ("", {"name": "Quartier X"}, "Quartier X"),
            (None, {"name": "Quartier X"}, "Quartier X"),
            ("Paris 15e", {}, "Paris 15e"),
            ("", {}, ""),
        ],
    )
    def test_location_is_the_city_or_the_district(self, city, district, expected_location):
        listing = _dict_to_listing({"id": "1", "city": city, "district": district})
        assert listing.location == expected_location

    @pytest.mark.parametrize(
        ("account_type", "expected_is_private"),
        [
            ("agency", False),
            ("network", False),
            ("mandatary", False),
            # Aucune valeur "personal"/"individual" rencontrée en direct,
            # mais tout ce qui n'est pas un des trois types pro connus est
            # traité comme un particulier plutôt que deviner une liste
            # fermée qui pourrait exclure un cas réel non encore vu.
            ("personal", True),
            (None, True),
            ("", True),
        ],
    )
    def test_account_type_determines_is_private(self, account_type, expected_is_private):
        listing = _dict_to_listing({"id": "1", "accountType": account_type})
        assert listing.is_private is expected_is_private

    @pytest.mark.parametrize("field", ["is_new", "is_exclusive", "has_3d_visit"])
    def test_booleans_default_to_false(self, field):
        assert getattr(_dict_to_listing({"id": "1"}), field) is False

    @pytest.mark.parametrize(
        ("field", "key"),
        [
            ("title", "title"),
            ("city", "city"),
            ("zip_code", "postalCode"),
            ("property_type", "propertyType"),
            ("agency", "accountDisplayName"),
            ("epc", "energyClassification"),
            ("ges", "greenhouseGazClassification"),
            # Issue #12 : creation_date sort de cette liste — une clé None
            # donne désormais la sentinelle « unknown » (parsers/_dates.py),
            # plus une chaîne vide. Voir test_parsers_dates.py.
            ("update_date", "modificationDate"),
        ],
    )
    def test_a_key_present_but_null_never_leaks_none_into_a_string_field(self, field, key):
        """Contrairement à _dict_to_listing de SeLoger, `data.get(key) or ""`
        protège aussi bien la clé absente que la clé présente valant None."""
        assert getattr(_dict_to_listing({"id": "1", key: None}), field) == ""

    def test_a_null_reference_becomes_an_empty_string_not_the_string_none(self):
        assert _dict_to_listing({"id": "1", "reference": None}).legacy_id == ""


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
            ("-7444", {"zoneIds": ["-7444"]}),
            ("  -7444  ", {"zoneIds": ["-7444"]}),
            ("-7444, -9520", {"zoneIds": ["-7444", "-9520"]}),
            ("-7444 ,-9520", {"zoneIds": ["-7444", "-9520"]}),
            ("-7444,,   ,-9520", {"zoneIds": ["-7444", "-9520"]}),
            ("-7444,", {"zoneIds": ["-7444"]}),
            (",", {}),
            (" , , ", {}),
        ],
    )
    def test_comma_separated_zone_ids(self, value, expected):
        assert BienIciParser().parse_manual_override(value) == expected


# ---------------------------------------------------------------------------
# remember_manual_override : capitaliser la saisie manuelle
# ---------------------------------------------------------------------------

class TestRememberManualOverride:
    def test_banks_the_zone_ids_against_the_injected_repo(self):
        storage = fake_storage()
        parser = BienIciParser(storage=storage)
        criteria = {**manual(["-9520"]), "locations": [PARIS_15]}

        with patch("services.bienici_geocode.remember_manual_zone_ids") as mock_remember:
            assert parser.remember_manual_override(criteria) is None

        mock_remember.assert_called_once_with(criteria, repo=storage.bienici_geo)

    @pytest.mark.parametrize("storage", [None, object()], ids=["sans_storage", "storage_sans_repo_geo"])
    def test_without_a_repo_nothing_is_attempted(self, storage):
        parser = BienIciParser(storage=storage)
        with patch("services.bienici_geocode.remember_manual_zone_ids") as mock_remember:
            assert parser.remember_manual_override(make_criteria()) is None
        mock_remember.assert_not_called()

    def test_a_failure_is_logged_but_never_raised(self, logged):
        parser = BienIciParser(storage=fake_storage())

        with patch(
            "services.bienici_geocode.remember_manual_zone_ids",
            side_effect=RuntimeError("banque indisponible"),
        ):
            assert parser.remember_manual_override(make_criteria()) is None

        assert any(
            level == "DEBUG" and "non mémorisé" in message and "banque indisponible" in message
            for level, message in logged
        )


# ---------------------------------------------------------------------------
# _zone_ids : le cœur de la localisation bienici
# ---------------------------------------------------------------------------

class TestZoneIds:
    def test_a_manual_zone_id_short_circuits_every_resolution(self):
        storage = fake_storage()
        parser = BienIciParser(storage=storage)

        with patch("services.bienici_geocode.resolve_zone_ids") as mock_resolve:
            zone_ids = parser._zone_ids({**manual(["-12345"]), "locations": [PARIS_15, LYON_7]})

        assert zone_ids == ["-12345"]
        mock_resolve.assert_not_called()
        storage.bienici_geo.get_cached.assert_not_called()

    def test_the_manual_list_is_copied_not_aliased(self):
        criteria = manual(["-12345"])
        zone_ids = BienIciParser()._zone_ids(criteria)

        zone_ids.append("-99999")
        assert criteria["sourceOverrides"]["bienici"]["zoneIds"] == ["-12345"]

    @pytest.mark.parametrize(
        "criteria",
        [
            {},
            {"locations": []},
            {"priceMax": 1500},
            {"locations": [{"city": "Paris"}]},
            {"sourceOverrides": {"bienici": {"zoneIds": []}}},
        ],
        ids=["vide", "liste_vide", "sans_localisation", "localisation_incomplete", "surcharge_vide"],
    )
    def test_no_location_means_no_zone_id_and_no_call(self, criteria):
        storage = fake_storage()
        parser = BienIciParser(storage=storage)

        with patch("services.bienici_geocode.resolve_zone_ids") as mock_resolve:
            assert parser._zone_ids(criteria) == []

        mock_resolve.assert_not_called()
        storage.bienici_geo.get_cached.assert_not_called()

    def test_without_a_repo_the_impossibility_is_logged(self, logged):
        parser = BienIciParser()

        with patch("services.bienici_geocode.resolve_zone_ids") as mock_resolve:
            assert parser._zone_ids({"locations": [PARIS_15]}) == []

        mock_resolve.assert_not_called()
        assert any(
            level == "WARNING" and "Aucun storage fourni au parser" in message
            for level, message in logged
        )

    def test_a_storage_without_the_geo_repository_is_treated_as_absent(self):
        parser = BienIciParser(storage=object())
        assert parser._geo_repo() is None
        assert parser._zone_ids({"locations": [PARIS_15]}) == []

    def test_the_repo_comes_from_the_injected_storage(self):
        storage = fake_storage()
        assert BienIciParser(storage=storage)._geo_repo() is storage.bienici_geo
        assert BienIciParser()._geo_repo() is None

    def test_one_resolution_per_perimeter_in_order(self):
        parser = resolving_parser()
        with patch(
            "services.bienici_geocode.resolve_zone_ids",
            side_effect=[["-7444"], ["-99999"]],
        ) as mock_resolve:
            zone_ids = parser._zone_ids({"locations": [PARIS_15, LYON_7]})

        assert zone_ids == ["-7444", "-99999"]
        assert mock_resolve.call_count == 2
        assert [call.args[0] for call in mock_resolve.call_args_list] == [PARIS_15, LYON_7]

    def test_duplicates_across_locations_are_collapsed_keeping_the_first_position(self):
        parser = resolving_parser()
        with patch(
            "services.bienici_geocode.resolve_zone_ids",
            side_effect=[["-7444"], ["-9520"], ["-7444"]],
        ):
            zone_ids = parser._zone_ids({"locations": [PARIS_15, LYON_7, dict(PARIS_15)]})

        assert zone_ids == ["-7444", "-9520"]

    def test_a_single_location_can_resolve_to_several_zone_ids(self):
        """Contrairement au placeId singulier de SeLoger, une résolution
        bienici peut rendre plusieurs zoneIds (ex. une région = union de
        départements) : ils doivent tous être retenus, pas seulement le
        premier."""
        parser = resolving_parser()
        with patch("services.bienici_geocode.resolve_zone_ids", return_value=["-1", "-2", "-3"]):
            assert parser._zone_ids({"locations": [IDF]}) == ["-1", "-2", "-3"]

    @pytest.mark.parametrize(
        ("resolved", "expected"),
        [
            ([["-7444"], None], ["-7444"]),
            ([None, ["-9520"]], ["-9520"]),
            ([None, None], []),
            ([["-7444"], []], ["-7444"]),
        ],
    )
    def test_an_unresolved_perimeter_is_omitted_not_fatal(self, resolved, expected):
        parser = resolving_parser()
        with patch("services.bienici_geocode.resolve_zone_ids", side_effect=resolved):
            assert parser._zone_ids({"locations": [PARIS_15, NOWHERE]}) == expected

    @pytest.mark.parametrize(
        "location",
        [PARIS_15, PARIS_WHOLE, GIRONDE, IDF],
        ids=["code_postal", "ville_entiere", "departement", "region"],
    )
    def test_one_resolution_covers_a_whole_perimeter_at_any_level(self, location):
        parser = resolving_parser()
        with patch("services.bienici_geocode.resolve_zone_ids", return_value=["-1"]) as mock_resolve:
            assert parser._zone_ids({"locations": [location]}) == ["-1"]
        assert mock_resolve.call_count == 1


# ---------------------------------------------------------------------------
# to_native : canonique -> vocabulaire bienici
# ---------------------------------------------------------------------------

class TestToNative:
    def test_translates_the_whole_canonical_vocabulary(self):
        criteria = {
            **manual(["-7444"]),
            "transaction": "buy",
            "propertyTypes": ["house", "land"],
            "priceMin": 100000,
            "priceMax": 500000,
            "surfaceMin": 40,
            "surfaceMax": 120,
            "rooms": [2, 3],
            "bedrooms": [1],
        }

        native = BienIciParser().to_native(criteria)

        assert native == {
            "onTheMarket": [True],
            "zoneIdsByTypes": {"zoneIds": ["-7444"]},
            "filterType": "buy",
            "propertyType": ["house", "land"],
            "minPrice": 100000,
            "maxPrice": 500000,
            "minArea": 40,
            "maxArea": 120,
            "minRooms": 2,
            "maxRooms": 3,
            "minBedrooms": 1,
            "maxBedrooms": 1,
        }
        for canonical_key in ("transaction", "propertyTypes", "surfaceMin", "surfaceMax", "locations"):
            assert canonical_key not in native

    @pytest.mark.parametrize(
        ("transaction", "expected"),
        [
            ("rent", "rent"),
            ("buy", "buy"),
            (None, None),
            ("", None),
            ("troc", None),
        ],
    )
    def test_transaction_translation(self, transaction, expected):
        native = BienIciParser().to_native({**manual(["X"]), "transaction": transaction})
        assert native.get("filterType") == expected

    @pytest.mark.parametrize(
        ("property_types", "expected"),
        [
            (["apartment"], ["flat"]),
            (["house"], ["house"]),
            (["parking"], ["parking"]),
            (["land"], ["land"]),
            (["land", "apartment"], ["land", "flat"]),
            (["apartment", "yacht"], ["flat"]),
            (["yacht"], None),
            ([], None),
            (None, None),
        ],
    )
    def test_property_types_are_filtered_on_the_known_vocabulary(self, property_types, expected):
        native = BienIciParser().to_native({**manual(["X"]), "propertyTypes": property_types})
        assert native.get("propertyType") == expected

    @pytest.mark.parametrize(
        ("canonical_key", "native_key"),
        [
            ("priceMin", "minPrice"),
            ("priceMax", "maxPrice"),
            ("surfaceMin", "minArea"),
            ("surfaceMax", "maxArea"),
        ],
    )
    @pytest.mark.parametrize("value", [0, 1, 1500])
    def test_bounds_are_forwarded_including_zero(self, canonical_key, native_key, value):
        native = BienIciParser().to_native({**manual(["X"]), canonical_key: value})
        assert native[native_key] == value

    @pytest.mark.parametrize(
        ("canonical_key", "native_key"),
        [
            ("priceMin", "minPrice"),
            ("priceMax", "maxPrice"),
            ("surfaceMin", "minArea"),
            ("surfaceMax", "maxArea"),
        ],
    )
    def test_absent_bounds_are_not_invented(self, canonical_key, native_key):
        native = BienIciParser().to_native({**manual(["X"]), canonical_key: None})
        assert native_key not in native

    @pytest.mark.parametrize("key", ["rooms", "bedrooms"])
    @pytest.mark.parametrize(
        ("value", "expected_min", "expected_max"),
        [
            ([2, 3], 2, 3),
            ([5], 5, 5),
            ([2, 5], 2, 5),
            ([0], 0, 0),
        ],
    )
    def test_room_counts_become_a_min_max_range(self, key, value, expected_min, expected_max):
        """bienici ne connaît qu'un intervalle, contrairement à SeLoger qui
        accepte une liste de valeurs exactes : une sélection non contiguë se
        traduit donc en un intervalle plus large — traduction imparfaite
        mais acceptée."""
        min_key, max_key = {"rooms": ("minRooms", "maxRooms"), "bedrooms": ("minBedrooms", "maxBedrooms")}[key]
        native = BienIciParser().to_native({**manual(["X"]), key: value})
        assert native[min_key] == expected_min
        assert native[max_key] == expected_max

    @pytest.mark.parametrize("key", ["rooms", "bedrooms"])
    @pytest.mark.parametrize("value", [[], None])
    def test_an_empty_room_count_invents_no_bounds(self, key, value):
        min_key, max_key = {"rooms": ("minRooms", "maxRooms"), "bedrooms": ("minBedrooms", "maxBedrooms")}[key]
        native = BienIciParser().to_native({**manual(["X"]), key: value})
        assert min_key not in native
        assert max_key not in native

    def test_manual_zone_ids_are_never_overridden(self):
        criteria = {**manual(["-12345"]), "locations": [PARIS_15]}
        with patch("services.bienici_geocode.resolve_zone_ids") as mock_resolve:
            native = BienIciParser().to_native(criteria)

        assert native["zoneIdsByTypes"] == {"zoneIds": ["-12345"]}
        mock_resolve.assert_not_called()

    def test_zone_ids_are_resolved_from_the_locations(self):
        parser = resolving_parser()
        with patch(
            "services.bienici_geocode.resolve_zone_ids",
            side_effect=[["-7444"], ["-99999"]],
        ):
            native = parser.to_native({"locations": [PARIS_15, LYON_7]})
        assert native["zoneIdsByTypes"] == {"zoneIds": ["-7444", "-99999"]}

    def test_no_zone_ids_by_types_key_at_all_when_nothing_resolves(self):
        parser = resolving_parser()
        with patch("services.bienici_geocode.resolve_zone_ids", return_value=None):
            native = parser.to_native({"locations": [NOWHERE]})
        assert "zoneIdsByTypes" not in native

    def test_a_location_typed_by_hand_still_resolves_by_postal_code(self):
        """# BUG corrigé : une localisation sans code INSEE (tapée à la main,
        ou héritée d'une recherche créée avant l'autocomplete unifié) était
        traitée comme non résolvable et ne déclenchait AUCUNE recherche,
        alors que `_find_city_zone_ids` résout déjà par le seul code postal
        (voir services.bienici_geocode.area_cache_key). Elle doit désormais
        déclencher la même résolution qu'une localisation choisie dans les
        suggestions."""
        parser = resolving_parser()
        with patch("services.bienici_geocode._resolve_uncached", return_value=["-7444"]) as mock_lookup:
            native = parser.to_native({"locations": [TYPED_BY_HAND]})
        assert native["zoneIdsByTypes"] == {"zoneIds": ["-7444"]}
        mock_lookup.assert_called_once_with(TYPED_BY_HAND)

    def test_the_market_filter_is_always_present(self):
        """Seules les annonces actives sont recherchées : un tracker de
        nouveautés n'a pas à remonter des annonces archivées."""
        assert BienIciParser().to_native({}) == {"onTheMarket": [True]}

    def test_does_not_mutate_the_input(self):
        parser = resolving_parser()
        criteria = {
            "locations": [dict(PARIS_15)],
            "transaction": "rent",
            "propertyTypes": ["apartment"],
            "rooms": [2, 3],
        }
        snapshot = json.loads(json.dumps(criteria))

        with patch("services.bienici_geocode.resolve_zone_ids", return_value=["-7444"]):
            parser.to_native(criteria)

        assert criteria == snapshot


# ---------------------------------------------------------------------------
# scrape
# ---------------------------------------------------------------------------

class TestScrape:
    ERROR_MATCH = r"Aucune zone bienici n'a pu être déterminée"

    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}, {"priceMax": 1500}, {"locations": [TYPED_BY_HAND]}],
        ids=["vide", "liste_vide", "sans_localisation", "sans_code_insee"],
    )
    def test_no_zone_id_raises_instead_of_searching_all_of_france(self, criteria):
        parser = resolving_parser()
        with patch("scraper.bienici.scrape") as mock_scrape:
            with pytest.raises(ValueError, match=self.ERROR_MATCH):
                parser.scrape(criteria)
        mock_scrape.assert_not_called()

    def test_an_unresolvable_location_raises_too(self):
        parser = resolving_parser()
        with patch("services.bienici_geocode.resolve_zone_ids", return_value=None):
            with pytest.raises(ValueError, match=self.ERROR_MATCH):
                parser.scrape({"locations": [NOWHERE]})

    def test_without_storage_no_zone_id_is_invented(self):
        with pytest.raises(ValueError, match=self.ERROR_MATCH):
            BienIciParser().scrape({"locations": [PARIS_15]})

    def test_a_manual_zone_id_needs_no_storage_at_all(self):
        with patch("scraper.bienici.scrape", return_value=[]) as mock_scrape:
            assert BienIciParser().scrape(manual(["-7444"])) == []
        assert mock_scrape.call_args[0][0]["zoneIdsByTypes"] == {"zoneIds": ["-7444"]}

    def test_scrapes_with_an_auto_resolved_zone_id(self):
        parser = resolving_parser()
        with patch("services.bienici_geocode.resolve_zone_ids", return_value=["-7444"]):
            with patch("scraper.bienici.scrape", return_value=[]) as mock_scrape:
                parser.scrape({"locations": [PARIS_15]})
        assert mock_scrape.call_args[0][0]["zoneIdsByTypes"] == {"zoneIds": ["-7444"]}

    def test_the_scraper_only_ever_sees_native_criteria(self):
        criteria = {
            **manual(["-7444"]),
            "transaction": "rent",
            "surfaceMin": 30,
            "propertyTypes": ["apartment"],
        }
        with patch("scraper.bienici.scrape", return_value=[]) as mock_scrape:
            BienIciParser().scrape(criteria)

        native = mock_scrape.call_args[0][0]
        assert native["filterType"] == "rent"
        assert native["minArea"] == 30
        assert native["propertyType"] == ["flat"]
        assert "transaction" not in native
        assert "surfaceMin" not in native
        assert "propertyTypes" not in native

    def test_results_are_converted_and_deduplicated_in_order(self):
        detailed = [
            {"id": "1", "title": "un"},
            {"id": "2", "title": "deux"},
            {"id": "1", "title": "un (doublon)"},
            {"id": "3", "title": "trois"},
            {"id": "2", "title": "deux (doublon)"},
        ]
        with patch("scraper.bienici.scrape", return_value=detailed):
            listings = BienIciParser().scrape(manual(["-7444"]))

        assert [li.listing_id for li in listings] == ["bi_1", "bi_2", "bi_3"]
        assert [li.title for li in listings] == ["un", "deux", "trois"]

    def test_an_empty_result_is_a_legitimate_empty_list(self):
        with patch("scraper.bienici.scrape", return_value=[]):
            assert BienIciParser().scrape(manual(["-7444"])) == []

    @pytest.mark.parametrize(
        "error",
        [ValueError("tentatives réseau épuisées"), ConnectionError("réseau coupé"), KeyError("total")],
        ids=["reseau_epuise", "reseau", "format_inattendu"],
    )
    def test_scraper_errors_propagate_instead_of_becoming_an_empty_list(self, error):
        with patch("scraper.bienici.scrape", side_effect=error):
            with pytest.raises(type(error)):
                BienIciParser().scrape(manual(["-7444"]))

    def test_the_scraper_is_called_exactly_once(self):
        with patch("scraper.bienici.scrape", return_value=[]) as mock_scrape:
            BienIciParser().scrape(manual(["-7444"]))
        assert mock_scrape.call_count == 1


# ---------------------------------------------------------------------------
# build_search_url
# ---------------------------------------------------------------------------

class TestBuildSearchUrl:
    def test_the_url_is_anchored_on_the_first_resolvable_location(self):
        url = BienIciParser().build_search_url(
            {"locations": [PARIS_15], "transaction": "rent", "propertyTypes": ["apartment"]}
        )
        assert url == "https://www.bienici.com/recherche/location/paris-75015/appartement?tri=publication-desc"

    @pytest.mark.parametrize(
        ("transaction", "slug"), [("rent", "location"), ("buy", "achat"), (None, "location")],
    )
    def test_the_transaction_slug(self, transaction, slug):
        url = BienIciParser().build_search_url({"locations": [PARIS_15], "transaction": transaction})
        assert f"/recherche/{slug}/" in url

    @pytest.mark.parametrize(
        ("property_types", "slug"),
        [
            (["apartment"], "appartement"),
            (["house"], "maisonvilla"),
            (["parking"], "parking"),
            (["land"], "terrain"),
            ([], "appartement"),
            (None, "appartement"),
            # Le premier type demandé gouverne le slug, comme Laforêt.
            (["house", "land"], "maisonvilla"),
        ],
    )
    def test_the_property_type_slug(self, property_types, slug):
        url = BienIciParser().build_search_url({"locations": [PARIS_15], "propertyTypes": property_types})
        assert f"/{slug}?" in url

    def test_accents_and_spaces_in_the_city_name_are_slugified(self):
        location = make_city_location("Saint-Étienne-du-Rouvray", "76800", "76675")
        url = BienIciParser().build_search_url({"locations": [location]})
        assert "saint-etienne-du-rouvray-76800" in url

    def test_a_whole_city_anchors_on_the_generic_department_postal_code(self):
        """# BUG corrigé : le plus petit code postal trié (75001, un
        arrondissement précis) était utilisé pour « toute la ville », ce qui
        montrait un lien « Paris 1er » quand l'utilisateur avait choisi
        « Paris » (ville entière). Vérifié en direct sur un lien réel de
        recherche bienici multi-localisation : le code générique du
        département (paris-75000) est ce que bienici utilise pour désigner
        une ville entière, jamais le premier arrondissement."""
        url = BienIciParser().build_search_url({"locations": [PARIS_WHOLE]})
        assert "paris-75000" in url
        assert "75001" not in url

    def test_a_whole_city_with_a_three_digit_department_code_pads_correctly(self):
        """Un DOM (ex. La Réunion, département 974) a un code à 3 chiffres,
        pas 2 : le code générique reste sur 5 chiffres (97400, pas 9740)."""
        location = make_whole_city_location("Saint-Denis", ("97400", "97490"), "97411")
        url = BienIciParser().build_search_url({"locations": [location]})
        assert "saint-denis-97400" in url

    def test_a_department_uses_its_name_and_code_with_no_network_call(self):
        """Vérifié en direct : bienici.com/recherche/achat/gironde-33 — le
        nom slugifié suivi du code département, aucune ville de repli
        nécessaire (contrairement à Laforêt qui doit ancrer un chemin par
        ville faute d'URL native par département)."""
        with patch("core.geocode.department_main_city") as mock_main:
            url = BienIciParser().build_search_url({"locations": [GIRONDE], "transaction": "buy"})
        assert url.startswith("https://www.bienici.com/recherche/achat/gironde-33/")
        mock_main.assert_not_called()

    def test_a_region_uses_its_name_alone_with_no_department_code(self):
        """Vérifié en direct : bienici.com/recherche/location/ile-de-france —
        pas de code, contrairement au département."""
        url = BienIciParser().build_search_url({"locations": [IDF]})
        assert url.startswith("https://www.bienici.com/recherche/location/ile-de-france/")

    @pytest.mark.parametrize(
        "criteria",
        [
            {},
            {"locations": []},
            {"locations": [{"kind": "city", "city": "Paris"}]},
            {"locations": [make_department_location(code="")]},
        ],
        ids=["vide", "liste_vide", "sans_code_postal", "departement_sans_code"],
    )
    def test_none_when_no_location_is_reconstructible(self, criteria):
        assert BienIciParser().build_search_url(criteria) is None

    def test_url_anchor_returns_none_for_a_whole_city_without_postal_codes(self):
        """`get_locations()` filtre déjà les périmètres incomplets en amont,
        mais `_url_anchor` ne doit pas non plus lever si on l'appelle
        directement sur une entrée malformée."""
        assert BienIciParser()._url_anchor({"kind": "whole_city", "city": "Poitiers"}) is None

    def test_url_anchor_returns_none_for_a_department_without_a_name(self):
        assert BienIciParser()._url_anchor({"kind": "department", "code": "33"}) is None

    def test_url_anchor_returns_none_for_a_region_without_a_name(self):
        assert BienIciParser()._url_anchor({"kind": "region", "code": "11"}) is None

    def test_url_anchor_returns_none_for_an_unknown_kind(self):
        anchor = {"kind": "nawak", "city": "Paris", "postalCode": "75015"}
        assert BienIciParser()._url_anchor(anchor) is None

    def test_a_city_location_is_preferred_over_a_department_when_both_are_present(self):
        url = BienIciParser().build_search_url({"locations": [PARIS_15, GIRONDE]})
        assert "paris-75015" in url

    def test_no_network_call_is_ever_made(self):
        """Contrairement à to_native(), aucune résolution de zoneId n'est
        nécessaire : l'URL est purement dérivée du canonique, y compris pour
        un département/région (nom + code déjà dans les critères, plus
        besoin de department_main_city comme avant)."""
        with patch("services.bienici_geocode.resolve_zone_ids") as mock_resolve:
            BienIciParser().build_search_url({"locations": [GIRONDE]})
        mock_resolve.assert_not_called()

    def test_the_price_and_surface_bounds_are_forwarded_as_query_params(self):
        url = BienIciParser().build_search_url({
            "locations": [PARIS_15],
            "priceMin": 800, "priceMax": 8500,
            "surfaceMin": 20, "surfaceMax": 70,
        })
        assert "prix-min=800" in url
        assert "prix-max=8500" in url
        assert "surface-min=20" in url
        assert "surface-max=70" in url

    def test_absent_bounds_are_not_invented_in_the_query_string(self):
        url = BienIciParser().build_search_url({"locations": [PARIS_15]})
        assert url == "https://www.bienici.com/recherche/location/paris-75015/appartement?tri=publication-desc"

    @pytest.mark.parametrize(
        ("rooms", "expected_segment"),
        [
            ([2], "/2-pieces-et-plus"),
            ([2, 3], "/2-pieces-et-plus"),
            ([5], "/5-pieces-et-plus"),
            ([1], ""),
            ([0], ""),
            ([], ""),
            (None, ""),
        ],
    )
    def test_the_rooms_segment_uses_the_lowest_requested_count(self, rooms, expected_segment):
        """Vérifié en direct pour n>=2 (ile-de-france/appartement/2-pieces-et-plus) ;
        en dessous, la forme exacte (studio ?) n'est pas confirmée, donc omise
        plutôt que devinée."""
        url = BienIciParser().build_search_url({"locations": [PARIS_15], "rooms": rooms})
        if expected_segment:
            assert expected_segment in url
        else:
            assert "pieces-et-plus" not in url

    def test_build_search_urls_wraps_the_single_url(self):
        urls = BienIciParser().build_search_urls({"locations": [PARIS_15]})
        assert len(urls) == 1
        assert urls[0].startswith("https://www.bienici.com/recherche/")

    def test_build_search_urls_is_empty_not_a_list_holding_none(self):
        assert BienIciParser().build_search_urls({"locations": [make_department_location(code="")]}) == []

    def test_several_locations_are_comma_joined_in_a_single_anchor(self):
        """# BUG corrigé : `build_search_url` n'ancrait le lien que sur la
        première localisation reconstructible, les autres étaient invisibles
        alors que le scrape lui-même (zoneIds combinés, voir `_zone_ids`) les
        couvrait bien toutes. Vérifié en direct sur un lien réel de
        recherche bienici à deux communes
        (recherche/location/montrouge-92120,paris-75000/appartement) :
        bienici accepte plusieurs périmètres nommés joints par une virgule
        dans la même ancre, à la manière du `locations=` de SeLoger plutôt
        que du `filter[cities][]` répété de Laforet."""
        url = BienIciParser().build_search_url({"locations": [PARIS_15, LYON_7]})

        assert "/location/paris-75015,lyon-69007/appartement" in url

    def test_a_location_repeated_twice_is_not_duplicated_in_the_anchor(self):
        url = BienIciParser().build_search_url({"locations": [PARIS_15, dict(PARIS_15)]})
        assert "/location/paris-75015/appartement" in url

    def test_an_unresolvable_location_among_several_is_skipped_not_the_whole_url(self):
        url = BienIciParser().build_search_url({
            "locations": [PARIS_15, make_department_location(code=""), LYON_7]
        })

        assert "/location/paris-75015,lyon-69007/appartement" in url


# ---------------------------------------------------------------------------
# has_valid_criteria / cannot_search_reason
# ---------------------------------------------------------------------------

class TestHasValidCriteria:
    def test_a_manual_zone_id_is_always_valid(self):
        assert BienIciParser().has_valid_criteria(manual(["-7444"])) is True

    def test_a_location_with_an_insee_code_is_valid_without_any_network_call(self):
        with patch("services.bienici_geocode._resolve_uncached") as mock_lookup:
            assert BienIciParser().has_valid_criteria({"locations": [PARIS_15]}) is True
        mock_lookup.assert_not_called()

    def test_a_hand_typed_location_without_insee_code_is_still_valid(self):
        """# BUG corrigé : `area_cache_key` résout maintenant une commune par
        son seul code postal (voir services.bienici_geocode), donc une
        localisation tapée à la main — sans code INSEE, mais avec ville +
        code postal — reste utilisable, au lieu d'échouer ici pour une
        information dont la résolution du zoneId n'a en réalité pas besoin."""
        assert BienIciParser().has_valid_criteria({"locations": [TYPED_BY_HAND]}) is True

    @pytest.mark.parametrize("criteria", [{}, {"locations": []}])
    def test_no_location_is_invalid(self, criteria):
        assert BienIciParser().has_valid_criteria(criteria) is False


class TestCannotSearchReason:
    def test_none_when_usable(self):
        assert BienIciParser().cannot_search_reason(make_criteria()) is None

    def test_no_location_at_all(self):
        reason = BienIciParser().cannot_search_reason({})
        assert reason == "aucune localisation exploitable (ville + code postal requis)"

    def test_a_location_without_insee_code_is_usable_not_rejected(self):
        """# BUG corrigé : ce message ("pas de code INSEE") s'affichait pour
        toute localisation tapée à la main ou héritée d'une recherche créée
        avant l'autocomplete unifié, alors que ville + code postal suffisent
        à résoudre le zoneId (voir services.bienici_geocode.area_cache_key)."""
        assert BienIciParser().cannot_search_reason({"locations": [TYPED_BY_HAND]}) is None

    def test_bienici_supports_every_property_type_and_transaction(self):
        """Les quatre types de bien et les deux transactions : rien à refuser."""
        criteria = {
            "locations": [PARIS_15],
            "transaction": "buy",
            "propertyTypes": ["apartment", "house", "parking", "land"],
        }
        assert BienIciParser().cannot_search_reason(criteria) is None
        assert BienIciParser().unsupported_criteria(criteria) == []

    def test_the_capability_message_would_still_be_reachable(self, monkeypatch):
        """bienici ne déclare aujourd'hui aucune limite, mais le second volet
        du contrat hérité de BaseParser doit rester branché — ce test le
        prouve en restreignant temporairement les capacités déclarées."""
        monkeypatch.setattr(BienIciParser, "SUPPORTED_PROPERTY_TYPES", ("apartment",))
        criteria = {"locations": [PARIS_15], "propertyTypes": ["parking"]}

        assert BienIciParser().cannot_search_reason(criteria) == (
            "Bien'ici ne référence pas les biens de type « Parking »"
        )

    def test_the_location_is_checked_before_the_capabilities(self, monkeypatch):
        monkeypatch.setattr(BienIciParser, "SUPPORTED_PROPERTY_TYPES", ("apartment",))
        assert BienIciParser().cannot_search_reason({"propertyTypes": ["parking"]}) == (
            "aucune localisation exploitable (ville + code postal requis)"
        )
