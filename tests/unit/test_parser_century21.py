"""Tests unitaires de parsers/century21.py.

Century 21 rend ses pages de résultats côté serveur : le module est donc fait de
fonctions PURES (parsing de cartes, filtres, formats d'URL) — c'est là que se
trouve la valeur de ces tests, comme pour Laforêt.

Invariants figés ici, chacun vérifié en direct le 15/08/2026 sur des captures
réelles de century21.fr (dossier materiel_century21/) :

* le parsing ne cible QUE `.c-the-list-of-properties-list .c-the-property-
  thumbnail-with-content[data-uid]` : la section « Biens se rapprochant »
  (`.c-the-list-of-properties-related`, biens HORS périmètre, ex. Paris 4e
  pour une recherche Paris 1er) et les blocs publicitaires `c-the-ad` (pas de
  data-uid) n'apparaissent jamais ;
* le code postal est TRONQUÉ pour les villes simples (Montrouge -> « 92 »,
  Nantes -> « 44 ») voire au-delà du département (Corse-du-Sud -> « 201 »,
  vérifié en direct) mais complet pour les arrondissements de Paris/Lyon/
  Marseille (« 75019 ») — `_location_ok` compare strictement un code complet
  et par préfixe un code incomplet DANS LES DEUX SENS (« 92 » tombe sous
  « 92120 », « 201 » couvre « 20 »), et échoue fermé si le code est vide ;
* le format du chemin dépend du NOMBRE de types demandés : aucun type ->
  `/annonces/f/achat/{slug}/`, un type -> `/annonces/achat-appartement/{slug}/`
  (sans le « f »), deux types et plus ->
  `/annonces/f/achat-appartement-maison/{slug}/` (un seul type avec « f »
  renvoie 410, vérifié) ;
* Century 21 est mono-localisation : une URL par localisation, jamais de
  fusion. Départements et régions sont couverts : le département a son slug
  propre (`d-33_gironde`, dérivé de l'autocomplete), la région est élargie à
  ses départements côté parser (`_resolvable_targets`) — jamais en liste de
  communes.

Les blobs HTML sont des reproductions minimales du balisage réel des cartes,
comme dans tests/unit/test_parser_laforet.py.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

from parsers.century21 import (
    BASE_URL,
    DESKTOP_UA,
    MAX_PAGES,
    Century21Parser,
    _dict_to_listing,
    _fetch_with_retries,
    _is_merged_slug,
    _location_ok,
    _merged_slug,
    _parse_cards,
    _parse_price,
    _passes_filters,
    _property_type_label,
    _region_segments,
    _resolvable_targets,
    _type_slugs,
    _url_filters,
)
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_listing,
    make_region_location,
    make_whole_city_location,
)

# ---------------------------------------------------------------------------
# Périmètres réutilisés
# ---------------------------------------------------------------------------

PARIS_1ER = make_city_location("Paris", "75001", "75101")
PARIS_19 = make_city_location("Paris", "75019", "75119")
MONTROUGE = make_city_location("Montrouge", "92120", "92049")
NANTES = make_city_location("Nantes", "44000", "44109")
PARIS_WHOLE = make_whole_city_location("Paris", ("75001", "75015"), "75056")
GIRONDE = make_department_location("33", "Gironde")
CORSE_DU_SUD = make_department_location("2A", "Corse-du-Sud")
IDF = make_region_location("11", "Île-de-France", ("75", "77", "78", "91", "92", "93", "94", "95"))


# ---------------------------------------------------------------------------
# Fabriques de HTML — reproductions du balisage réel des cartes Century 21
# ---------------------------------------------------------------------------

def card(
    uid: str = "15581564422",
    *,
    city: str = "PARIS",
    zip_code: str = "75019",
    surface: str = "48,66 m<sup>2</sup>",
    rooms: str = "2 pièces",
    type_title: str = "Appartement F2 à vendre",
    price: str = "340 000 €",
    description: str = "Une description de carte.",
    image: str = "/imagesBien/s3/x.jpg",
    href: str | None = None,
) -> str:
    """Une carte d'annonce au balisage de Century 21, paramétrable.

    Le titre affiché (`c-text-theme-heading-3`) est le type seul ; l'`aria-label`
    du lien porte le type ET la ville (c'est lui que le parser lit). Le
    `<sup>2</sup>` de la surface est aplaté en « m 2 » par le `get_text(" ", ...)`
    du parser : le regex surface tolère cet espace.
    """
    link = href if href is not None else f"/trouver_logement/detail/{uid}/"
    return (
        '<div class="c-the-property-thumbnail-with-content '
        f'js-the-property-thumbnail-with-content" data-uid="{uid}">'
        '<div class="c-the-property-thumbnail-with-content__col-left">'
        f'<a aria-label="{type_title} {city}" href="{link}">'
        f'<img src="{image}"/></a>'
        "</div>"
        '<div class="c-the-property-thumbnail-with-content__col-right">'
        "<h3>"
        f'<div class="c-text-theme-heading-4">{city} {zip_code} {surface}, {rooms} Ref : 21329</div>'
        f'<div class="c-text-theme-heading-3">{type_title}</div>'
        f'<div class="c-text-theme-heading-1">{price}</div>'
        "</h3>"
        f'<div class="c-text-theme-base">{description}</div>'
        "</div></div>"
    )


# Une carte de la section « Biens se rapprochant » : même uid, mais dans le
# conteneur related et avec la classe `c-the-property-thumbnail` SANS le suffixe
# `-with-content` que le sélecteur du parser exige.
RELATED_CARD = (
    '<div class="c-the-property-thumbnail js-the-property-thumbnail" data-uid="99999999999">'
    '<a href="/trouver_logement/detail/99999999999/"><img src="/imagesBien/related.jpg"/></a>'
    "</div>"
)

# Un bloc publicitaire : classe `c-the-ad`, aucun data-uid.
AD_BLOCK = '<div class="c-the-ad"><a href="/annonce-pub">Publicité</a></div>'


def page(*cards: str, related: str = "", ad: bool = False) -> str:
    """Une page de résultats : la liste des vrais résultats, éventuellement la
    section « Biens se rapprochant » et un bloc publicitaire, comme sur le vrai
    site."""
    list_html = (
        '<div class="c-the-list-of-properties-list js-the-list-of-properties-list">'
        + "".join(cards) + "</div>"
    )
    related_html = (
        f'<div class="c-the-list-of-properties-related js-the-list-of-properties-related">{related}</div>'
        if related
        else ""
    )
    ad_html = AD_BLOCK if ad else ""
    return f"<html><body>{list_html}{related_html}{ad_html}</body></html>"


EMPTY_PAGE_HTML = "<html><body></body></html>"


# ---------------------------------------------------------------------------
# Double de requests.Session (même forme que Laforêt)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error")


class FakeSession:
    """Double de `requests.Session` : enregistre les appels, rejoue des réponses.

    `pages` est consommée dans l'ordre et la DERNIÈRE réponse est ensuite
    répétée indéfiniment, ce qui laisse la pagination s'arrêter d'elle-même sur
    « aucune annonce inédite ». `handler(url)` prend le dessus pour décider
    réponse par réponse.
    """

    def __init__(self, pages=None, handler=None):
        self.headers: dict[str, str] = {}
        self.calls: list[dict] = []
        self._responses = [
            p if isinstance(p, FakeResponse) else FakeResponse(p)
            for p in (pages or [EMPTY_PAGE_HTML])
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

    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]


def run_scrape(criteria: dict, session: FakeSession, parser: Century21Parser | None = None):
    """Exécute `scrape()` en substituant `session` à la vraie requests.Session."""
    parser = parser or Century21Parser()
    with patch("requests.Session", return_value=session):
        return parser.scrape(criteria)


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


def manual_criteria(slug: str = "cp-75001", **overrides) -> dict:
    """Des critères portant un slug saisi à la main (aucun storage nécessaire).

    La localisation par défaut est le 19e (75019), le code postal de la carte
    par défaut : les tests de scrape retiennent ainsi les cartes sans avoir à
    re-préciser le périmètre."""
    criteria = {
        "locations": [PARIS_19],
        "transaction": "buy",
        "propertyTypes": ["apartment", "house"],
        "sourceOverrides": {"century21": {"slugs": [slug]}},
    }
    criteria.update(overrides)
    return criteria


# ---------------------------------------------------------------------------
# _parse_price
# ---------------------------------------------------------------------------

class TestParsePrice:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("340 000 €", 340000.0),
            # Loyer mensuel : « 820,30 € par mois charges comprises » (capture
            # location_nantes.html) -> le premier montant gagne, virgule décimale
            # comprise.
            ("820,30 € par mois charges comprises", 820.3),
            ("1 920 €", 1920.0),
            ("0 €", 0.0),
            # Aucun montant : pas de prix, pas d'exception.
            ("Prix : nous consulter", None),
            ("", None),
            # Le symbole sans chiffre : float("") lève, ValueError rattrapée.
            ("Nous consulter €", None),
            # Le symbole avant le montant n'est pas la convention française.
            ("€ 1021", None),
        ],
    )
    def test_prices(self, text, expected):
        assert _parse_price(text) == expected


# ---------------------------------------------------------------------------
# _property_type_label
# ---------------------------------------------------------------------------

class TestPropertyTypeLabel:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Appartement F2 à vendre PARIS", "Appartement"),
            ("Maison à vendre MONTROUGE", "Maison"),
            ("Studio à louer PARIS", "Studio"),
            ("Parking à vendre LYON", "Parking"),
            ("Terrain à vendre NANTES", "Terrain"),
            # Type inconnu : pas de type deviné.
            ("Loft à vendre PARIS", ""),
            ("", ""),
        ],
    )
    def test_the_type_is_read_in_the_title(self, title, expected):
        assert _property_type_label(title) == expected

    def test_a_maison_mention_inside_another_word_does_not_win(self):
        assert _property_type_label("Immeuble à vendre PARIS") == ""


# ---------------------------------------------------------------------------
# _parse_cards
# ---------------------------------------------------------------------------

class TestParseCards:
    def test_extracts_every_field_of_a_real_card(self):
        cards = _parse_cards(page(card()))

        assert len(cards) == 1
        assert cards[0]["uid"] == "15581564422"
        assert cards[0]["url"] == f"{BASE_URL}/trouver_logement/detail/15581564422/"
        assert cards[0]["title"] == "Appartement F2 à vendre PARIS"
        assert cards[0]["price_value"] == 340000.0
        assert cards[0]["price"].group(1).strip() == "340 000"
        assert cards[0]["city"] == "PARIS"
        assert cards[0]["zip_code"] == "75019"
        assert cards[0]["surface"] == "48,66"
        assert cards[0]["rooms"] == "2"
        assert cards[0]["description"] == "Une description de carte."
        assert cards[0]["image_url"] == f"{BASE_URL}/imagesBien/s3/x.jpg"
        assert cards[0]["property_type"] == "Appartement"

    def test_document_order_is_preserved(self):
        cards = _parse_cards(page(card("111"), card("222")))

        assert [c["uid"] for c in cards] == ["111", "222"]

    def test_the_related_section_cards_are_never_parsed(self):
        """🔒 La section « Biens se rapprochant » alimente la page avec des biens
        HORS périmètre (vérifié en direct : Paris 4e pour une recherche Paris
        1er). Ses cartes partagent le balisage ET un data-uid : seul le sélecteur
        restreint à `.c-the-list-of-properties-list` les écarte."""
        html = page(card("111", zip_code="75001"), related=RELATED_CARD)

        cards = _parse_cards(html)

        assert [c["uid"] for c in cards] == ["111"]

    def test_the_ad_blocks_are_never_parsed(self):
        """Les blocs publicitaires `c-the-ad` n'ont pas de data-uid : ils ne
        doivent pas devenir de fausses annonces."""
        html = page(card("111"), ad=True)

        assert [c["uid"] for c in _parse_cards(html)] == ["111"]

    def test_a_page_without_the_results_container_yields_nothing(self):
        assert _parse_cards(EMPTY_PAGE_HTML) == []
        assert _parse_cards("<html><body><div>rien</div></body></html>") == []

    def test_a_card_without_a_detail_link_is_skipped(self):
        html = page(
            '<div class="c-the-property-thumbnail-with-content" data-uid="42">'
            '<a href="/autre-page">pas un détail</a></div>'
        )

        assert _parse_cards(html) == []

    def test_a_card_without_an_aria_label_falls_back_to_the_heading(self):
        html = page(
            '<div class="c-the-property-thumbnail-with-content" data-uid="42">'
            '<div class="c-the-property-thumbnail-with-content__col-left">'
            '<a href="/trouver_logement/detail/42/"><img src="/x.jpg"/></a>'
            "</div>"
            '<div class="c-the-property-thumbnail-with-content__col-right">'
            '<div class="c-text-theme-heading-3">Titre du bien</div>'
            "</div></div>"
        )

        cards = _parse_cards(html)

        assert cards[0]["title"] == "Titre du bien"

    def test_a_card_missing_its_facts_still_parses(self):
        """Fail-open sur les données de la carte : mieux vaut une annonce
        incomplète (elle sera filtrée plus loin) qu'une exception qui ferait
        perdre toute la page."""
        html = page(
            '<div class="c-the-property-thumbnail-with-content" data-uid="7">'
            '<div class="c-the-property-thumbnail-with-content__col-left">'
            '<a href="/trouver_logement/detail/7/"><img src="/x.jpg"/></a>'
            "</div></div>"
        )

        parsed = _parse_cards(html)[0]

        assert parsed["uid"] == "7"
        assert parsed["city"] == ""
        assert parsed["zip_code"] == ""
        assert parsed["price_value"] is None
        assert parsed["surface"] == ""
        assert parsed["rooms"] == ""

    def test_the_description_is_truncated_to_300_characters(self):
        html = page(card("1", description="a" * 500))

        assert _parse_cards(html)[0]["description"] == "a" * 300

    @pytest.mark.parametrize(
        ("image", "expected"),
        [
            # src relatif -> préfixé par le domaine.
            ("/imagesBien/s3/x.jpg", f"{BASE_URL}/imagesBien/s3/x.jpg"),
            # Déjà absolu -> laissé tel quel.
            ("https://cdn.century21.fr/1.jpg", "https://cdn.century21.fr/1.jpg"),
            # Placeholder base64 : pas une photo d'annonce.
            ("data:image/gif;base64,R0lGODlh", ""),
            # Absent -> chaîne vide.
            (None, ""),
        ],
    )
    def test_image_url_normalisation(self, image, expected):
        src = f'<img src="{image}"/>' if image else "<img alt='rien'>"
        html = page(
            '<div class="c-the-property-thumbnail-with-content" data-uid="1">'
            '<div class="c-the-property-thumbnail-with-content__col-left">'
            f'<a href="/trouver_logement/detail/1/">{src}</a>'
            "</div></div>"
        )

        assert _parse_cards(html)[0]["image_url"] == expected

    @pytest.mark.parametrize(
        ("heading", "expected_city", "expected_zip"),
        [
            # Arrondissement de Paris : code postal COMPLET (capture
            # location_paris.html « PARIS 75004 »).
            ("PARIS 75004 44,87 m<sup>2</sup>, 2 pièces", "PARIS", "75004"),
            # Ville simple : CP TRONQUÉ au département (capture
            # achat_montrouge.html « MONTROUGE 92 », location_nantes.html
            # « NANTES 44 »).
            ("MONTROUGE 92 40,58 m<sup>2</sup>, 2 pièces", "MONTROUGE", "92"),
            ("NANTES 44 64,12 m<sup>2</sup>, 3 pièces", "NANTES", "44"),
        ],
    )
    def test_city_and_postal_code_extraction(self, heading, expected_city, expected_zip):
        html = page(card("1", city="", zip_code="").replace(
            "  , 1 pi", ""  # placeholder sans importance, remplacé ci-dessous
        ))
        # On reconstruit une carte avec le heading exact voulu.
        html = page(
            '<div class="c-the-property-thumbnail-with-content" data-uid="1">'
            '<div class="c-the-property-thumbnail-with-content__col-left">'
            '<a aria-label="Bien à vendre" href="/trouver_logement/detail/1/">'
            '<img src="/x.jpg"/></a>'
            "</div>"
            '<div class="c-the-property-thumbnail-with-content__col-right">'
            f'<div class="c-text-theme-heading-4">{heading}</div>'
            "</div></div>"
        )

        parsed = _parse_cards(html)[0]
        assert parsed["city"] == expected_city
        assert parsed["zip_code"] == expected_zip

    def test_the_sup_surface_space_is_tolerated(self):
        """Le get_text aplatit `<sup>2</sup>` en « m 2 » : le regex surface doit
        tolérer cet espace (capture réelle « 52,11 m 2 »)."""
        cards = _parse_cards(page(card("1", surface="52,11 m<sup>2</sup>")))

        assert cards[0]["surface"] == "52,11"


# ---------------------------------------------------------------------------
# _location_ok
# ---------------------------------------------------------------------------

class TestLocationOk:
    @pytest.mark.parametrize(
        ("zip_code", "locations", "expected"),
        [
            # Code complet : comparaison stricte via matches_locations.
            ("75019", [PARIS_19], True),
            ("75019", [PARIS_1ER], False),
            # Code tronqué au département : accepté si un préfixe de périmètre
            # commence par ce code (92120 startswith « 92 »).
            ("92", [MONTROUGE], True),
            ("44", [NANTES], True),
            ("92", [PARIS_1ER], False),
            # Tronquage AU-DELÀ du département (Corse, vérifié en direct) :
            # le site affiche « 201 » alors que le préfixe du département 2A
            # est « 20 » — la compatibilité de préfixe marche dans les deux
            # sens. Limite assumée de la granularité : « 202 » (Haute-Corse)
            # partage le même préfixe « 20 » et passe aussi ; en pratique le
            # site ne mélange pas les départements sur une page.
            ("201", [CORSE_DU_SUD], True),
            # Recherche multi-périmètres : n'importe lequel suffit.
            ("92", [PARIS_1ER, MONTROUGE], True),
            # Code postal illisible : échec FERMÉ — c'est ce qui tient la section
            # « Biens se rapprochant » hors périmètre.
            ("", [MONTROUGE], False),
            (None, [MONTROUGE], False),
            # Aucun périmètre : rien ne peut correspondre.
            ("75019", [], False),
        ],
    )
    def test_complete_and_truncated_postal_codes(self, zip_code, locations, expected):
        assert _location_ok(zip_code, locations) is expected

    def test_a_whole_city_covers_its_postal_codes(self):
        assert _location_ok("75015", [PARIS_WHOLE]) is True
        assert _location_ok("75019", [PARIS_WHOLE]) is False


# ---------------------------------------------------------------------------
# _passes_filters
# ---------------------------------------------------------------------------

class TestPassesFilters:
    def test_a_rejected_location_short_circuits_every_other_filter(self):
        listing = make_listing(zip_code="75004", price_value=1000.0, surface="50", rooms="2")
        criteria = {"priceMin": 0, "priceMax": 10000}

        assert _passes_filters(listing, criteria, [PARIS_1ER]) is False

    @pytest.mark.parametrize(
        ("price_value", "criteria", "expected"),
        [
            (1000.0, {"priceMin": 900, "priceMax": 1100}, True),
            (1000.0, {"priceMax": 900}, False),
            (1000.0, {"priceMin": 1100}, False),
            # Bornes inclusives.
            (1000.0, {"priceMin": 1000, "priceMax": 1000}, True),
            # FAIL-OPEN : un prix illisible ne fait pas écarter l'annonce.
            (None, {"priceMax": 1}, True),
        ],
    )
    def test_price_bounds(self, price_value, criteria, expected):
        listing = make_listing(zip_code="75019", price_value=price_value)
        assert _passes_filters(listing, criteria, [PARIS_19]) is expected

    @pytest.mark.parametrize(
        ("surface", "criteria", "expected"),
        [
            ("50", {"surfaceMin": 40, "surfaceMax": 60}, True),
            ("50", {"surfaceMax": 40}, False),
            # La virgule décimale du site est convertie avant comparaison.
            ("50,5", {"surfaceMin": 51}, False),
            ("50,5", {"surfaceMin": 50}, True),
            ("", {"surfaceMin": 1000}, True),
        ],
    )
    def test_surface_bounds(self, surface, criteria, expected):
        listing = make_listing(zip_code="75019", surface=surface)
        assert _passes_filters(listing, criteria, [PARIS_19]) is expected

    @pytest.mark.parametrize(
        ("rooms", "criteria", "expected"),
        [
            ("2", {"rooms": [2, 3]}, True),
            ("3", {"rooms": [2]}, False),
            # « 5 » signifie « 5 et plus » dans le vocabulaire partagé.
            ("6", {"rooms": [5]}, True),
            ("4", {"rooms": [5]}, False),
            ("", {"rooms": [2]}, True),
        ],
    )
    def test_room_counts(self, rooms, criteria, expected):
        listing = make_listing(zip_code="75019", rooms=rooms)
        assert _passes_filters(listing, criteria, [PARIS_19]) is expected


# ---------------------------------------------------------------------------
# _dict_to_listing
# ---------------------------------------------------------------------------

class TestDictToListing:
    def test_maps_a_card_to_the_common_listing_schema(self):
        listing = _dict_to_listing(_parse_cards(page(card()))[0])

        assert listing.listing_id == "c21_15581564422"
        assert listing.legacy_id == "15581564422"
        assert listing.source == "century21"
        assert listing.title == "Appartement F2 à vendre PARIS"
        assert listing.price == "340 000 €"
        assert listing.price_value == 340000.0
        assert listing.surface == "48,66"
        assert listing.rooms == "2"
        assert listing.city == "PARIS"
        assert listing.location == "PARIS"
        assert listing.zip_code == "75019"
        assert listing.property_type == "Appartement"
        assert listing.agency == ""

    def test_the_identifier_is_prefixed_by_the_source(self):
        listing = _dict_to_listing(_parse_cards(page(card("42")))[0])
        assert listing.listing_id == "c21_42"

    def test_an_unreadable_price_gives_an_empty_display_string(self):
        listing = _dict_to_listing(_parse_cards(page(card("1", price="Prix sur demande")))[0])
        assert listing.price == ""
        assert listing.price_value is None


# ---------------------------------------------------------------------------
# _type_slugs
# ---------------------------------------------------------------------------

class TestTypeSlugs:
    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            (None, []),
            ([], []),
            (["apartment"], ["appartement"]),
            (["house"], ["maison"]),
            (["parking"], ["parking"]),
            (["land"], ["terrain"]),
            # L'ordre demandé est conservé (il décide du chemin).
            (["apartment", "house"], ["appartement", "maison"]),
            # Les types hors capacités sont retirés SANS exception.
            (["apartment", "yacht"], ["appartement"]),
            (["yacht"], []),
        ],
    )
    def test_supported_types_only(self, requested, expected):
        assert _type_slugs({"propertyTypes": requested}) == expected


# ---------------------------------------------------------------------------
# _base_path
# ---------------------------------------------------------------------------

class TestBasePath:
    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            # Aucun type -> recherche « f » sans segment de type.
            ({"transaction": "buy", "propertyTypes": []}, f"{BASE_URL}/annonces/f/achat/v-paris/"),
            # Un seul type -> SANS le « f » (un seul type avec « f » renvoie 410,
            # vérifié en direct).
            ({"transaction": "buy", "propertyTypes": ["apartment"]}, f"{BASE_URL}/annonces/achat-appartement/v-paris/"),
            # Deux types et plus -> recherche « f » avec les types joints.
            (
                {"transaction": "buy", "propertyTypes": ["apartment", "house"]},
                f"{BASE_URL}/annonces/f/achat-appartement-maison/v-paris/",
            ),
            # Transaction par défaut : la location (comme Laforêt).
            ({}, f"{BASE_URL}/annonces/f/location/v-paris/"),
            # L'ordre demandé décide du segment.
            (
                {"propertyTypes": ["house", "apartment"]},
                f"{BASE_URL}/annonces/f/location-maison-appartement/v-paris/",
            ),
        ],
    )
    def test_the_path_depends_on_the_number_of_types(self, criteria, expected):
        assert Century21Parser()._base_path(criteria, "v-paris") == expected

    @pytest.mark.parametrize(
        ("types", "expected"),
        [
            # Un seul type mais un slug FUSIONNÉ : la forme « f » est
            # obligatoire (vérifié en direct : sans elle, 410).
            (
                ["apartment"],
                f"{BASE_URL}/annonces/f/location-appartement/d-91_essonne-92_hauts_de_seine/",
            ),
            ([], f"{BASE_URL}/annonces/f/location/d-91_essonne-92_hauts_de_seine/"),
            (
                ["apartment", "house"],
                f"{BASE_URL}/annonces/f/location-appartement-maison/d-91_essonne-92_hauts_de_seine/",
            ),
        ],
        ids=["un_type", "aucun_type", "deux_types"],
    )
    def test_a_merged_slug_always_takes_the_f_form(self, types, expected):
        criteria = {"transaction": "rent", "propertyTypes": types}

        assert (
            Century21Parser()._base_path(criteria, "d-91_essonne-92_hauts_de_seine") == expected
        )

    def test_url_filters_force_the_f_form_even_with_a_single_type(self):
        """Un seul type + filtres : la forme sans « f » renvoie 410 en direct
        (/annonces/location-appartement/v-paris/p-2/) — les segments de
        filtres basculent donc le chemin en forme « f », après la
        localisation."""
        criteria = {
            "transaction": "rent",
            "propertyTypes": ["apartment"],
            "surfaceMin": 19,
            "surfaceMax": 98,
            "priceMax": 1850,
            "rooms": [2],
        }

        assert Century21Parser()._base_path(criteria, "v-paris") == (
            f"{BASE_URL}/annonces/f/location-appartement/v-paris/s-19-98/st-0-/b-0-1850/p-2/"
        )


# ---------------------------------------------------------------------------
# _url_filters — les segments prix/surface/pièces (grammaire vérifiée en direct)
# ---------------------------------------------------------------------------

class TestUrlFilters:
    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            ({}, []),
            # Le trio s/st/b est atomique et ordonné : un seul filtre du trio
            # émet les trois, avec des neutres (`st-0-`, bornes vides).
            ({"surfaceMin": 30}, ["s-30-", "st-0-", "b-0-"]),
            ({"surfaceMax": 50}, ["s-0-50", "st-0-", "b-0-"]),
            ({"priceMax": 1850}, ["s-0-", "st-0-", "b-0-1850"]),
            ({"surfaceMin": 19, "surfaceMax": 98, "priceMax": 1850}, ["s-19-98", "st-0-", "b-0-1850"]),
            # p-N : exactement n pièces, un seul segment.
            ({"rooms": [2]}, ["p-2"]),
            ({"rooms": [3]}, ["p-3"]),
        ],
        ids=[
            "rien", "surface_min", "surface_max", "prix_max",
            "trio_complet", "pieces_2", "pieces_3",
        ],
    )
    def test_the_segments_follow_the_site_s_own_grammar(self, criteria, expected):
        assert _url_filters(criteria) == expected

    @pytest.mark.parametrize(
        ("rooms", "case"),
        [
            ([2, 3], "plusieurs valeurs : un seul segment p autorisé par URL"),
            ([5], "le « 5 » canonique signifie « 5 et plus », p-5 serait exact"),
            ([], "pas de pièces demandées"),
        ],
        ids=["multi_valeurs", "cinq_et_plus", "vide"],
    )
    def test_inexpressible_room_filters_are_left_to_the_scraper(self, rooms, case):
        """Le minimum de prix canonique (sans max) n'a pas non plus d'équivalent
        URL : le budget ne porte que le maximum, le reste est appliqué côté
        scraper par _passes_filters."""
        assert _url_filters({"rooms": rooms}) == [], case
        assert _url_filters({"priceMin": 800}) == []


# ---------------------------------------------------------------------------
# _merged_slug / _is_merged_slug
# ---------------------------------------------------------------------------

class TestMergedSlug:
    def test_first_slug_keeps_its_prefix_the_others_are_stripped(self):
        """Format vérifié en direct : `v-paris` + `d-91_essonne` +
        `d-92_hauts_de_seine` -> segment unique où seul le premier slug garde
        son préfixe de niveau."""
        assert _merged_slug(["d-77_seine_et_marne", "d-78_yvelines", "d-95_val_d_oise"]) == (
            "d-77_seine_et_marne-78_yvelines-95_val_d_oise"
        )
        assert _merged_slug(["cp-75001", "cp-75002"]) == "cp-75001-75002"

    def test_a_single_slug_is_returned_as_is(self):
        assert _merged_slug(["d-33_gironde"]) == "d-33_gironde"

    @pytest.mark.parametrize(
        ("slug", "expected"),
        [
            # Un nom de lieu ne contient jamais de tiret interne (normalisé en
            # underscores ou « + ») : un tiret après le préfixe = fusion.
            ("d-33_gironde", False),
            ("v-st+etienne", False),
            ("cpv-69003_villeurbanne", False),
            ("d-91_essonne-92_hauts_de_seine", True),
            ("cp-75001-cp-75002", True),
        ],
        ids=["dept", "ville_abregee", "cpv", "fusion_depts", "fusion_cp"],
    )
    def test_merged_detection(self, slug, expected):
        assert _is_merged_slug(slug) is expected


class TestRegionSegments:
    def test_departments_merge_into_one_segment(self):
        """Le cas Corse : deux départements, aucun repli ville — un seul
        segment où seul le premier slug garde son préfixe."""
        assert _region_segments(["d-201_corse_du_sud", "d-202_haute_corse"]) == (
            "d-201_corse_du_sud-202_haute_corse"
        )

    def test_a_city_fallback_gets_its_own_segment_before_the_departments(self):
        """Paris n'a pas d'entrée départementale : son repli `v-paris` vit
        dans un segment séparé. Tout mettre à plat dans un même segment
        (`v-paris-77_seine_et_marne-...`) renvoie 410, vérifié en direct."""
        slugs = ["v-paris", "d-77_seine_et_marne", "d-78_yvelines"]

        assert _region_segments(slugs) == "v-paris/d-77_seine_et_marne-78_yvelines"

    def test_only_cities(self):
        assert _region_segments(["v-paris"]) == "v-paris"


# ---------------------------------------------------------------------------
# build_search_urls
# ---------------------------------------------------------------------------

class TestBuildSearchUrls:
    def test_one_url_per_location_never_merged(self):
        """Hors région, chaque localisation garde SA propre URL : la fusion en
        un segment unique est réservée aux départements d'une même région
        (_slugs), pas aux villes choisies séparément — le formulaire du site
        ne propose qu'un seul champ de ville (vérifié en direct)."""
        criteria = {
            "locations": [PARIS_1ER, MONTROUGE],
            "transaction": "buy",
            "propertyTypes": ["apartment", "house"],
            "sourceOverrides": {"century21": {"slugs": ["cp-75001", "v-montrouge"]}},
        }

        urls = Century21Parser().build_search_urls(criteria)

        assert urls == [
            f"{BASE_URL}/annonces/f/achat-appartement-maison/cp-75001/",
            f"{BASE_URL}/annonces/f/achat-appartement-maison/v-montrouge/",
        ]

    @pytest.mark.parametrize(
        ("types", "expected"),
        [
            (["apartment", "house"], f"{BASE_URL}/annonces/f/achat-appartement-maison/v-montrouge/"),
            (["apartment"], f"{BASE_URL}/annonces/achat-appartement/v-montrouge/"),
            ([], f"{BASE_URL}/annonces/f/achat/v-montrouge/"),
        ],
        ids=["deux_types", "un_type", "aucun_type"],
    )
    def test_the_number_of_types_decides_the_url_format(self, types, expected):
        criteria = manual_criteria("v-montrouge", locations=[MONTROUGE], propertyTypes=types)

        assert Century21Parser().build_search_urls(criteria) == [expected]

    def test_build_search_url_returns_the_first_url(self):
        parser = Century21Parser()
        urls = parser.build_search_urls(manual_criteria("cp-75001"))
        assert parser.build_search_url(manual_criteria("cp-75001")) == urls[0]

    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}, {"locations": [{"kind": "city", "city": "Paris"}]}],
        ids=["vide", "liste_vide", "localisation_incomplete"],
    )
    def test_no_url_without_a_location(self, criteria):
        assert Century21Parser().build_search_urls(criteria) == []
        assert Century21Parser().build_search_url(criteria) is None


# ---------------------------------------------------------------------------
# _slugs
# ---------------------------------------------------------------------------

class TestSlugs:
    def test_a_manual_slug_short_circuits_every_resolution(self, logged):
        from tests.helpers.fakes import fake_storage

        parser = Century21Parser(storage=fake_storage())
        criteria = manual_criteria("cp-75001")

        with patch(
            "services.century21_geocode.resolve_slug_id"
        ) as mock_resolve:
            slugs = parser._slugs(criteria, criteria["locations"])

        assert slugs == [(PARIS_19, "cp-75001")]
        mock_resolve.assert_not_called()

    def test_manual_slugs_are_honoured_for_every_perimeter_level(self, logged):
        """Avec des slugs collés à la main, AUCUN niveau n'est filtré :
        `zip(strict=False)` épouse la liste des slugs, département et région
        compris — c'est l'utilisateur qui a collé ces slugs, on les honore. Le
        filtrage des niveaux non couverts ne s'applique qu'à la résolution
        automatique (test suivant)."""
        parser = Century21Parser()
        locations = [PARIS_1ER, GIRONDE, IDF]

        slugs = parser._slugs({"sourceOverrides": {"century21": {"slugs": ["cp-75001", "x", "y"]}}}, locations)

        assert [loc["kind"] for loc, _ in slugs] == ["city", "department", "region"]

    def test_without_storage_the_impossibility_is_logged(self, logged):
        parser = Century21Parser()

        assert parser._slugs({"locations": [PARIS_1ER]}, [PARIS_1ER]) == []
        assert any(
            level == "WARNING" and "Aucun storage fourni au parser" in message
            for level, message in logged
        )

    def test_a_department_resolves_like_any_other_perimeter(self, logged):
        """Le département a son propre niveau de couverture : il est résolu
        directement (slug complet dérivé de l'autocomplete), pas écarté."""
        from tests.helpers.fakes import fake_storage

        parser = Century21Parser(storage=fake_storage())
        with patch(
            "services.century21_geocode.resolve_slug_id",
            side_effect=lambda loc, repo: f"d-{loc['code']}_slug" if loc["kind"] == "department" else "v-paris",
        ):
            slugs = parser._slugs({"locations": [GIRONDE, PARIS_1ER]}, [GIRONDE, PARIS_1ER])

        assert slugs == [
            (GIRONDE, "d-33_slug"),
            (PARIS_1ER, "v-paris"),
        ]
        assert not any(level == "WARNING" for level, _ in logged)

    def test_a_region_is_merged_into_one_resolved_target(self, logged):
        """Une région n'a pas d'identifiant Century 21 : élargie à SES
        départements, chaque slug est résolu individuellement (et caché sous
        sa clé propre), puis FUSIONNÉ en un seul segment (_merged_slug) — la
        région se scrape en UNE série de pages, pas une par département."""
        from tests.helpers.fakes import fake_storage

        parser = Century21Parser(storage=fake_storage())
        with patch(
            "services.century21_geocode.resolve_slug_id",
            side_effect=lambda loc, repo: f"d-{loc['code']}_nom",
        ) as mock_resolve:
            slugs = parser._slugs({"locations": [IDF]}, [IDF])

        expected = "d-75_nom-" + "-".join(f"{c}_nom" for c in IDF["departments"][1:])
        assert slugs == [(IDF, expected)]
        assert mock_resolve.call_count == len(IDF["departments"])
        assert not any(level == "WARNING" for level, _ in logged)

    def test_a_region_with_partial_resolution_merges_what_resolved(self, logged):
        """Un département dont le slug ne se résout pas n'écarte pas la région :
        les résolus sont fusionnés, l'échec est tracé en warning."""
        from tests.helpers.fakes import fake_storage

        parser = Century21Parser(storage=fake_storage())

        def flaky(loc, repo):
            if loc["code"] == "77":
                return None
            return f"d-{loc['code']}_nom"

        with patch("services.century21_geocode.resolve_slug_id", side_effect=flaky):
            slugs = parser._slugs({"locations": [IDF]}, [IDF])

        expected = "d-75_nom-" + "-".join(
            f"{c}_nom" for c in IDF["departments"][1:] if c != "77"
        )
        assert slugs == [(IDF, expected)]
        assert any(level == "WARNING" and "Aucun slug résolu" in m for level, m in logged)

    def test_the_expansion_falls_back_to_the_geo_api_without_a_departments_list(self):
        """Une localisation région sans liste de départements embarquée
        (critères anciens ?) interroge l'API geo avec son code — jamais une
        liste de communes."""
        with patch("core.geocode.region_departments", return_value=["33", "40"]) as mock_api:
            targets = _resolvable_targets({"kind": "region", "code": "75"})

        assert targets == [{"kind": "department", "code": "33"}, {"kind": "department", "code": "40"}]
        mock_api.assert_called_once_with("75")

    def test_a_city_or_whole_city_or_department_never_gets_expanded(self):
        """L'élargissement ne concerne QUE la région : les autres niveaux sont
        leurs propres cibles, tels quels."""
        for location in (PARIS_1ER, PARIS_WHOLE, GIRONDE):
            assert _resolvable_targets(location) == [location]

    def test_a_region_without_identifiable_departments_is_skipped_with_a_warning(self, logged):
        """Ni liste embarquée ni code exploitable : aucun département à
        résoudre, la localisation est écartée avec un avertissement — jamais
        développée en liste de communes."""
        from tests.helpers.fakes import fake_storage

        parser = Century21Parser(storage=fake_storage())
        region_nue = {"kind": "region", "name": "Corse"}
        with (
            patch("core.geocode.region_departments", return_value=[]),
            patch("services.century21_geocode.resolve_slug_id") as mock_resolve,
        ):
            slugs = parser._slugs({"locations": [region_nue]}, [region_nue])

        assert slugs == []
        mock_resolve.assert_not_called()
        assert any(
            level == "WARNING" and "sans départements identifiables" in message
            for level, message in logged
        )

    def test_an_unresolved_location_is_omitted_with_a_warning(self, logged):
        from tests.helpers.fakes import fake_storage

        parser = Century21Parser(storage=fake_storage())
        with patch(
            "services.century21_geocode.resolve_slug_id", return_value=None
        ):
            slugs = parser._slugs({"locations": [PARIS_1ER]}, [PARIS_1ER])

        assert slugs == []
        assert any(level == "WARNING" and "Aucun slug résolu" in message for level, message in logged)

    def test_the_repo_comes_from_the_injected_storage(self):
        from tests.helpers.fakes import fake_storage

        storage = fake_storage()
        assert Century21Parser(storage=storage)._geo_repo() is storage.century21_geo
        assert Century21Parser()._geo_repo() is None


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
            ("v-montrouge", {"slugs": ["v-montrouge"]}),
            ("  v-montrouge  ", {"slugs": ["v-montrouge"]}),
            ("v-paris, cp-75001", {"slugs": ["v-paris", "cp-75001"]}),
            ("v-paris ,cp-75001", {"slugs": ["v-paris", "cp-75001"]}),
            ("v-paris,,   ,cp-75001", {"slugs": ["v-paris", "cp-75001"]}),
            ("v-paris,", {"slugs": ["v-paris"]}),
            (",", {}),
            (" , , ", {}),
        ],
    )
    def test_comma_separated_slugs(self, value, expected):
        assert Century21Parser().parse_manual_override(value) == expected


class TestRememberManualOverride:
    def test_banks_the_slugs_against_the_injected_repo(self):
        from tests.helpers.fakes import fake_storage

        storage = fake_storage()
        parser = Century21Parser(storage=storage)
        criteria = manual_criteria("v-montrouge")

        with patch("services.century21_geocode.remember_manual_slugs") as mock_remember:
            assert parser.remember_manual_override(criteria) is None

        mock_remember.assert_called_once_with(criteria, repo=storage.century21_geo)

    @pytest.mark.parametrize("storage", [None, object()], ids=["sans_storage", "storage_sans_repo_geo"])
    def test_without_a_repo_nothing_is_attempted(self, storage):
        parser = Century21Parser(storage=storage)
        with patch("services.century21_geocode.remember_manual_slugs") as mock_remember:
            assert parser.remember_manual_override(manual_criteria("v-paris")) is None
        mock_remember.assert_not_called()

    def test_a_failure_is_logged_but_never_raised(self, logged):
        from tests.helpers.fakes import fake_storage

        parser = Century21Parser(storage=fake_storage())

        with patch(
            "services.century21_geocode.remember_manual_slugs",
            side_effect=RuntimeError("banque indisponible"),
        ):
            assert parser.remember_manual_override(manual_criteria("v-paris")) is None

        assert any(
            level == "DEBUG" and "non mémorisé" in message and "banque indisponible" in message
            for level, message in logged
        )


# ---------------------------------------------------------------------------
# has_valid_criteria / cannot_search_reason
# ---------------------------------------------------------------------------

class TestHasValidCriteria:
    def test_a_manual_slug_is_always_valid(self):
        assert Century21Parser().has_valid_criteria({"sourceOverrides": {"century21": {"slugs": ["v-paris"]}}}) is True

    @pytest.mark.parametrize(
        "criteria",
        [
            {"locations": [PARIS_1ER]},
            {"locations": [PARIS_WHOLE]},
            # Ville tapée à la main : area_cache_key retombe sur le code postal.
            {"locations": [{"kind": "city", "city": "Montrouge", "postalCode": "92120"}]},
            # Ville entière sans code INSEE : area_cache_key retombe sur le nom.
            {"locations": [{"kind": "whole_city", "city": "Paris", "postalCodes": ["75001"]}]},
            # Département : clé propre dept:{code}.
            {"locations": [GIRONDE]},
            # Région : elle porte ses départements, l'élargissement saura
            # produire des cibles.
            {"locations": [IDF]},
        ],
        ids=[
            "commune", "ville_entiere", "ville_sans_insee", "ville_entiere_sans_insee",
            "departement", "region",
        ],
    )
    def test_a_perimeter_with_a_cache_key_is_valid(self, criteria):
        assert Century21Parser().has_valid_criteria(criteria) is True

    @pytest.mark.parametrize(
        "criteria",
        [
            {"locations": [{"kind": "city", "city": "Paris"}]},
            {},
            {"locations": []},
        ],
        ids=["ville_sans_code", "vide", "liste_vide"],
    )
    def test_everything_else_is_invalid(self, criteria):
        assert Century21Parser().has_valid_criteria(criteria) is False

    def test_validation_never_resolves_anything(self):
        with patch(
            "services.century21_geocode._resolve_uncached"
        ) as mock_resolve:
            assert Century21Parser().has_valid_criteria({"locations": [PARIS_1ER]}) is True
        mock_resolve.assert_not_called()


class TestCannotSearchReason:
    def test_none_when_usable(self):
        assert Century21Parser().cannot_search_reason(manual_criteria("cp-75001")) is None

    def test_no_location_at_all(self):
        assert Century21Parser().cannot_search_reason({}) == (
            "aucune localisation exploitable (ville + code postal requis)"
        )

    def test_a_department_is_a_usable_location(self):
        """Le niveau département est couvert (slug complet dérivé de
        l'autocomplete) : aucune raison de refuser la recherche."""
        assert Century21Parser().cannot_search_reason({"locations": [GIRONDE]}) is None

    def test_century21_covers_every_property_type_and_transaction(self):
        criteria = manual_criteria(
            "cp-75001",
            transaction="buy",
            propertyTypes=["apartment", "house", "parking", "land"],
        )
        assert Century21Parser().cannot_search_reason(criteria) is None
        assert Century21Parser().unsupported_criteria(criteria) == []


# ---------------------------------------------------------------------------
# _fetch_with_retries
# ---------------------------------------------------------------------------

class TestFetchWithRetries:
    def test_a_200_returns_the_body(self):
        session = FakeSession(["<html>ok</html>"])

        assert _fetch_with_retries(session, "https://www.century21.fr/annonces/f/achat/v-paris/") == "<html>ok</html>"

    def test_a_404_is_a_definitive_bad_location(self):
        session = FakeSession([FakeResponse("", status_code=404)])

        with pytest.raises(ValueError, match="localisation invalide"):
            _fetch_with_retries(session, "https://www.century21.fr/annonces/f/achat/v-nawak/")

        # Aucune retentative : le 404 est définitif.
        assert len(session.calls) == 1

    def test_a_410_is_retried_with_backoff_then_returns(self, slept):
        """🔒 Century 21 renvoie un HTTP 410 temporaire (quota anti-bot) sur les
        requêtes répétées : le 410 est retenté avec un backoff exponentiel, au
        contraire du 404."""
        session = FakeSession([
            FakeResponse("", status_code=410),
            FakeResponse("", status_code=410),
            FakeResponse("<html>enfin</html>"),
        ])

        assert _fetch_with_retries(session, "https://www.century21.fr/x/") == "<html>enfin</html>"
        assert len(session.calls) == 3
        assert slept == [2.0, 4.0]

    def test_a_410_on_every_attempt_raises_with_the_quota_reason(self, slept):
        session = FakeSession([FakeResponse("", status_code=410)])

        with pytest.raises(ValueError, match=r"page inaccessible après 3 tentatives .*HTTP 410"):
            _fetch_with_retries(session, "https://www.century21.fr/x/")

        assert len(session.calls) == 3
        assert slept == [2.0, 4.0]

    def test_a_network_error_is_retried(self, slept):
        def handler(url):
            if len(session.calls) < 3:
                raise requests.ConnectionError("réseau coupé")
            return FakeResponse("<html>ok</html>")

        session = FakeSession(handler=handler)

        assert _fetch_with_retries(session, "https://www.century21.fr/x/") == "<html>ok</html>"
        assert slept == [2.0, 4.0]

    def test_a_500_is_raised_by_raise_for_status(self):
        session = FakeSession([FakeResponse("", status_code=500)])

        with pytest.raises(requests.HTTPError, match="500 Server Error"):
            _fetch_with_retries(session, "https://www.century21.fr/x/")

        assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# scrape : garde-fous d'entrée et session
# ---------------------------------------------------------------------------

class TestScrapeGuards:
    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}, {"locations": [{"kind": "city", "city": "Paris"}]}],
        ids=["vide", "liste_vide", "localisation_incomplete"],
    )
    def test_no_location_raises_before_any_request(self, criteria):
        with pytest.raises(ValueError, match="nécessite au moins une localisation"):
            Century21Parser().scrape(criteria)

    def test_no_resolvable_location_raises(self):
        """Un département seul n'est pas une localisation Century 21 : le scrape
        doit le dire, pas chercher tout le département."""
        with pytest.raises(ValueError, match="ne référence que les villes"):
            Century21Parser().scrape({"locations": [GIRONDE]})

    def test_the_session_announces_a_desktop_browser(self):
        session = FakeSession([page(card("111"))])
        run_scrape(manual_criteria("cp-75001"), session)

        assert session.headers["User-Agent"] == DESKTOP_UA
        assert session.headers["Accept"] == "text/html, application/xhtml+xml"
        assert session.headers["Referer"] == "https://www.century21.fr/"

    def test_every_request_is_bounded_by_a_timeout(self):
        session = FakeSession([page(card("111"))])
        run_scrape(manual_criteria("cp-75001"), session)

        assert session.calls
        assert all(call["timeout"] == 15 for call in session.calls)


# ---------------------------------------------------------------------------
# scrape : pagination (_collect_pages)
# ---------------------------------------------------------------------------

class TestPagination:
    def test_it_stops_on_the_first_page_without_a_new_listing(self):
        session = FakeSession([page(card("111"))])
        listings = run_scrape(manual_criteria("cp-75001"), session)

        assert [li.listing_id for li in listings] == ["c21_111"]
        # Page 1 puis page 2 qui ne répète rien de neuf.
        assert len(session.calls) == 2

    def test_pagination_continues_while_new_listings_appear(self):
        session = FakeSession([
            page(card("111"), card("222")),
            page(card("222"), card("333")),
            page(card("333")),
        ])
        listings = run_scrape(manual_criteria("cp-75001"), session)

        assert [li.legacy_id for li in listings] == ["111", "222", "333"]
        assert len(session.calls) == 3

    def test_the_page_url_suffix_starts_at_page_two(self):
        session = FakeSession([page(card("111")), page(card("222")), page(card("222"))])
        run_scrape(manual_criteria("cp-75001"), session)

        base = f"{BASE_URL}/annonces/f/achat-appartement-maison/cp-75001/"
        assert session.urls == [base, f"{base}page-2/", f"{base}page-3/"]

    def test_an_empty_first_page_stops_immediately(self):
        session = FakeSession([EMPTY_PAGE_HTML])
        assert run_scrape(manual_criteria("cp-75001"), session) == []
        assert len(session.calls) == 1

    def test_the_page_limit_is_enforced_and_reported(self, logged):
        session = FakeSession([page(card(str(i))) for i in range(1, MAX_PAGES + 1)])
        listings = run_scrape(manual_criteria("cp-75001"), session)

        assert len(session.calls) == MAX_PAGES
        assert len(listings) == MAX_PAGES
        assert any(
            level == "WARNING" and f"limite de {MAX_PAGES} pages atteinte" in message
            for level, message in logged
        )

    def test_duplicates_across_pages_are_kept_once(self):
        session = FakeSession([page(card("111")), page(card("111"), card("222")), page(card("222"))])
        listings = run_scrape(manual_criteria("cp-75001"), session)

        assert [li.listing_id for li in listings] == ["c21_111", "c21_222"]


# ---------------------------------------------------------------------------
# scrape : filtrage et dégradation partielle
# ---------------------------------------------------------------------------

class TestScrapeFilters:
    def test_out_of_price_listings_are_dropped(self):
        session = FakeSession([
            page(card("111", price="340 000 €"), card("222", price="690 000 €")),
            page(card("222", price="690 000 €")),
        ])
        criteria = manual_criteria("cp-75001", priceMax=500000)

        listings = run_scrape(criteria, session)

        assert [li.legacy_id for li in listings] == ["111"]

    def test_out_of_area_listings_are_dropped(self):
        """Une carte hors périmètre (ex. la section « Biens se rapprochant » est
        déjà exclue du parse ; ici on vérifie le filtre aval sur une carte lue
        dans la liste) ne doit pas ressortir."""
        session = FakeSession([
            page(card("111", zip_code="75001"), card("222", zip_code="75004")),
            page(card("222", zip_code="75004")),
        ])
        criteria = manual_criteria("cp-75001", locations=[PARIS_1ER])

        listings = run_scrape(criteria, session)

        assert [li.legacy_id for li in listings] == ["111"]


class TestScrapePartialFailures:
    def test_one_bad_location_never_discards_the_others(self, logged):
        def handler(url):
            if "v-nawak" in url:
                return FakeResponse("", status_code=404)
            return FakeResponse(page(card("111", zip_code="75001")))

        session = FakeSession(handler=handler)
        criteria = {
            "locations": [PARIS_1ER, make_city_location("Nawak", "99999", "99999")],
            "sourceOverrides": {"century21": {"slugs": ["cp-75001", "v-nawak"]}},
        }

        listings = run_scrape(criteria, session)

        assert [li.legacy_id for li in listings] == ["111"]
        assert any(level == "WARNING" and "Nawak (99999)" in message for level, message in logged)

    def test_it_raises_only_when_every_attempt_fails(self):
        def handler(url):
            return FakeResponse("", status_code=404)

        session = FakeSession(handler=handler)
        criteria = {
            "locations": [
                make_city_location("Nawak", "99999", "99999"),
                make_city_location("Bidule", "88888", "88888"),
            ],
            "sourceOverrides": {"century21": {"slugs": ["v-nawak", "v-bidule"]}},
        }

        with pytest.raises(ValueError, match=r"Nawak \(99999\).*Bidule \(88888\)"):
            run_scrape(criteria, session)


# ---------------------------------------------------------------------------
# to_native : rien à traduire
# ---------------------------------------------------------------------------

class TestToNative:
    def test_the_canonical_criteria_are_used_as_is(self):
        criteria = manual_criteria("cp-75001")
        assert Century21Parser().to_native(criteria) is criteria
