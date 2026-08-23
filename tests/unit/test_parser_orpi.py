"""Tests unitaires de parsers/orpi.py.

Orpi expose une API AJAX publique qui renvoie directement du JSON : le parser
est donc fait de fonctions PURES autour d'un seul appel HTTP — conversion
item -> Listing, filtres rejoués localement, construction des paramètres.

Invariants figés ici, chacun adossé aux captures réelles du 2026-08-23
(tests/fixtures/orpi/, voir SCENARIOS.md pour les requêtes exactes) :

* l'union multi-slugs part en UNE SEULE requête (`locations[][value]`
  répétés, ordre : transactions[], realEstateTypes[], localisations, puis
  filtres prix/surface/pièces) — jamais d'expansion en liste de communes ;
* la clé `items` est TOUJOURS présente sur une vraie réponse, même vide
  (`items: []`, capture search_sans_resultat.json) : le chemin
  « JSON sans items » n'est atteignable que via un mock synthétique ;
* `zipCode` existe sur chaque item mais vaut TOUJOURS null en live : le
  code postal vient exclusivement du slug (_zip_from_slug), et un slug
  illisible échoue FERMÉ (CP vide -> hors périmètre) ;
* `totalCount` est un champ mort (toujours 0) : c'est `count` qui décide
  du warning de troncature au cap serveur de 500 annonces ;
* sold/enabled et la troncature ne sont PAS observables en live (18/18
  items capturés sont sains, count == len(items)) : ces chemins sont
  exercés sur des copies synthétiques bumpées d'items réels ;
* une région se résout par son NOM (identifiant natif `ile-de-france`),
  ou sans nom est élargie à ses départements — la copie portée à la
  résolution porte TOUJOURS les codes de départements (le filtrage aval
  matches_locations lit `departments`), et les critères originaux ne
  sont JAMAIS mutés.

Aucun appel réseau : le socle bloque le transport (tests/conftest.py),
la session requests est doublée ci-dessous (même forme que Laforêt/
Century 21).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qsl

import pytest
import requests
from loguru import logger

from core.geocode import REGION
from parsers.orpi import (
    BASE_URL,
    DESKTOP_UA,
    MAX_RESULTS,
    SEARCH_AJAX_URL,
    OrpiParser,
    _detail_url,
    _dict_to_listing,
    _passes_filters,
    _property_types,
    _rooms_values,
    _search_params,
    _transaction,
    _zip_from_slug,
)
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_listing,
    make_region_location,
    make_whole_city_location,
)

# ---------------------------------------------------------------------------
# Périmètres réutilisés (les deux villes de la capture union)
# ---------------------------------------------------------------------------

ROSNY = make_city_location("Rosny-sous-Bois", "93110", "93066")
MONTREUIL = make_city_location("Montreuil", "93100", "93000")
SEINE_SAINT_DENIS = make_department_location("93", "Seine-Saint-Denis")
IDF = make_region_location(
    "11", "Île-de-France", ("75", "77", "78", "91", "92", "93", "94", "95")
)


# ---------------------------------------------------------------------------
# Captures réelles (tests/fixtures/orpi/ — octets originaux, aucune retouche)
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "orpi"


def load_fixture(name: str) -> dict:
    """Une capture réelle d'orpi.com, telle que requêtée par le code de prod."""
    return json.loads((FIXTURES_DIR / name).read_text())


UNION_2_VILLES = load_fixture("search_union_2_villes_location.json")
ACHAT_FILTRE = load_fixture("search_achat_pieces_filtrees.json")
SANS_RESULTAT = load_fixture("search_sans_resultat.json")

# Cas nominal déterministe désigné par SCENARIOS.md : les 16 champs lus par
# _dict_to_listing y sont présents et non vides.
ITEM_NOMINAL = UNION_2_VILLES["items"][0]
ITEM_ACHAT = ACHAT_FILTRE["items"][0]


def item(**overrides) -> dict:
    """Une copie synthétique de l'item nominal, bumpée pour un chemin précis."""
    return {**ITEM_NOMINAL, **overrides}


# ---------------------------------------------------------------------------
# Doubles de requests.Session (même forme que Laforêt / Century 21)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error")

    def json(self):
        return self._payload


class FakeSession:
    """Double de `requests.Session` : enregistre les appels, rejoue du JSON.

    `payloads` est consommé dans l'ordre et le DERNIER est ensuite répété.
    `handler(url)` prend le dessus pour décider réponse par réponse.
    """

    def __init__(self, payloads=None, handler=None):
        self.headers: dict[str, str] = {}
        self.calls: list[dict] = []
        self._responses = [
            p if isinstance(p, FakeResponse) else FakeResponse(p) for p in (payloads or [])
        ]
        self._handler = handler

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if self._handler is not None:
            return self._handler(url)
        response = self._responses[0]
        if len(self._responses) > 1:
            self._responses.pop(0)
        return response


def run_scrape(criteria: dict, session: FakeSession, parser: OrpiParser | None = None):
    """Exécute `scrape()` en substituant `session` à la vraie requests.Session."""
    parser = parser or OrpiParser()
    with patch("requests.Session", return_value=session):
        return parser.scrape(criteria)


@pytest.fixture
def logged():
    records: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
    )
    yield records
    logger.remove(sink_id)


def orpi_storage():
    """Un Storage doublé dont le cache géo Orpi est câblé.

    `fake_storage()` pré-câble désormais `orpi_geo` comme tous les repos géo
    (mock spécifié sur OrpiGeoRepository, cache neutre). L'override explicite
    est conservé pour afficher l'intention : un cache géo sous contrôle, sans
    résolution automatique parasite, sans dépendre du câblage par défaut du
    helper.
    """
    from tests.helpers.fakes import fake_storage

    return fake_storage(orpi_geo=MagicMock())


def manual_criteria(slug: str = "rosny-sous-bois", **overrides) -> dict:
    """Des critères portant un slug saisi à la main (aucun storage nécessaire).

    La localisation par défaut est Rosny (93110), ville de l'item nominal :
    les tests de scrape retiennent ses annonces sans re-préciser le périmètre."""
    criteria = {
        "locations": [ROSNY],
        "transaction": "rent",
        "propertyTypes": ["apartment"],
        "sourceOverrides": {"orpi": {"slugs": [slug]}},
    }
    criteria.update(overrides)
    return criteria


UNION_PARAMS = [
    ("transactions[]", "rent"),
    ("realEstateTypes[]", "appartement"),
    ("locations[][value]", "rosny-sous-bois"),
    ("locations[][value]", "montreuil"),
]


# ---------------------------------------------------------------------------
# _zip_from_slug — le CP n'existe QUE dans le slug (zipCode null en live)
# ---------------------------------------------------------------------------

class TestZipFromSlug:
    @pytest.mark.parametrize(
        ("slug", "expected"),
        [
            # Item nominal de la capture union : le CP est incrusté entre la
            # ville et la référence UUID.
            (
                "appartement-t3-rosny-sous-bois-93110-73544025-bb3a-40d0-aa33-93d44e5bbc34",
                "93110",
            ),
            # Référence NUMÉRIQUE d'agence (format réel observé) : l'ancre se
            # limite au groupe de 5 chiffres, les segments suivants (492,
            # 018097, 660) ne doivent pas être confondus avec un CP.
            ("maison-prugna-20166-492-018097-660", "20166"),
        ],
    )
    def test_the_postal_code_is_extracted_from_the_slug(self, slug, expected):
        assert _zip_from_slug(slug) == expected

    @pytest.mark.parametrize(
        ("slug", "case"),
        [
            ("appartement-t2-ville-12345678-ref", "un bloc de 8 chiffres n'est pas un CP"),
            ("terrain-a-vendre-sans-commune", "aucun groupe de 5 chiffres"),
            ("", "slug vide"),
            (None, "slug absent"),
        ],
        ids=["huit_chiffres", "sans_groupe", "vide", "none"],
    )
    def test_an_unreadable_slug_fails_closed(self, slug, case):
        """Pas de CP deviné : '' fait échouer matches_locations fermé — une
        annonce illisible n'est jamais supposée être dans le périmètre."""
        assert _zip_from_slug(slug) == "", case

    def test_every_real_item_carries_a_usable_postal_code(self):
        """🔒 En live, `zipCode` vaut null sur TOUS les items (18/18 capturés) :
        si l'extraction slug rendait autre chose que les CP attendus, TOUTES
        les annonces seraient écartées du périmètre par matches_locations."""
        extracted = {_zip_from_slug(i["slug"]) for i in UNION_2_VILLES["items"]}
        assert extracted == {"93110", "93100"}


# ---------------------------------------------------------------------------
# _detail_url / _transaction — P2 : formats vérifiés en direct (HTTP 200)
# ---------------------------------------------------------------------------

class TestDetailUrl:
    def test_a_rent_listing_uses_the_location_prefix(self):
        assert _detail_url(ITEM_NOMINAL) == (
            f"{BASE_URL}/annonce-location-{ITEM_NOMINAL['slug']}/"
        )

    def test_a_buy_listing_uses_the_sale_prefix(self):
        assert _detail_url(ITEM_ACHAT) == f"{BASE_URL}/annonce-vente-{ITEM_ACHAT['slug']}/"

    def test_an_unknown_transaction_falls_back_to_the_sale_prefix(self):
        assert _detail_url({"transaction": "swap", "slug": "x"}) == f"{BASE_URL}/annonce-vente-x/"

    def test_a_missing_slug_still_builds_a_path(self):
        assert _detail_url({"transaction": "rent"}) == f"{BASE_URL}/annonce-location-/"


class TestTransaction:
    @pytest.mark.parametrize(
        ("criteria_transaction", "expected"),
        [
            ("rent", "rent"),
            ("buy", "buy"),
            (None, "rent"),  # La location par défaut : une recherche muette
            ("swap", "rent"),  # n'a jamais voulu dire « achat ».
        ],
        ids=["location", "achat", "absente", "invalide"],
    )
    def test_the_requested_transaction(self, criteria_transaction, expected):
        assert _transaction({"transaction": criteria_transaction}) == expected


# ---------------------------------------------------------------------------
# _property_types / _rooms_values — le canonique vers les valeurs natives
# ---------------------------------------------------------------------------

class TestPropertyTypes:
    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            (None, ["apartment"]),  # Aucun type demandé -> défaut du formulaire.
            ([], ["apartment"]),
            (["apartment"], ["apartment"]),
            (["house"], ["house"]),
            (["parking"], ["parking"]),
            (["land"], ["land"]),
            # L'ordre demandé est conservé (il décide de l'ordre des paramètres).
            (["apartment", "house"], ["apartment", "house"]),
            # Les types hors capacités sont retirés SANS exception.
            (["apartment", "yacht"], ["apartment"]),
            (["yacht"], []),  # Et surtout PAS le défaut appartement :
        ],  # des appartements à qui demande un bateau serait un faux résultat.
        ids=["aucun", "liste_vide", "appartement", "maison", "parking", "terrain",
             "deux_types", "mixte", "hors_capacites"],
    )
    def test_supported_types_only(self, requested, expected):
        assert _property_types({"propertyTypes": requested}) == expected


class TestRoomsValues:
    @pytest.mark.parametrize(
        ("rooms", "expected"),
        [
            ([2, 3], [2, 3]),  # Égalité multiple, triée.
            ([3, 2], [2, 3]),
            (["2"], [2]),  # Le canonique peut arriver en chaînes.
            ([0, -1, "abc", None], []),  # Valeurs illisibles ignorées.
            ([5], [5, 6, 7, 8]),  # « 5 » canonique = « 5 et plus » -> {5..8}.
            ([2, 5], [2, 5, 6, 7, 8]),
            ([], []),
        ],
        ids=["egalites", "triees", "chaines", "illisibles", "cinq_plus",
             "mixte", "vide"],
    )
    def test_values_sent_to_the_site(self, rooms, expected):
        assert _rooms_values({"rooms": rooms}) == expected


# ---------------------------------------------------------------------------
# _search_params — miroir EXACT des requêtes capturées (voir SCENARIOS.md)
# ---------------------------------------------------------------------------

class TestSearchParams:
    def test_the_union_of_two_cities_in_one_request(self):
        """Requête EXACTE de search_union_2_villes_location.json : transactions[]
        puis realEstateTypes[], puis un locations[][value] par slug — l'union
        native du site, jamais une requête par ville."""
        criteria = {"locations": [ROSNY, MONTREUIL], "transaction": "rent",
                    "propertyTypes": ["apartment"]}

        assert _search_params(criteria, ["rosny-sous-bois", "montreuil"]) == UNION_PARAMS

    def test_buy_with_repeated_room_equality(self):
        """Requête EXACTE de search_achat_pieces_filtrees.json : transactions[]=buy
        puis numbersOfRooms[] répétés (égalité multiple vérifiée en live)."""
        criteria = {"transaction": "buy", "propertyTypes": ["apartment"], "rooms": [2, 3]}

        params = _search_params(criteria, ["rosny-sous-bois"])

        assert params[0] == ("transactions[]", "buy")
        assert params[-2:] == [("numbersOfRooms[]", "2"), ("numbersOfRooms[]", "3")]

    def test_price_and_surface_bounds_use_the_native_names(self):
        criteria = {"priceMin": 500, "priceMax": 1500, "surfaceMin": 20, "surfaceMax": 80}

        params = dict(_search_params(criteria, ["rosny-sous-bois"]))

        assert params["minPrice"] == "500"
        assert params["maxPrice"] == "1500"
        assert params["minSurface"] == "20"
        assert params["maxSurface"] == "80"

    def test_five_plus_expands_into_multiple_equalities(self):
        params = _search_params({"rooms": [5]}, ["rosny-sous-bois"])

        assert params[-4:] == [
            ("numbersOfRooms[]", "5"),
            ("numbersOfRooms[]", "6"),
            ("numbersOfRooms[]", "7"),
            ("numbersOfRooms[]", "8"),
        ]


# ---------------------------------------------------------------------------
# _passes_filters — le recadrage local de ce que le site a renvoyé
# ---------------------------------------------------------------------------

class TestPassesFilters:
    def test_a_rejected_location_short_circuits_every_other_filter(self):
        listing = make_listing(zip_code="75011", price_value=1000.0, surface="50", rooms="2")

        assert _passes_filters(listing, {"priceMin": 0}, [ROSNY]) is False

    @pytest.mark.parametrize(
        ("price_value", "criteria", "expected"),
        [
            (1190.0, {"priceMin": 1000, "priceMax": 1500}, True),
            (1190.0, {"priceMax": 1100}, False),
            (1190.0, {"priceMin": 1200}, False),
            # Bornes inclusives.
            (1190.0, {"priceMin": 1190, "priceMax": 1190}, True),
            # FAIL-OPEN : un prix illisible ne fait pas écarter l'annonce.
            (None, {"priceMax": 1}, True),
        ],
        ids=["dans_les_bornes", "trop_cher", "trop_peu_cher", "bornes_inclusives", "prix_illisible"],
    )
    def test_price_bounds(self, price_value, criteria, expected):
        listing = make_listing(zip_code="93110", price_value=price_value)
        assert _passes_filters(listing, criteria, [ROSNY]) is expected

    @pytest.mark.parametrize(
        ("surface", "criteria", "expected"),
        [
            ("60.0", {"surfaceMin": 40, "surfaceMax": 80}, True),
            ("60.0", {"surfaceMax": 50}, False),
            # Une virgule décimale résiduelle est convertie avant comparaison.
            ("60,5", {"surfaceMin": 61}, False),
            ("60,5", {"surfaceMin": 60}, True),
            # Pas de surface : fail-open.
            ("", {"surfaceMin": 100}, True),
        ],
        ids=["dans_les_bornes", "trop_grande", "virgule_exclue", "virgule_incluse", "absente"],
    )
    def test_surface_bounds(self, surface, criteria, expected):
        listing = make_listing(zip_code="93110", surface=surface)
        assert _passes_filters(listing, criteria, [ROSNY]) is expected

    @pytest.mark.parametrize(
        ("rooms", "criteria", "expected"),
        [
            ("3", {"rooms": [2, 3]}, True),
            ("4", {"rooms": [2, 3]}, False),
            # « 5 » signifie « 5 et plus » : tout >= 5 passe.
            ("6", {"rooms": [5]}, True),
            ("4", {"rooms": [5]}, False),
            # Pièces illisibles : fail-open.
            ("", {"rooms": [2]}, True),
            ("n/a", {"rooms": [2]}, True),
        ],
        ids=["egalite_ok", "egalite_ko", "cinq_plus_ok", "cinq_plus_ko",
             "vide", "illisible"],
    )
    def test_room_counts(self, rooms, criteria, expected):
        listing = make_listing(zip_code="93110", rooms=rooms)
        assert _passes_filters(listing, criteria, [ROSNY]) is expected

    def test_a_region_scope_is_matched_through_its_departments(self):
        """La région arrive à _passes_filters sous sa forme RÉSOLUE (copie
        portant `departments`) : le cadrage se fait par préfixes postaux des
        départements. Sans cette liste, matches_locations échouerait fermé et
        TOUTES les annonces d'une recherche régionale seraient perdues."""
        scoped = {"kind": REGION, "code": "11", "departments": ["93"]}
        in_scope = make_listing(zip_code="93110")  # Seine-Saint-Denis.
        out_of_scope = make_listing(zip_code="77000")  # Seine-et-Marne (77).

        assert _passes_filters(in_scope, {}, [scoped]) is True
        assert _passes_filters(out_of_scope, {}, [scoped]) is False


# ---------------------------------------------------------------------------
# _dict_to_listing — l'item JSON vers le schéma commun Listing
# ---------------------------------------------------------------------------

class TestDictToListing:
    def test_maps_the_nominal_item_of_the_union_capture(self):
        """Item d'index 0 de search_union_2_villes_location.json : les 16 champs
        lus par le parser y sont présents et non vides (vérifié en live)."""
        listing = _dict_to_listing(ITEM_NOMINAL)

        assert listing.listing_id == "orpi_73544025-bb3a-40d0-aa33-93d44e5bbc34"
        assert listing.legacy_id == "73544025-bb3a-40d0-aa33-93d44e5bbc34"
        assert listing.source == "orpi"
        assert listing.url == (
            f"{BASE_URL}/annonce-location-appartement-t3-rosny-sous-bois-93110"
            "-73544025-bb3a-40d0-aa33-93d44e5bbc34/"
        )
        assert listing.title == "Appartement · 3 pièces · Rosny-sous-Bois"
        assert listing.price == "1190 €"
        assert listing.price_value == 1190.0
        assert listing.surface == "60.0"
        assert listing.rooms == "3"
        assert listing.location == "Rosny-sous-Bois"
        assert listing.city == "Rosny-sous-Bois"
        assert listing.district == "Rosny Sud - Gare RER A"
        # zipCode vaut null dans la capture : le CP vient du slug.
        assert ITEM_NOMINAL["zipCode"] is None
        assert listing.zip_code == "93110"
        assert listing.property_type == "Appartement"
        assert listing.agency == "Agence De La Mairie"
        assert listing.is_exclusive is True
        assert listing.creation_date == "2026-08-06T00:00:00+02:00"

    def test_photos_are_serialised_from_full_url_first(self):
        listing = _dict_to_listing(ITEM_NOMINAL)

        photos = json.loads(listing.photos)
        assert len(photos) == len(ITEM_NOMINAL["estatePhotos"])
        assert photos[0]["url"] == ITEM_NOMINAL["estatePhotos"][0]["fullUrl"]
        assert listing.image_url == ITEM_NOMINAL["estatePhotos"][0]["fullUrl"]

    def test_a_photo_without_full_url_falls_back_to_url(self):
        raw = item(estatePhotos=[{"url": "https://cdn.orpi.com/p.jpg"}])

        listing = _dict_to_listing(raw)

        assert listing.image_url == "https://cdn.orpi.com/p.jpg"

    def test_non_dict_photo_entries_are_skipped(self):
        raw = item(estatePhotos=["corrompu", {"fullUrl": "https://cdn.orpi.com/ok.jpg"}])

        listing = _dict_to_listing(raw)

        assert listing.image_url == "https://cdn.orpi.com/ok.jpg"
        assert len(json.loads(listing.photos)) == 1

    def test_no_photo_yields_empty_image_and_empty_serialisation(self):
        listing = _dict_to_listing(item(estatePhotos=[]))

        assert listing.image_url == ""
        assert listing.photos == "[]"

    def test_an_unknown_type_has_no_label_and_the_title_survives(self):
        """Le site peut glisser des biens hors référentiel : pas de type deviné,
        mais le titre garde pièces et localité."""
        listing = _dict_to_listing(item(type="chalet"))

        assert listing.property_type == ""
        assert listing.title == "3 pièces · Rosny-sous-Bois"

    @pytest.mark.parametrize(
        "field", ["nbRooms", "price", "surface"],
        ids=["pieces", "prix", "surface"],
    )
    def test_a_missing_number_yields_display_defaults(self, field):
        raw = item(**{field: None})

        listing = _dict_to_listing(raw)

        if field == "price":
            assert listing.price == ""
            assert listing.price_value is None
        elif field == "surface":
            assert listing.surface == ""
        else:
            assert listing.rooms == ""

    def test_the_reference_falls_back_to_the_numeric_id(self):
        raw = item(reference=None, id=1484996)

        listing = _dict_to_listing(raw)

        assert listing.listing_id == "orpi_1484996"
        assert listing.legacy_id == "1484996"

    def test_missing_nested_objects_yield_empty_strings(self):
        raw = item(city=None, district=None, agency=None)

        listing = _dict_to_listing(raw)

        assert listing.city == ""
        assert listing.district == ""
        assert listing.agency == ""

    def test_the_description_is_truncated_to_300_characters(self):
        listing = _dict_to_listing(ITEM_NOMINAL)

        assert listing.description == ITEM_NOMINAL["longAd"][:300]

    def test_a_single_room_has_no_plural_in_the_title(self):
        listing = _dict_to_listing(item(nbRooms=1))

        assert "1 pièce ·" in listing.title


# ---------------------------------------------------------------------------
# _slugs — résolution des identifiants de lieu
# ---------------------------------------------------------------------------

class TestSlugs:
    def test_a_manual_slug_short_circuits_every_resolution(self, logged):
        parser = OrpiParser(storage=orpi_storage())

        with patch("services.orpi_geocode.resolve_slug_id") as mock_resolve:
            slugs = parser._slugs(manual_criteria(), [ROSNY])

        assert slugs == [(ROSNY, "rosny-sous-bois")]
        mock_resolve.assert_not_called()

    @pytest.mark.parametrize(
        ("slugs_count", "locations_count"),
        [(1, 3), (3, 1)],
        ids=["moins_de_slugs_que_de_villes", "plus_de_slugs_que_de_villes"],
    )
    def test_manual_slugs_zip_loose_not_strict(self, slugs_count, locations_count):
        """L'utilisateur peut coller plus ou moins de slugs que de villes :
        `zip(strict=False)` épouse ce qu'il y a, dans l'ordre."""
        locations = [ROSNY, MONTREUIL, SEINE_SAINT_DENIS][:locations_count]
        slugs_input = ["rosny-sous-bois", "montreuil", "seine-saint-denis"][:slugs_count]
        criteria = {"sourceOverrides": {"orpi": {"slugs": slugs_input}}}

        pairs = OrpiParser()._slugs(criteria, locations)

        assert [(loc, slug) for loc, slug in pairs] == list(
            zip(locations, slugs_input, strict=False)
        )

    def test_without_storage_the_impossibility_is_logged(self, logged):
        parser = OrpiParser()

        assert parser._slugs({"locations": [ROSNY]}, [ROSNY]) == []
        assert any(
            level == "WARNING" and "Aucun storage fourni au parser" in message
            for level, message in logged
        )

    def test_auto_resolution_passes_the_injected_repo(self):
        """Le repo de cache vient du storage INJECTÉ (thread de fond, jamais
        flask.current_app) : le mock doit recevoir exactement cet objet."""
        storage = orpi_storage()
        parser = OrpiParser(storage=storage)
        seen_repos = []

        def spy(location, repo):
            seen_repos.append(repo)
            return "rosny-sous-bois"

        with patch("services.orpi_geocode.resolve_slug_id", side_effect=spy):
            slugs = parser._slugs({"locations": [ROSNY]}, [ROSNY])

        assert slugs == [(ROSNY, "rosny-sous-bois")]
        assert seen_repos == [storage.orpi_geo]

    def test_an_unresolved_location_is_omitted_with_a_warning(self, logged):
        parser = OrpiParser(storage=orpi_storage())

        with patch("services.orpi_geocode.resolve_slug_id", return_value=None):
            slugs = parser._slugs({"locations": [ROSNY]}, [ROSNY])

        assert slugs == []
        assert any(
            level == "WARNING" and "Aucun slug résolu" in message
            for level, message in logged
        )

    def test_a_named_region_resolves_through_its_native_slug(self, logged):
        """« Île-de-France » a un identifiant natif Orpi (`ile-de-france`,
        capture autocomplete_region_par_nom.json) : UNE valeur locations[][value],
        pas une par département."""
        parser = OrpiParser(storage=orpi_storage())
        region = {"kind": REGION, "code": "11", "name": "Île-de-France",
                  "departments": ["75", "93"]}

        with patch(
            "services.orpi_geocode.resolve_slug_id", return_value="ile-de-france"
        ) as mock_resolve:
            slugs = parser._slugs({"locations": [region]}, [region])

        assert len(slugs) == 1
        scoped, slug = slugs[0]
        assert slug == "ile-de-france"
        # La copie porte les départements : le cadrage aval en dépend.
        assert scoped["departments"] == ["75", "93"]
        assert mock_resolve.call_count == 1
        assert not any(level == "WARNING" for level, _ in logged)

    def test_the_original_criteria_are_never_mutated_by_a_region_resolution(self):
        """🔒 Les critères sont partagés entre sources pendant un scrape : la
        copie régionale porte les départements, JAMAIS l'original."""
        parser = OrpiParser(storage=orpi_storage())
        region = {"kind": REGION, "code": "11", "name": "Île-de-France"}
        criteria = {"locations": [region]}
        snapshot = json.dumps(criteria, sort_keys=True)

        with (
            patch("parsers.orpi.region_departments", return_value=["75", "93"]),
            patch("services.orpi_geocode.resolve_slug_id", return_value="ile-de-france"),
        ):
            slugs = parser._slugs(criteria, [region])

        assert slugs[0][0]["departments"] == ["75", "93"]  # La copie.
        assert json.dumps(criteria, sort_keys=True) == snapshot  # L'original.

    def test_a_nameless_region_expands_into_one_target_per_department(self, logged):
        """Sans nom, pas de requête région possible (« 11 » renvoie l'Aude) :
        chaque département est résolu individuellement puis tous partent quand
        même dans UNE requête (union native)."""
        parser = OrpiParser(storage=orpi_storage())
        region = {"kind": REGION, "departments": ["93", "94"]}

        with patch(
            "services.orpi_geocode.resolve_slug_id",
            side_effect=lambda loc, repo: f"dept-{loc['code']}",
        ) as mock_resolve:
            slugs = parser._slugs({"locations": [region]}, [region])

        assert [(slug,) for _, slug in slugs] == [("dept-93",), ("dept-94",)]
        assert all(pair[0]["departments"] == ["93", "94"] for pair in slugs)
        assert {call.args[0]["code"] for call in mock_resolve.call_args_list} == {"93", "94"}
        assert not any(level == "WARNING" for level, _ in logged)

    def test_the_expansion_falls_back_to_the_geo_api_without_a_departments_list(self):
        """Région SANS nom ni liste embarquée : les codes sont demandés à
        l'API geo, puis chaque département est résolu individuellement.
        NB : contrairement à Century 21 (import local), orpi.py importe
        region_departments au niveau MODULE — le patch passe donc par
        parsers.orpi, pas core.geocode."""
        parser = OrpiParser(storage=orpi_storage())
        region = {"kind": REGION}

        with (
            patch("parsers.orpi.region_departments", return_value=["2A", "2B"]) as mock_api,
            patch(
                "services.orpi_geocode.resolve_slug_id",
                side_effect=lambda loc, repo: f"dept-{loc['code']}",
            ),
        ):
            slugs = parser._slugs({"locations": [region]}, [region])

        mock_api.assert_called_once_with("")
        assert [(loc["kind"], slug) for loc, slug in slugs] == [
            ("region", "dept-2A"),
            ("region", "dept-2B"),
        ]
        # Chaque entrée porte la COPIE élargie de la région (jamais l'original).
        assert all(loc is not region for loc, _ in slugs)

    def test_a_region_without_identifiable_departments_is_skipped_with_a_warning(self, logged):
        parser = OrpiParser(storage=orpi_storage())
        region_nue = {"kind": REGION}

        with (
            patch("parsers.orpi.region_departments", return_value=[]),
            patch("services.orpi_geocode.resolve_slug_id") as mock_resolve,
        ):
            slugs = parser._slugs({"locations": [region_nue]}, [region_nue])

        assert slugs == []
        mock_resolve.assert_not_called()
        assert any(
            level == "WARNING" and "sans départements identifiables" in message
            for level, message in logged
        )

    def test_the_repo_comes_from_the_injected_storage(self):
        storage = orpi_storage()
        assert OrpiParser(storage=storage)._geo_repo() is storage.orpi_geo
        assert OrpiParser()._geo_repo() is None


# ---------------------------------------------------------------------------
# _scraped_locations — l'échec bruyant quand AUCUN périmètre n'est exploitable
# ---------------------------------------------------------------------------

class TestScrapedLocations:
    def test_no_resolved_slug_raises_instead_of_a_silent_national_search(self, logged):
        """Sans identifiant de lieu, le scrape doit REFUSER plutôt que lancer
        une recherche nationale silencieuse (des milliers d'annonces hors
        critères)."""
        parser = OrpiParser()  # Pas de storage : rien n'est résoluble.

        with pytest.raises(ValueError, match="Aucune localisation Orpi exploitable"):
            parser._scraped_locations({"locations": [ROSNY]}, [ROSNY])


# ---------------------------------------------------------------------------
# build_search_urls / build_search_url — « Voir l'URL » montre la vérité
# ---------------------------------------------------------------------------

class TestBuildSearchUrls:
    def test_the_union_url_mirrors_the_captured_query_byte_for_byte(self):
        """Reconstruction EXACTE de la requête capturée (SCENARIOS.md §1) :
        la page /recherche est server-rendered avec les mêmes paramètres."""
        criteria = manual_criteria()
        criteria["locations"] = [ROSNY, MONTREUIL]
        criteria["sourceOverrides"] = {
            "orpi": {"slugs": ["rosny-sous-bois", "montreuil"]}
        }

        urls = OrpiParser().build_search_urls(criteria)

        assert len(urls) == 1
        assert urls[0] == (
            f"{BASE_URL}/recherche?transactions%5B%5D=rent&realEstateTypes%5B%5D=appartement"
            "&locations%5B%5D%5Bvalue%5D=rosny-sous-bois&locations%5B%5D%5Bvalue%5D=montreuil"
        )

    def test_filters_are_part_of_the_single_url(self):
        criteria = manual_criteria(priceMin=800, surfaceMax=98, rooms=[2, 3])

        query = OrpiParser().build_search_urls(criteria)[0].split("?", 1)[1]
        pairs = parse_qsl(query)

        assert ("minPrice", "800") in pairs
        assert ("maxSurface", "98") in pairs
        assert pairs.count(("numbersOfRooms[]", "2")) == 1
        assert pairs.count(("numbersOfRooms[]", "3")) == 1

    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}],
        ids=["vide", "liste_vide"],
    )
    def test_no_url_without_a_location(self, criteria):
        assert OrpiParser().build_search_urls(criteria) == []
        assert OrpiParser().build_search_url(criteria) is None

    def test_no_url_when_no_type_is_supported(self):
        criteria = manual_criteria(propertyTypes=["yacht"])

        assert OrpiParser().build_search_urls(criteria) == []

    def test_no_url_when_nothing_resolves(self):
        criteria = {"locations": [ROSNY]}  # Pas de slug manuel ni de storage.

        assert OrpiParser().build_search_urls(criteria) == []


# ---------------------------------------------------------------------------
# has_valid_criteria — utilisable dès qu'un périmètre est identifiable
# ---------------------------------------------------------------------------

class TestHasValidCriteria:
    def test_a_manual_slug_is_always_valid(self):
        criteria = {"sourceOverrides": {"orpi": {"slugs": ["rosny-sous-bois"]}}}
        assert OrpiParser().has_valid_criteria(criteria) is True

    @pytest.mark.parametrize(
        "criteria",
        [
            {"locations": [ROSNY]},
            {"locations": [make_whole_city_location("Rosny-sous-Bois", ("93110",), "93066")]},
            {"locations": [SEINE_SAINT_DENIS]},
            # Région avec nom : identifiant natif.
            {"locations": [IDF]},
            # Région SANS nom mais avec sa liste : résoluble par élargissement.
            # (Un code reste requis : sans lui, normalize_locations écarte la
            # localisation avant même le parser.)
            {"locations": [{"kind": REGION, "code": "11", "departments": ["93"]}]},
            # Région avec seulement son code : la liste est demandable à l'API geo.
            {"locations": [{"kind": REGION, "code": "11"}]},
            # Commune tapée à la main : clé de cache dérivée du code postal.
            {"locations": [{"kind": "city", "city": "Rosny", "postalCode": "93110"}]},
        ],
        ids=["commune", "ville_entiere", "departement", "region_nommee",
             "region_departements_seuls", "region_code_seul", "commune_sans_insee"],
    )
    def test_any_identifiable_perimeter_is_valid(self, criteria):
        assert OrpiParser().has_valid_criteria(criteria) is True

    @pytest.mark.parametrize(
        "criteria",
        [
            {"locations": [{"kind": REGION}]},
            {},
            {"locations": []},
        ],
        ids=["region_nue", "vide", "liste_vide"],
    )
    def test_everything_else_is_invalid(self, criteria):
        assert OrpiParser().has_valid_criteria(criteria) is False

    def test_validation_never_resolves_anything(self):
        with patch("services.orpi_geocode._resolve_uncached") as mock_resolve:
            assert OrpiParser().has_valid_criteria({"locations": [ROSNY]}) is True
        mock_resolve.assert_not_called()


# ---------------------------------------------------------------------------
# parse_manual_override / remember_manual_override
# ---------------------------------------------------------------------------

class TestParseManualOverride:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", {}),
            ("   ", {}),
            (None, {}),
            ("rosny-sous-bois", {"slugs": ["rosny-sous-bois"]}),
            ("  rosny-sous-bois  ", {"slugs": ["rosny-sous-bois"]}),
            ("rosny-sous-bois, gironde", {"slugs": ["rosny-sous-bois", "gironde"]}),
            ("rosny-sous-bois ,gironde", {"slugs": ["rosny-sous-bois", "gironde"]}),
            ("rosny,,   ,gironde", {"slugs": ["rosny", "gironde"]}),
            ("rosny,", {"slugs": ["rosny"]}),
            (",", {}),
            (" , , ", {}),
        ],
    )
    def test_comma_separated_slugs(self, value, expected):
        assert OrpiParser().parse_manual_override(value) == expected


class TestRememberManualOverride:
    def test_banks_the_slugs_against_the_injected_repo(self):
        storage = orpi_storage()
        parser = OrpiParser(storage=storage)
        criteria = manual_criteria()

        with patch("services.orpi_geocode.remember_manual_slugs") as mock_remember:
            assert parser.remember_manual_override(criteria) is None

        mock_remember.assert_called_once_with(criteria, repo=storage.orpi_geo)

    @pytest.mark.parametrize("storage", [None, object()], ids=["sans_storage", "storage_sans_repo_geo"])
    def test_without_a_repo_nothing_is_attempted(self, storage):
        parser = OrpiParser(storage=storage)
        with patch("services.orpi_geocode.remember_manual_slugs") as mock_remember:
            assert parser.remember_manual_override(manual_criteria()) is None
        mock_remember.assert_not_called()

    def test_a_failure_is_logged_but_never_raised(self, logged):
        parser = OrpiParser(storage=orpi_storage())

        with patch(
            "services.orpi_geocode.remember_manual_slugs",
            side_effect=RuntimeError("banque indisponible"),
        ):
            assert parser.remember_manual_override(manual_criteria()) is None

        assert any(
            level == "DEBUG" and "non mémorisé" in message and "banque indisponible" in message
            for level, message in logged
        )


# ---------------------------------------------------------------------------
# to_native — rien à traduire
# ---------------------------------------------------------------------------

class TestToNative:
    def test_the_canonical_criteria_are_used_as_is(self):
        criteria = manual_criteria()
        assert OrpiParser().to_native(criteria) is criteria


# ---------------------------------------------------------------------------
# scrape : garde-fous d'entrée, session et transport
# ---------------------------------------------------------------------------

class TestScrapeGuards:
    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}, {"locations": [{"kind": "city", "city": "Rosny"}]}],
        ids=["vide", "liste_vide", "localisation_incomplete"],
    )
    def test_no_location_raises_before_any_request(self, criteria):
        with pytest.raises(ValueError, match="nécessite au moins une localisation"):
            OrpiParser().scrape(criteria)

    def test_unsupported_property_types_raise_before_any_request(self):
        with pytest.raises(ValueError, match="ne référence aucun des types"):
            OrpiParser().scrape(manual_criteria(propertyTypes=["yacht"]))

    def test_no_resolvable_location_raises(self):
        """Pas de storage -> aucun slug -> refus bruyant, jamais une recherche
        nationale déguisée."""
        with pytest.raises(ValueError, match="Aucune localisation Orpi exploitable"):
            OrpiParser().scrape({"locations": [ROSNY], "propertyTypes": ["apartment"]})

    def test_the_session_announces_a_desktop_browser_asking_json(self):
        session = FakeSession([UNION_2_VILLES])
        run_scrape(manual_criteria(), session)

        assert session.headers["User-Agent"] == DESKTOP_UA
        assert session.headers["Accept"] == "application/json"

    def test_the_ajax_endpoint_gets_one_request_per_scrape_with_a_timeout(self):
        """Deux villes = UNE requête (union native) : le cap serveur de 500
        annonces s'applique au périmètre COMBINÉ, d'où l'affinage conseillé."""
        criteria = manual_criteria()
        criteria["locations"] = [ROSNY, MONTREUIL]
        criteria["sourceOverrides"] = {
            "orpi": {"slugs": ["rosny-sous-bois", "montreuil"]}
        }
        session = FakeSession([UNION_2_VILLES])
        run_scrape(criteria, session)

        assert len(session.calls) == 1
        assert session.calls[0]["url"] == SEARCH_AJAX_URL
        assert session.calls[0]["timeout"] == 20
        assert session.calls[0]["params"] == UNION_PARAMS


# ---------------------------------------------------------------------------
# scrape : cas nominal et filtrage sur les captures réelles
# ---------------------------------------------------------------------------

class TestScrapeNominal:
    def test_the_union_capture_returns_both_cities_in_order(self):
        criteria = manual_criteria()
        criteria["locations"] = [ROSNY, MONTREUIL]
        criteria["sourceOverrides"] = {
            "orpi": {"slugs": ["rosny-sous-bois", "montreuil"]}
        }

        listings = run_scrape(criteria, FakeSession([UNION_2_VILLES]))

        assert [li.listing_id for li in listings] == [
            f"orpi_{i['reference']}" for i in UNION_2_VILLES["items"]
        ]
        assert {li.city for li in listings} == {"Rosny-sous-Bois", "Montreuil"}
        assert all(li.source == "orpi" for li in listings)

    def test_the_purchase_capture_keeps_only_in_scope_items(self):
        """Les 11 items buy (nbRooms ∈ {2,3} vérifié en live) passent le
        recadrage local : CP extraits des slugs = 93110 = Rosny."""
        criteria = manual_criteria("rosny-sous-bois", transaction="buy")

        listings = run_scrape(criteria, FakeSession([ACHAT_FILTRE]))

        assert len(listings) == len(ACHAT_FILTRE["items"])
        assert all(li.zip_code == "93110" for li in listings)
        assert all("/annonce-vente-" in li.url for li in listings)

    def test_duplicates_by_reference_are_kept_once(self):
        duplicated = {"items": [ITEM_NOMINAL, dict(ITEM_NOMINAL)], "count": 1}

        listings = run_scrape(manual_criteria(), FakeSession([duplicated]))

        assert [li.listing_id for li in listings] == ["orpi_" + ITEM_NOMINAL["reference"]]

    @pytest.mark.parametrize(
        ("override", "reason"),
        [
            ({"sold": True}, "bien vendu — jamais observé en live, copie synthétique"),
            ({"enabled": False}, "annonce désactivée — idem"),
        ],
        ids=["vendu", "desactive"],
    )
    def test_sold_and_disabled_items_are_skipped(self, override, reason):
        # Référence DISTINCTE pour l'item écarté : le dédoublonnage passe avant
        # les contrôles de santé, deux copies de même référence ne prouveraient
        # que le dedup, pas l'écartement.
        excluded = item(reference="exclue-0001", **override)
        payload = {"items": [excluded, ITEM_NOMINAL], "count": 2}

        listings = run_scrape(manual_criteria(), FakeSession([payload]))

        assert len(listings) == 1, reason
        assert listings[0].legacy_id == ITEM_NOMINAL["reference"]

    def test_items_outside_the_type_vocabulary_are_skipped(self, logged):
        """Le site peut glisser des programmes hors référentiel dans les
        réponses larges : écartés, comptabilisés en debug."""
        excluded = item(type="chalet", reference="chalet-0001")
        payload = {"items": [excluded, ITEM_NOMINAL], "count": 2}

        listings = run_scrape(manual_criteria(), FakeSession([payload]))

        assert len(listings) == 1
        assert any(
            level == "DEBUG" and "hors types demandés" in message
            for level, message in logged
        )

    def test_out_of_area_items_are_dropped_by_the_local_filter(self):
        """Le filet aval : un item dont le CP (extrait du slug) sort du
        périmètre ne ressort pas, même si le site l'a envoyé."""
        intruder = item(
            reference="paris-0001",
            slug=item()["slug"].replace("-93110-", "-75011-"),
            locationDescription="Paris 11e",
            city={"name": "Paris"},
        )
        payload = {"items": [intruder, ITEM_NOMINAL], "count": 2}

        listings = run_scrape(manual_criteria(), FakeSession([payload]))

        assert [li.legacy_id for li in listings] == [ITEM_NOMINAL["reference"]]

    def test_filters_are_replayed_against_the_merged_region_scope(self, logged):
        """Recherche RÉGIONALE : le recadrage utilise la copie élargie (avec
        `departments`). Si le parser y passait la région brute sans cette liste,
        matches_locations échouerait fermé et toutes les annonces seraient
        perdues. NB : la région doit porter un CODE, sinon normalize_locations
        l'écarte avant même d'atteindre le parser."""
        region = {"kind": REGION, "code": "11", "departments": ["93"]}
        criteria = {
            "locations": [region],
            "transaction": "rent",
            "propertyTypes": ["apartment"],
        }
        parser = OrpiParser(storage=orpi_storage())
        with patch(
            "services.orpi_geocode.resolve_slug_id", return_value="seine-saint-denis"
        ):
            listings = run_scrape(criteria, FakeSession([UNION_2_VILLES]), parser)

        # Tout l'union capturée est en Seine-Saint-Denis (93).
        assert len(listings) == len(UNION_2_VILLES["items"])
        assert any(level == "INFO" and "annonces uniques" in m for level, m in logged)


class TestScrapeEmptyAndFailures:
    def test_the_empty_capture_returns_an_empty_list_not_an_error(self):
        """Capture search_sans_resultat.json : `items: []` + count 0 — un
        résultat VIDE, distinct d'un échec (le pipeline s'en sert pour
        différencier « rien trouvé » de « source en erreur »)."""
        criteria = manual_criteria(priceMin=99999999)

        listings = run_scrape(criteria, FakeSession([SANS_RESULTAT]))

        assert listings == []

    def test_a_payload_without_items_raises(self):
        """Chemin INATEIGNABLE en live (la clé items est toujours présente,
        même à zéro résultat) : page d'erreur HTML ou JSON inattendu simulés
        par un mock synthétique."""
        for payload in [{"count": 0}, []]:
            with pytest.raises(ValueError, match="JSON sans items"):
                run_scrape(manual_criteria(), FakeSession([payload]))

    def test_a_network_error_is_wrapped_in_a_value_error(self):
        def handler(url):
            raise requests.ConnectionError("réseau coupé")

        session = FakeSession(handler=handler)

        with pytest.raises(ValueError, match="Requête Orpi échouée .*rosny-sous-bois"):
            run_scrape(manual_criteria(), session)

    def test_an_http_error_is_wrapped_in_a_value_error(self):
        session = FakeSession([FakeResponse({}, status_code=503)])

        with pytest.raises(ValueError, match="Requête Orpi échouée"):
            run_scrape(manual_criteria(), session)


class TestScrapeTruncation:
    def test_a_count_above_len_items_warns_about_the_server_cap(self, logged):
        """Le site pagine côté CLIENT : au-delà de MAX_RESULTS (500) annonces,
        le résultat est TRONQUÉ sans paramètre serveur pour tout voir. Jamais
        observé en live (count == len(items) sur les captures) : `count` est
        bumpé synthétiquement sur une copie de fixture."""
        criteria = manual_criteria()
        criteria["locations"] = [ROSNY, MONTREUIL]
        criteria["sourceOverrides"] = {
            "orpi": {"slugs": ["rosny-sous-bois", "montreuil"]}
        }
        payload = {**UNION_2_VILLES, "count": MAX_RESULTS + 1}

        listings = run_scrape(criteria, FakeSession([payload]))

        assert len(listings) == len(UNION_2_VILLES["items"])
        assert any(
            level == "WARNING"
            and "tronqué par le site" in message
            and str(MAX_RESULTS) in message
            for level, message in logged
        )

    def test_total_count_is_dead_and_never_triggers_anything(self, logged):
        """`totalCount` vaut toujours 0 dans les captures, même avec 7 annonces :
        le parser ne le lit pas — seul `count` décide du warning."""
        payload = {**UNION_2_VILLES, "totalCount": MAX_RESULTS + 1}

        run_scrape(manual_criteria(), FakeSession([payload]))

        assert not any(level == "WARNING" for level, _ in logged)


# ---------------------------------------------------------------------------
# Le registre connaît la source
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_orpi_is_registered_under_its_source_id(self):
        from parsers.base import ParserRegistry

        assert ParserRegistry.get("orpi").SOURCE_NAME == "Orpi"
