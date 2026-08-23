"""Tests unitaires de parsers/guyhoquet.py.

Guy Hoquet sert ses résultats en AJAX JSON : `to_native()`, `_search_params()`
et `build_search_url()` sont des fonctions PURES — c'est là que se trouve la
valeur de ces tests ; le transport HTTP est mocké par `requests_mock` (jamais
de vrai réseau, cf. tests/conftest.py).

Invariants figés ici, chacun vérifié en direct le 23/08/2026 :

* la query AJAX exige des IDs NUMÉRIQUES (`filters[20][]`) : la variante
  préfixée `filters[f20][]` est ignorée SILENCIEUSEMENT par le site — la
  réponse repasse en France entière (total=16380 au lieu de 17, vérifié dans
  les deux sens) ; le préfixe « f » n'existe que dans le hash de l'URL
  publique (`#1&p=1&f20=a,b` : littéral « 1 », puis la page, puis les
  filtres — format natif de frontend/js/common/filters.js) ;
* UN SEUL appel markers pour tous les périmètres (fusion OR native du site),
  avec bascule sur la pagination HTML quand la source TRONQUE silencieusement
  sa réponse au-delà de 1000 annonces ;
* sémantique pièces : le canonique note « 5 » pour « 5+ », les cases GH sont
  EXACTES de 1 à 10 — une demande « 5+ » s'étend donc en [5..10] ;
* deux formats de markers coexistent côté site (imbriqué `price{}`/
  `address{}` avec type en code numérique vs plat au premier niveau avec type
  en libellé) — reproduits d'après captures réelles du jour.

Les blobs HTML sont des reproductions minimales du balisage réel des cartes
(`div.resultat-item[data-id]`), comme dans tests/unit/test_parser_century21.py.
"""

from __future__ import annotations

import copy
import json

import pytest
import requests
from bs4 import BeautifulSoup

from core.criteria import PROPERTY_TYPES
from core.geocode import REGION
from parsers.guyhoquet import (
    BASE_URL,
    MAX_PAGES,
    RESULT_URL,
    GuyHoquetParser,
    _as_float,
    _dict_to_listing,
    _format_price,
    _gh_bedroom_values,
    _gh_room_values,
    _parse_card,
    _property_type_label,
)
from tests.helpers.factories import (
    make_city_location,
    make_criteria,
    make_department_location,
    make_region_location,
    make_whole_city_location,
)

# ---------------------------------------------------------------------------
# Périmètres réutilisés
# ---------------------------------------------------------------------------

TOULOUSE = make_city_location("Toulouse", "31000", "31555")
LYON = make_city_location("Lyon", "69003", "69381")
IVRY = make_city_location("Ivry-sur-Seine", "94200", "94052")
HAUTE_GARONNE = make_department_location("31", "Haute-Garonne")
OCCITANIE = make_region_location("76", "Occitanie", ("31", "82", "81"))
REGION_NUE = {"kind": REGION, "code": "76", "name": "Occitanie"}

SLUGS_BY_KIND = {
    "city": lambda loc: f"{loc['city'].lower().replace(' ', '-')}-{loc['postalCode']}_c3",
}


def stub_slugs(monkeypatch, mapping):
    """Remplace la résolution réseau des slugs : chaque périmètre reçoit le
    slug de sa classe (`mapping[kind]`), None si absente. Enregistre aussi les
    appels pour vérifier ce que le parser passe au service (repo inclus)."""
    calls: list[tuple[dict, object]] = []

    def fake_resolve(location, repo):
        calls.append((location, repo))
        resolver = mapping.get(location.get("kind", "city"))
        return resolver(location) if callable(resolver) else resolver

    monkeypatch.setattr("services.guyhoquet_geocode.resolve_slug", fake_resolve)
    return calls


def gh_criteria(**overrides) -> dict:
    """Des critères canoniques sur Toulouse 31000 : le CP des fixtures."""
    criteria = make_criteria(locations=[TOULOUSE])
    criteria.update(overrides)
    return criteria


# ---------------------------------------------------------------------------
# Fabriques de réponse — formats markers observés en direct le 23/08/2026
# ---------------------------------------------------------------------------

def markers_response(total, *results) -> dict:
    """Le corps JSON markers, tel qu'observé (capture Toulouse gh_hdr.txt)."""
    return {"success": True, "markers": {"error": False, "total": total, "max_score": 1, "results": list(results)}}


def nested_marker(
    ad_id: int = 1894191,
    *,
    zip_code: str = "94200",
    city: str = "Ivry-sur-Seine",
    price: float | int = 1455,
    transaction: int = 2,
    marker_type=1,
    **overrides,
) -> dict:
    """Un marker au format RICHE (capture Ivry gh_props.json) : price/address
    imbriqués, type en code numérique."""
    data = {
        "id": ad_id,
        "name": "Appartement T4 meublé - 70m2",
        "description": 'Superbe appartement<br />\navec <b>ascenseur</b> et &eacute;té',
        "reference": str(ad_id),
        "type": marker_type,
        "type_transaction": transaction,
        "exclusivity": 0,
        "surface": 69.94,
        "number_room": 4,
        "energy_consumption": "E",
        "ges": "E",
        "created_at": "2026-08-22 18:08:17",
        "updated_at": "2026-08-22 19:11:38",
        "pictures": ["https://media.example/a.jpg", "", "https://media.example/b.jpg"],
        "virtual_visit": 1,
        "price": {"price": price, "old_price": None},
        "address": {"zip": zip_code, "city": city},
    }
    data.update(overrides)
    return data


def flat_marker(
    ad_id: str = "1890839",
    *,
    zip_code: str = "31000",
    city: str = "Toulouse",
    price: float | int = 525000,
    transaction: str = "1",
    marker_type="Maison",
    **overrides,
) -> dict:
    """Un marker au format PLAT (capture Toulouse gh_hdr.txt) : zip/city/price
    au premier niveau, type en libellé."""
    data = {
        "id": ad_id,
        "name": "Maison à vendre de 7 pièces de 130.20 m²",
        "description": "Maison familiale.",
        "reference": str(ad_id),
        "type": marker_type,
        "type_transaction": transaction,
        "exclusivity": 1,
        "surface": "130.2",
        "number_room": "7",
        "pictures": ["https://cdn.example/a.jpg"],
        "zip": zip_code,
        "city": city,
        "price": price,
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Fabriques de HTML — reproduction du balisage réel des cartes paginées
# ---------------------------------------------------------------------------

def card(
    card_id: str = "1894176",
    *,
    title: str = "Appartement 3 pièces 59.09 m²",
    city_block: str | None = "Villejuif 94800",
    price: str = "214 000 €",
    href: str | None = None,
) -> str:
    """Une carte `div.resultat-item` au balisage réel (capture
    templates.properties) : lien `a.property_link_block`, titre `span.ttl`,
    bloc ville dédié (seul contenu de son div), prix `div.price`."""
    link = href if href is not None else (
        f"https://www.guy-hoquet.com/achat-vente/appartement-villejuif-94800-{card_id}"
    )
    city_div = f'<div class="text-truncate">{city_block}</div>' if city_block else ""
    return (
        f'<div class="with_map resultat-item" data-id="{card_id}">'
        f'<a href="{link}" class="property_link_block">'
        f'<span class="ttl property-name">{title}</span>'
        f"{city_div}"
        f'<div class="price">{price}</div>'
        "</a></div>"
    )


def cards_page(*cards: str) -> str:
    """Le fragment `templates.properties` d'une page paginée."""
    inner = "".join(cards)
    return f"<div>{inner}</div>"


EMPTY_PAGE_HTML = "<html><body></body></html>"


def parse_single_card(card_html: str):
    return _parse_card(BeautifulSoup(card_html, "lxml").select_one("div.resultat-item[data-id]"))


def route_gh_responses(requests_mock, *, markers: dict, page_html) -> object:
    """Route les requêtes du parser : `with_markers=true` reçoit le corps
    markers, les autres appels reçoivent `page_html(numéro_de_page)`."""

    def handler(request, context):
        if request.qs.get("with_markers") == ["true"]:
            return json.dumps(markers)
        return json.dumps({"templates": {"properties": page_html(int(request.qs["p"][0]))}})

    return requests_mock.get(RESULT_URL, text=handler)


def markers_requests(mock) -> list:
    return [r for r in mock.request_history if r.qs.get("with_markers") == ["true"]]


def page_requests(mock) -> list:
    return [r for r in mock.request_history if r.qs.get("with_markers") != ["true"]]


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
# Sémantique pièces / chambres — l'expansion « 5 » canonique = « 5+ »
# ---------------------------------------------------------------------------

class TestGhRoomValues:
    def test_five_canonical_means_five_or_more_so_the_range_extends_to_ten(self):
        """Les cases GH sont EXACTES de 1 à 10 (« 10+ » sur la dernière) :
        une demande « 5+ » doit couvrir 5, 6, 7, 8, 9 et 10 — l'OR
        multi-valeurs du filtre étant vérifié côté site."""
        assert _gh_room_values([5]) == ["5", "6", "7", "8", "9", "10"]

    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            ([2, 3], ["2", "3"]),
            ([3, 2], ["2", "3"]),
            ([2, "2"], ["2"]),
            ([1], ["1"]),
            ([4, "deux", None], ["4"]),
            ([0, -2], []),
            ([], []),
            (None, []),
        ],
        ids=["simples", "non_triees", "duplicat", "un", "non_entiers_ignores", "negatifs", "vide", "none"],
    )
    def test_values_are_sorted_deduplicated_and_clean(self, values, expected):
        assert _gh_room_values(values) == expected


class TestGhBedroomValues:
    def test_seven_is_clamped_to_five_plus(self):
        """« 5+ » des DEUX côtés (dernière case GH libellée « 5+ ») : mapping
          direct plafonné à 5, sans extension artificielle."""
        assert _gh_bedroom_values([7]) == ["5"]

    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            ([1, 2], ["1", "2"]),
            ([5, 9], ["5"]),
            (["deux", None], []),
            ([], []),
            (None, []),
        ],
        ids=["simples", "plafonnees", "non_entiers_ignores", "vide", "none"],
    )
    def test_values_are_sorted_and_clamped(self, values, expected):
        assert _gh_bedroom_values(values) == expected


# ---------------------------------------------------------------------------
# to_native — traduction vers les IDs numériques de filtres du site
# ---------------------------------------------------------------------------

class TestToNative:
    def test_a_full_criteria_translates_to_every_numeric_filter(self, monkeypatch):
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})
        criteria = gh_criteria(
            transaction="buy",
            propertyTypes=["apartment", "house"],
            priceMax=340000,
            priceMin=100000,
            surfaceMin=30,
            surfaceMax=90,
            rooms=[3],
            bedrooms=[2],
        )

        native = GuyHoquetParser().to_native(criteria)

        assert native == {
            "10": ["1"],
            "20": ["toulouse-31000_c3"],
            "30": ["appartement", "maison"],
            "40": [340000],
            "45": [100000],
            "50": [30],
            "60": [90],
            "70": ["3"],
            "80": ["2"],
        }

    def test_rent_is_code_two_and_parking_land_have_their_own_words(self, monkeypatch):
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})
        criteria = gh_criteria(transaction="rent", propertyTypes=["parking", "land"])

        native = GuyHoquetParser().to_native(criteria)

        assert native["10"] == ["2"]
        assert native["30"] == ["parking-box", "terrain"]

    def test_unknown_property_types_are_silently_dropped(self, monkeypatch):
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})
        native = GuyHoquetParser().to_native(gh_criteria(propertyTypes=["yacht", "apartment"]))

        assert native["30"] == ["appartement"]

    def test_absent_criteria_leave_their_filter_out(self, monkeypatch):
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})
        native = GuyHoquetParser().to_native(gh_criteria(
            transaction=None, propertyTypes=[], priceMax=None, rooms=None, bedrooms=None
        ))

        assert native == {"20": ["toulouse-31000_c3"]}

    def test_room_expansion_lands_in_filters_70_and_80(self, monkeypatch):
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})
        native = GuyHoquetParser().to_native(gh_criteria(rooms=[5], bedrooms=[7]))

        assert native["70"] == ["5", "6", "7", "8", "9", "10"]
        assert native["80"] == ["5"]

    def test_the_criteria_are_never_mutated(self, monkeypatch):
        """🔒 Les critères sont partagés entre toutes les sources d'un même
        scrape : toute mutation en cours de route corromprait les sources
        suivantes. Comparaison profonde avant/après."""
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})
        criteria = gh_criteria(
            transaction="buy",
            propertyTypes=["apartment"],
            priceMax=340000,
            surfaceMin=30,
            rooms=[5],
            bedrooms=[2],
            locations=[make_region_location("76", "Occitanie")],  # région SANS departments
        )
        snapshot = copy.deepcopy(criteria)

        GuyHoquetParser().to_native(criteria)

        assert criteria == snapshot


# ---------------------------------------------------------------------------
# _search_params — LE piège : IDs numériques obligatoires dans la query AJAX
# ---------------------------------------------------------------------------

class TestSearchParams:
    NATIVE = {
        "10": ["2"],
        "20": ["toulouse-31000_c3", "lyon-69003_c3"],
        "40": [1500],
        "70": ["5", "6"],
    }

    def test_multi_valued_filters_repeat_their_key_and_page_comes_first(self):
        params = GuyHoquetParser()._search_params(self.NATIVE, page=2, with_markers=True)

        assert params == [
            ("p", "2"),
            ("filters[10][]", "2"),
            ("filters[20][]", "toulouse-31000_c3"),
            ("filters[20][]", "lyon-69003_c3"),
            ("filters[40][]", "1500"),
            ("filters[70][]", "5"),
            ("filters[70][]", "6"),
            ("with_markers", "true"),
        ]

    def test_the_localisation_key_never_carries_the_f_prefix(self):
        """🔒 `filters[f20][]` est ignoré SILENCIEUSEMENT par le site : la
        réponse repasse en France entière (vérifié en direct dans les deux sens).
        Le préfixe « f » ne vit QUE dans le hash de l'URL publique."""
        params = dict(GuyHoquetParser()._search_params({"20": ["x_c3"]}, page=1, with_markers=True))

        assert params["filters[20][]"] == "x_c3"
        assert all(not key.startswith("filters[f") for key in params)

    def test_with_markers_takes_the_false_form_for_html_pages(self):
        params = GuyHoquetParser()._search_params({}, page=7, with_markers=False)

        assert params == [("p", "7"), ("with_markers", "false")]


# ---------------------------------------------------------------------------
# build_search_url — le hash public #1&p=1&fXX=valeurs (format natif du site)
# ---------------------------------------------------------------------------

class TestBuildSearchUrl:
    def test_the_hash_follows_the_site_s_own_native_format(self, monkeypatch):
        """Format natif exact produit par le frontend du site
        (frontend/js/common/filters.js) : hashKey initialisé au LITTÉRAL « 1 »,
        puis `p=<page>`, puis chaque filtre `&fXX=valeurs` (valeurs jointes par
        virgules), ordre fixe 10→80 — vérifié octet pour octet contre une URL
        capturée sur le site réel."""
        stub_slugs(monkeypatch, {
            "city": SLUGS_BY_KIND["city"],
            "department": lambda loc: f"{loc['code'].lower()}_c2",
        })
        criteria = gh_criteria(
            locations=[make_city_location("Toulouse", "31000", "31555"), HAUTE_GARONNE],
            transaction="rent",
            propertyTypes=["apartment"],
            priceMax=1200,
            rooms=[5],
        )

        url = GuyHoquetParser().build_search_url(criteria)

        assert url == (
            f"{BASE_URL}/biens/result#1&p=1&f10=2&f20=toulouse-31000_c3,31_c2"
            "&f30=appartement&f40=1200&f70=5,6,7,8,9,10"
        )
        # 🔒 Piège init-hash.js : le site relit les filtres en cherchant
        # LITTÉRALEMENT « &f10 » AVEC le « & » — un segment de filtre collé à
        # la croise (« #f10=... », sans « & » devant) n'est JAMAIS lu côté
        # client : le premier segment du hash doit rester le littéral « 1 ».
        assert url.partition("#")[2].split("&", 1)[0] == "1"

    def test_only_present_filters_appear_in_order(self, monkeypatch):
        """Ordre fixe 10→80 du site : les filtres absents sont simplement
        sautés, jamais émis vides."""
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})
        criteria = {"locations": [TOULOUSE], "priceMax": 340000}

        assert GuyHoquetParser().build_search_url(criteria) == (
            f"{BASE_URL}/biens/result#1&p=1&f20=toulouse-31000_c3&f40=340000"
        )

    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}, {"locations": [{"kind": "city", "city": "Paris"}]}],
        ids=["vide", "liste_vide", "localisation_non_resolue"],
    )
    def test_no_url_without_any_resolved_perimeter(self, criteria, monkeypatch):
        stub_slugs(monkeypatch, {})

        assert GuyHoquetParser().build_search_url(criteria) is None

    def test_build_search_urls_wraps_the_single_merged_url(self, monkeypatch):
        """La recherche GH fusionne nativement toutes les localisations :
        cette URL est unique — miroir exact du scrape, qui fait lui aussi un
        seul appel."""
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})
        criteria = {"locations": [TOULOUSE, LYON]}

        urls = GuyHoquetParser().build_search_urls(criteria)

        assert urls == [f"{BASE_URL}/biens/result#1&p=1&f20=toulouse-31000_c3,lyon-69003_c3"]


# ---------------------------------------------------------------------------
# _dict_to_listing — les deux formats de markers
# ---------------------------------------------------------------------------

class TestDictToListingNestedFormat:
    def test_maps_a_rich_marker_to_the_common_listing_schema(self):
        listing = _dict_to_listing(nested_marker())

        assert listing.listing_id == "gh_1894191"
        assert listing.source == "guyhoquet"
        assert listing.url == f"{BASE_URL}/location/x-1894191"
        assert listing.title == "Appartement T4 meublé - 70m2"
        assert listing.price == "1 455 €/mois"
        assert listing.price_value == 1455.0
        assert listing.surface == "69.94"
        assert listing.rooms == "4"
        assert listing.city == "Ivry-sur-Seine"
        assert listing.zip_code == "94200"
        assert listing.location == "Ivry-sur-Seine 94200"
        assert listing.property_type == "Appartement"  # code numérique observé : 1
        assert listing.is_new is False
        assert listing.epc == "E"
        assert listing.ges == "E"
        assert listing.is_exclusive is False
        assert listing.has_3d_visit is True
        assert listing.legacy_id == "1894191"
        assert listing.creation_date == "2026-08-22 18:08:17"
        assert listing.update_date == "2026-08-22 19:11:38"

    def test_the_nested_price_and_address_win_over_flat_fields(self):
        """Quand les deux formats coexistent dans un même marker, l'imbrication
        fait foi (comportement de _nested_or_flat)."""
        marker = nested_marker(zip_code="99999", city="Nawak", address={"zip": "94200", "city": "Ivry-sur-Seine"})

        listing = _dict_to_listing(marker)

        assert listing.zip_code == "94200"
        assert listing.city == "Ivry-sur-Seine"

    def test_photos_keep_only_real_urls_and_feed_image_url(self):
        marker = nested_marker(pictures=["https://media.example/a.jpg", "", None, "https://media.example/b.jpg"])

        listing = _dict_to_listing(marker)

        assert json.loads(listing.photos) == [
            {"url": "https://media.example/a.jpg"},
            {"url": "https://media.example/b.jpg"},
        ]
        assert listing.image_url == "https://media.example/a.jpg"

    def test_the_description_keeps_its_html_entities_and_drops_the_tags(self):
        """Le site laisse les entités HTML dans ses descriptions markers : le
        parser ne retire que les balises (comportement de _clean_description)."""
        marker = nested_marker(description="Ligne 1<br />\nLigne <b>2</b> &amp; fin")

        assert _dict_to_listing(marker).description == "Ligne 1 Ligne 2 &amp; fin"

    def test_a_numeric_type_one_means_appartement(self):
        listing = _dict_to_listing(nested_marker(marker_type=1))

        assert listing.property_type == "Appartement"

    def test_an_unobserved_numeric_type_gives_no_guessed_label(self):
        """Seul le code 1 (« Appartement ») a été observé en direct : tout autre
        code vaut libellé vide plutôt qu'une supposition."""
        listing = _dict_to_listing(nested_marker(marker_type=12))

        assert listing.property_type == ""
        assert listing.is_new is False

    def test_a_buy_transaction_gets_the_achat_vente_prefix(self):
        marker = nested_marker(ad_id=42, transaction=1, price=214000)

        listing = _dict_to_listing(marker)

        assert listing.url == f"{BASE_URL}/achat-vente/x-42"
        assert listing.price == "214 000 €"


class TestDictToListingFlatFormat:
    def test_maps_a_flat_marker_to_the_common_listing_schema(self):
        """Capture Toulouse gh_hdr.txt : zip/city/price au premier niveau,
        type en libellé, id et transaction en chaînes."""
        listing = _dict_to_listing(flat_marker())

        assert listing.listing_id == "gh_1890839"
        assert listing.url == f"{BASE_URL}/achat-vente/x-1890839"
        assert listing.city == "Toulouse"
        assert listing.zip_code == "31000"
        assert listing.location == "Toulouse 31000"
        assert listing.price == "525 000 €"
        assert listing.price_value == 525000.0
        assert listing.surface == "130.2"
        assert listing.rooms == "7"
        assert listing.property_type == "Maison"
        assert listing.is_exclusive is True

    def test_a_programme_neuf_label_marks_the_listing_as_new(self):
        listing = _dict_to_listing(flat_marker(marker_type="Programme neuf"))

        assert listing.property_type == "Programme neuf"
        assert listing.is_new is True

    def test_a_decimal_string_price_parses_into_price_value(self):
        listing = _dict_to_listing(flat_marker(price="16505"))

        assert listing.price == "16 505 €"
        assert listing.price_value == 16505.0


class TestDictToListingEdgeCases:
    def test_a_missing_id_yields_no_url_at_all(self):
        listing = _dict_to_listing(nested_marker(ad_id=""))

        assert listing.listing_id == "gh_"
        assert listing.url == ""

    def test_an_unknown_transaction_code_falls_back_to_location(self):
        listing = _dict_to_listing(nested_marker(ad_id=42, transaction=5))

        assert listing.url == f"{BASE_URL}/location/x-42"

    def test_a_missing_price_displays_empty_and_values_none(self):
        marker = nested_marker()
        del marker["price"]

        listing = _dict_to_listing(marker)

        assert listing.price == ""
        assert listing.price_value is None

    def test_a_missing_description_and_photos_yield_empty_strings(self):
        listing = _dict_to_listing(nested_marker(description="", pictures=[]))

        assert listing.description == ""
        assert listing.image_url == ""
        assert json.loads(listing.photos) == []

    def test_missing_dates_and_energy_fields_default_to_empty(self):
        marker = nested_marker()
        for key in ("created_at", "updated_at", "energy_consumption", "ges"):
            del marker[key]

        listing = _dict_to_listing(marker)

        assert listing.creation_date == ""
        assert listing.update_date == ""
        assert listing.epc == ""
        assert listing.ges == ""


# ---------------------------------------------------------------------------
# Helpers purs : prix, types, tolérance numérique
# ---------------------------------------------------------------------------

class TestFormatPrice:
    @pytest.mark.parametrize(
        ("price", "transaction", "expected"),
        [
            (1455, "2", "1 455 €/mois"),
            (525000, "1", "525 000 €"),
            (None, "1", ""),
        ],
        ids=["loyer", "vente", "absent"],
    )
    def test_the_display_follows_the_transaction(self, price, transaction, expected):
        assert _format_price(price, transaction) == expected


class TestPropertyTypeLabel:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Maison", "Maison"),
            ("  Loft  ", "Loft"),
            (1, "Appartement"),
            (12, ""),  # code non observé : pas de supposition
            ("", ""),
            (None, ""),
        ],
        ids=["libelle", "libelle_espace", "code_connu", "code_inconnu", "vide", "none"],
    )
    def test_both_encodings_of_the_type_field(self, raw, expected):
        assert _property_type_label(raw) == expected


class TestAsFloat:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("1 650,5", 1650.5), (1650, 1650.0), ("130.2", 130.2), (None, None), ("nawak", None)],
        ids=["format_fr", "entier", "decimal_str", "none", "illisible"],
    )
    def test_tolerant_number_parsing(self, value, expected):
        assert _as_float(value) == expected


# ---------------------------------------------------------------------------
# _parse_card — cartes HTML de la pagination
# ---------------------------------------------------------------------------

class TestParseCard:
    def test_extracts_every_field_of_a_real_card(self):
        listing = parse_single_card(card())

        assert listing.listing_id == "gh_1894176"
        assert listing.source == "guyhoquet"
        assert listing.url == "https://www.guy-hoquet.com/achat-vente/appartement-villejuif-94800-1894176"
        assert listing.title == "Appartement 3 pièces 59.09 m²"
        assert listing.surface == "59.09"
        assert listing.rooms == "3"
        assert listing.city == "Villejuif"
        assert listing.zip_code == "94800"
        assert listing.location == "Villejuif 94800"
        assert listing.price == "214 000 €"
        assert listing.property_type == "Appartement"

    def test_the_singular_piece_word_is_tolerated(self):
        listing = parse_single_card(card(card_id="7", title="Studio 1 pièce 25 m²"))

        assert listing.rooms == "1"

    def test_the_decimal_comma_surface_is_normalised(self):
        listing = parse_single_card(card(card_id="7", title="Appartement 2 pièces 59,09 m²"))

        assert listing.surface == "59.09"

    def test_a_title_with_a_five_digit_surface_does_not_steal_the_city(self):
        """🔒 Le repli prend la DERNIÈRE occurrence du motif ville : un titre
        peut contenir un nombre à 5 chiffres (« 12345 m² ») qui ressemble à un
        code postal — le bloc ville vient toujours après lui."""
        html = (
            '<div class="resultat-item" data-id="7"><a href="/a-7" class="property_link_block">'
            '<span class="ttl">Loft 12345 m²</span>'
            "<span>Ivry-sur-Seine 94200</span>"
            "</a></div>"
        )

        listing = parse_single_card(html)

        assert listing.city == "Ivry-sur-Seine"
        assert listing.zip_code == "94200"

    def test_a_card_without_a_dedicated_city_block_still_finds_it_in_the_text(self):
        html = (
            '<div class="resultat-item" data-id="7"><a href="/a-7" class="property_link_block">'
            '<span class="ttl">Appartement 59 m²</span>'
            "<p>Villejuif 94800</p>"
            "</a></div>"
        )

        listing = parse_single_card(html)

        assert listing.city == "Villejuif"
        assert listing.zip_code == "94800"

    def test_no_city_at_all_yields_empty_fields_but_keeps_the_listing(self):
        listing = parse_single_card(card(card_id="7", city_block=None, title="Appartement 59 m²"))

        assert listing.city == ""
        assert listing.zip_code == ""
        assert listing.location == ""
        assert listing.listing_id == "gh_7"

    def test_a_card_without_data_id_is_rejected(self):
        html = (
            '<div class="resultat-item"><a href="/a" class="property_link_block"><span class="ttl">X</span></a></div>'
        )
        soup = BeautifulSoup(html, "lxml")

        assert _parse_card(soup.select_one("div.resultat-item")) is None

    def test_a_card_without_any_link_is_rejected(self):
        html = '<div class="resultat-item" data-id="7"><span class="ttl">X</span></div>'
        soup = BeautifulSoup(html, "lxml")

        assert _parse_card(soup.select_one("div.resultat-item")) is None

    def test_a_plain_link_without_the_expected_class_is_accepted(self):
        html = (
            '<div class="resultat-item" data-id="7">'
            '<a href="/fiche-7"><span class="ttl">Appartement 59 m²</span></a>'
            "</div>"
        )

        listing = parse_single_card(html)

        assert listing.url == "/fiche-7"


# ---------------------------------------------------------------------------
# _scoped_location — l'élargissement d'une région nue se fait sur COPIE
# ---------------------------------------------------------------------------

class TestScopedLocation:
    def test_a_region_without_departments_is_completed_from_the_geo_api(self, monkeypatch):
        calls: list[str] = []

        def fake_region_departments(code):
            calls.append(code)
            return ["31", "82"]

        monkeypatch.setattr("core.geocode.region_departments", fake_region_departments)

        scoped = GuyHoquetParser()._scoped_location(dict(REGION_NUE))

        assert scoped["departments"] == ["31", "82"]
        assert calls == ["76"]
        # La complétion vit sur la copie : l'original reste nu.
        assert "departments" not in REGION_NUE

    def test_a_region_already_holding_departments_is_returned_as_is(self, monkeypatch):
        monkeypatch.setattr("core.geocode.region_departments", lambda code: pytest.fail("ne doit pas être appelé"))

        location = OCCITANIE

        assert GuyHoquetParser()._scoped_location(location) is location

    def test_a_city_or_department_is_returned_as_is(self, monkeypatch):
        monkeypatch.setattr("core.geocode.region_departments", lambda code: pytest.fail("ne doit pas être appelé"))

        for location in (TOULOUSE, IVRY, HAUTE_GARONNE):
            assert GuyHoquetParser()._scoped_location(location) is location

    def test_a_region_without_any_code_cannot_be_completed(self, monkeypatch):
        monkeypatch.setattr("core.geocode.region_departments", lambda code: pytest.fail("ne doit pas être appelé"))
        naked = {"kind": REGION, "name": "Corse"}

        assert GuyHoquetParser()._scoped_location(naked) is naked


# ---------------------------------------------------------------------------
# _geo_repo / _slugs — injection explicite du storage
# ---------------------------------------------------------------------------

class TestGeoRepo:
    def test_the_repo_comes_from_the_injected_storage(self):
        from tests.helpers.fakes import fake_storage

        storage = fake_storage()

        assert GuyHoquetParser(storage=storage)._geo_repo() is storage.guyhoquet_geo
        assert GuyHoquetParser()._geo_repo() is None


class TestSlugs:
    def test_slugs_are_resolved_in_criteria_order_and_deduplicated(self, monkeypatch):
        from tests.helpers.fakes import fake_storage

        parser = GuyHoquetParser(storage=fake_storage())
        criteria = {"locations": [TOULOUSE, HAUTE_GARONNE]}
        calls = stub_slugs(monkeypatch, {
            "city": SLUGS_BY_KIND["city"],
            # Le département résout au même slug que Toulouse (simulation).
            "department": lambda loc: "toulouse-31000_c3",
        })

        slugs = parser._slugs(criteria)

        assert slugs == ["toulouse-31000_c3"]
        assert [loc["kind"] for loc, _ in calls] == ["city", "department"]

    def test_the_repo_is_forwarded_from_the_storage(self, monkeypatch):
        from tests.helpers.fakes import fake_storage

        storage = fake_storage()
        parser = GuyHoquetParser(storage=storage)
        calls = stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})

        parser._slugs({"locations": [TOULOUSE]})

        assert calls == [(TOULOUSE, storage.guyhoquet_geo)]

    def test_an_unresolved_perimeter_is_skipped_without_blocking_the_others(self, monkeypatch):
        parser = GuyHoquetParser()
        stub_slugs(monkeypatch, {
            "city": lambda loc: None,  # aucune ville ne résout
            "department": lambda loc: f"{loc['code'].lower()}_c2",
        })

        assert parser._slugs({"locations": [TOULOUSE, HAUTE_GARONNE]}) == ["31_c2"]


# ---------------------------------------------------------------------------
# scrape : garde-fous d'entrée et appel markers unique
# ---------------------------------------------------------------------------

class TestScrapeGuards:
    def test_no_resolvable_perimeter_raises_before_any_request(self, monkeypatch, requests_mock):
        mock = requests_mock.get(RESULT_URL, json={})
        stub_slugs(monkeypatch, {})  # rien ne résout

        with pytest.raises(ValueError, match="Aucun périmètre Guy Hoquet"):
            GuyHoquetParser().scrape(gh_criteria(locations=[{"kind": "city", "city": "Paris"}]))

        assert mock.call_count == 0

    def test_a_malformed_markers_response_raises_instead_of_looking_empty(self, monkeypatch, requests_mock):
        """Une réponse sans champ `results` signe un changement de format du
        site : c'est un échec réel, pas une recherche vide légitime — il doit
        remonter pour que ScrapeService le distingue."""
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})
        requests_mock.get(RESULT_URL, json={"success": True, "markers": {}})

        with pytest.raises(ValueError, match="champ results absent"):
            GuyHoquetParser().scrape(gh_criteria())

    def test_a_real_http_error_is_raised_not_swallowed(self, monkeypatch, requests_mock):
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})
        requests_mock.get(RESULT_URL, status_code=500, json={})

        with pytest.raises(requests.HTTPError, match="500"):
            GuyHoquetParser().scrape(gh_criteria())


class TestScrapeMarkers:
    def test_one_merged_call_returns_listings_from_both_formats(self, monkeypatch, requests_mock):
        """La recherche GH fusionne nativement toutes les localisations : UN
        seul appel markers quel que soit le nombre de villes."""
        mock = route_gh_responses(
            requests_mock,
            markers=markers_response(
                2, nested_marker(ad_id=1), flat_marker(ad_id="2"), nested_marker(ad_id=1, zip_code="31100")
            ),
            page_html=lambda page: pytest.fail("pagination inattendue"),
        )
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})

        listings = GuyHoquetParser().scrape(gh_criteria(locations=[TOULOUSE, IVRY], transaction="rent"))

        assert [li.listing_id for li in listings] == ["gh_1", "gh_2"]
        assert len(markers_requests(mock)) == 1
        assert page_requests(mock) == []

    def test_duplicates_are_kept_once(self, monkeypatch, requests_mock):
        route_gh_responses(
            requests_mock,
            markers=markers_response(3, nested_marker(ad_id=1), nested_marker(ad_id=1), nested_marker(ad_id=2)),
            page_html=lambda page: "",
        )
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})

        listings = GuyHoquetParser().scrape(gh_criteria(locations=[IVRY]))

        assert [li.listing_id for li in listings] == ["gh_1", "gh_2"]

    def test_out_of_area_listings_are_dropped(self, monkeypatch, requests_mock):
        """Le contrôle aval par préfixes postaux reste le garde-fou : un
        marker dont le zip sort du périmètre ne ressort jamais."""
        route_gh_responses(
            requests_mock,
            markers=markers_response(
                2,
                nested_marker(ad_id=1, zip_code="31000"),
                nested_marker(ad_id=99, zip_code="75001"),
            ),
            page_html=lambda page: "",
        )
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})

        listings = GuyHoquetParser().scrape(gh_criteria(locations=[TOULOUSE]))

        assert [li.listing_id for li in listings] == ["gh_1"]

    def test_the_request_announces_ajax_headers_and_a_timeout(self, monkeypatch, requests_mock):
        """Sans `X-Requested-With: XMLHttpRequest`, l'endpoint répond un shell
        HTML au lieu du JSON (vérifié en direct) — le header fait partie du
        contrat."""
        mock = route_gh_responses(
            requests_mock,
            markers=markers_response(0),
            page_html=lambda page: EMPTY_PAGE_HTML,
        )
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})

        GuyHoquetParser().scrape(gh_criteria())

        request = mock.last_request
        assert request.qs["filters[20][]"] == ["toulouse-31000_c3"]
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        assert "Chrome" in request.headers["User-Agent"]
        assert request.timeout == 15


# ---------------------------------------------------------------------------
# scrape : bascule pagination HTML au-delà du plafond markers
# ---------------------------------------------------------------------------

class TestScrapePaged:
    TOTAL_OVER_LIMIT = 1500  # > _MARKERS_LIMIT : la source tronque sa réponse

    def test_a_total_over_the_markers_limit_switches_to_html_pagination(
        self, monkeypatch, requests_mock, logged
    ):
        def page_html(page: int) -> str:
            if page <= 2:
                first = (page - 1) * 18
                return cards_page(*(card(str(first + i), city_block="Toulouse 31000") for i in range(1, 19)))
            return EMPTY_PAGE_HTML  # page suivante vide : arrêt propre

        mock = route_gh_responses(requests_mock, markers=markers_response(self.TOTAL_OVER_LIMIT), page_html=page_html)
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})

        listings = GuyHoquetParser().scrape(gh_criteria())

        assert len(listings) == 36
        assert len(page_requests(mock)) == 3
        assert any("[Guy Hoquet]" in m and "plafond markers" in m and "pagination HTML" in m for _, m in logged)
        # La page paginée demande bien du HTML (with_markers=false).
        assert page_requests(mock)[0].qs.get("with_markers") == ["false"]

    def test_pages_stop_at_the_first_page_without_cards(self, monkeypatch, requests_mock):
        route_gh_responses(
            requests_mock,
            markers=markers_response(self.TOTAL_OVER_LIMIT),
            page_html=lambda page: EMPTY_PAGE_HTML,
        )
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})

        assert GuyHoquetParser().scrape(gh_criteria()) == []

    def test_duplicates_across_pages_are_kept_once(self, monkeypatch, requests_mock):
        route_gh_responses(
            requests_mock,
            markers=markers_response(self.TOTAL_OVER_LIMIT),
            page_html=lambda page: (
                cards_page(card("111", city_block="Toulouse 31000"), card("222", city_block="Toulouse 31000"))
                if page == 1
                else cards_page(card("222", city_block="Toulouse 31000"))
            ),
        )
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})

        listings = GuyHoquetParser().scrape(gh_criteria())

        assert [li.listing_id for li in listings] == ["gh_111", "gh_222"]

    def test_three_consecutive_pages_without_any_match_stop_the_crawl(self, monkeypatch, requests_mock, logged):
        """🔒 Filet anti-mode-dégradé : le site a été observé répondant 200 OK
        en IGNORANT silencieusement les filtres de localisation. Trois pages
        consécutives sans un seul bien du périmètre signent ce mode — inutile
        de marteler les 120 pages."""
        out_of_area = card("999", city_block="Paris 75001")
        mock = route_gh_responses(
            requests_mock,
            markers=markers_response(self.TOTAL_OVER_LIMIT),
            page_html=lambda page: cards_page(out_of_area),
        )
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})

        listings = GuyHoquetParser().scrape(gh_criteria())

        assert listings == []
        assert len(page_requests(mock)) == 3
        assert any("filtres semblent ignorés" in m and "arrêt anticipé" in m for _, m in logged)

    def test_a_matching_page_resets_the_degraded_counter(self, monkeypatch, requests_mock):
        def page_html(page: int) -> str:
            if page == 3:
                return cards_page(card("in-3", city_block="Toulouse 31000"))
            return cards_page(card(f"out-{page}", city_block="Paris 75001"))

        mock = route_gh_responses(requests_mock, markers=markers_response(self.TOTAL_OVER_LIMIT), page_html=page_html)
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})

        listings = GuyHoquetParser().scrape(gh_criteria())

        assert [li.listing_id for li in listings] == ["gh_in-3"]
        # Pages 1-2 hors périmètre (compteur remonté), page 3 dans le
        # périmètre (remise à zéro), pages 4-5-6 hors périmètre -> arrêt.
        assert len(page_requests(mock)) == 6

    def test_the_page_limit_caps_the_crawl_and_warns(self, monkeypatch, requests_mock, logged):
        """MAX_PAGES (120 x 18 = 2160 biens) borne le parcours : au-delà, la
        recherche reste volontairement tronquée plutôt que de tourner
        indéfiniment — même filet que les autres sources paginées."""
        total_beyond_max = MAX_PAGES * 18 + 500

        def page_html(page: int) -> str:
            first = (page - 1) * 18
            return cards_page(*(card(str(first + i), city_block="Toulouse 31000") for i in range(1, 19)))

        mock = route_gh_responses(requests_mock, markers=markers_response(total_beyond_max), page_html=page_html)
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})

        listings = GuyHoquetParser().scrape(gh_criteria())

        assert len(page_requests(mock)) == MAX_PAGES
        assert len(listings) == MAX_PAGES * 18
        assert any(
            level == "WARNING" and "tronquée aux 120 premières pages" in message
            for level, message in logged
        )


# ---------------------------------------------------------------------------
# scrape : région élargie sur COPIE, filtrage par départements complétés
# ---------------------------------------------------------------------------

class TestScrapeRegionScope:
    def test_a_naked_region_is_completed_for_the_postal_check_without_touching_the_criteria(
        self, monkeypatch, requests_mock
    ):
        """Sans liste de départements, une région n'aurait AUCUN préfixe
        postal et écarterait toutes les annonces en silence : elle est
        complétée depuis l'API geo, sur COPIE."""
        monkeypatch.setattr("core.geocode.region_departments", lambda code: ["31"])
        route_gh_responses(
            requests_mock,
            markers=markers_response(
                2,
                nested_marker(ad_id=1, zip_code="31100"),   # Haute-Garonne : dedans
                nested_marker(ad_id=99, zip_code="75001"),  # Paris : dehors
            ),
            page_html=lambda page: "",
        )
        stub_slugs(monkeypatch, {"region": lambda loc: f"{loc['code']}_c1"})
        criteria = gh_criteria(locations=[dict(REGION_NUE)])

        listings = GuyHoquetParser().scrape(criteria)

        assert [li.listing_id for li in listings] == ["gh_1"]
        # Les critères originaux restent intacts après le scrape.
        assert "departments" not in criteria["locations"][0]


# ---------------------------------------------------------------------------
# Capacités déclarées et contrat utilisateur
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_guyhoquet_declares_its_observed_capabilities(self):
        parser = GuyHoquetParser()

        assert parser.SOURCE_ID == "guyhoquet"
        assert parser.SOURCE_NAME == "Guy Hoquet"
        assert parser.SUPPORTED_TRANSACTIONS == ("rent", "buy")
        # Les quatre types canoniques sont référencés : valeur par défaut.
        assert parser.SUPPORTED_PROPERTY_TYPES == PROPERTY_TYPES
        # Décision produit : pas de repli manuel, la résolution hybride suffit.
        assert parser.MANUAL_OVERRIDE_LABEL == ""
        assert parser.MANUAL_OVERRIDE_HELP == ""
        assert parser.URL_NOTE == ""


class TestHasValidCriteria:
    @pytest.mark.parametrize(
        "criteria",
        [
            {"locations": [TOULOUSE]},
            {"locations": [make_whole_city_location()]},
            {"locations": [make_city_location("Montrouge", "92120")]},
            # Ville entière sans INSEE : la clé retombe sur le nom.
            {"locations": [{"kind": "whole_city", "city": "Poitiers", "postalCodes": ["86000"]}]},
            {"locations": [HAUTE_GARONNE]},
            {"locations": [OCCITANIE]},
        ],
        ids=[
            "commune",
            "ville_entiere",
            "commune_sans_insee",
            "ville_entiere_sans_insee",
            "departement_statique",
            "region_statique",
        ],
    )
    def test_any_identifiable_or_static_perimeter_is_valid(self, criteria):
        assert GuyHoquetParser().has_valid_criteria(criteria) is True

    @pytest.mark.parametrize(
        "criteria",
        [
            {},
            {"locations": []},
            {"locations": [{"kind": "city", "city": "Paris"}]},
            # Rejetée AVANT tout calcul : une ville entière sans code postal
            # n'est pas un périmètre canonique (normalize_locations).
            {"locations": [{"kind": "whole_city", "city": "Poitiers"}]},
            {"locations": [{"kind": "region", "name": "Guadeloupe"}]},
        ],
        ids=["vide", "liste_vide", "ville_sans_code", "ville_entiere_non_normalisable", "region_sans_code"],
    )
    def test_everything_else_is_invalid(self, criteria):
        assert GuyHoquetParser().has_valid_criteria(criteria) is False

    def test_validation_never_resolves_anything(self, monkeypatch):
        """Créer une recherche ne doit pas dépendre d'un appel réseau : la
        résolution elle-même est tentée au moment du scrape."""
        def fail_resolve(location, repo):
            raise AssertionError("aucune résolution pendant la validation")

        monkeypatch.setattr("services.guyhoquet_geocode.resolve_slug", fail_resolve)

        assert GuyHoquetParser().has_valid_criteria({"locations": [TOULOUSE, HAUTE_GARONNE]}) is True


class TestCannotSearchReason:
    def test_none_when_usable(self, monkeypatch):
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})

        assert GuyHoquetParser().cannot_search_reason(gh_criteria()) is None

    def test_the_location_message_names_the_static_levels_too(self):
        """Contrairement à Century 21, GH accepte département et région : le
        message dit « ville, département ou région »."""
        assert GuyHoquetParser().cannot_search_reason({}) == (
            "aucune localisation exploitable (ville, département ou région requis)"
        )

    def test_every_canonical_capability_is_covered(self, monkeypatch):
        stub_slugs(monkeypatch, {"city": SLUGS_BY_KIND["city"]})
        criteria = gh_criteria(
            transaction="buy",
            propertyTypes=["apartment", "house", "parking", "land"],
        )

        assert GuyHoquetParser().cannot_search_reason(criteria) is None
        assert GuyHoquetParser().unsupported_criteria(criteria) == []
