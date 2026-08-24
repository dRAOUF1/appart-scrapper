r"""Tests unitaires de parsers/pap.py.

PAP rend ses pages de résultats côté serveur : le module est fait de fonctions
PURES (parsing de cartes, filtres, formats d'URL), comme Century 21 et Laforêt.

Invariants figés ici, chacun adossé aux captures réelles du 2026-08-23
(tests/fixtures/pap/, voir SCENARIOS.md pour la méthode et l'inventaire) :

* une carte est repérée par `a.item-title[href]` dont le href matche
  `/annonces/[\w-]*-r{id}` : les bannières promo déguisées en cartes (mêmes
  classes `search-list-item-alt`, href externe ou sans fiche -r{id}, capture
  rennes page02) sortent naturellement ;
* la classe du prix peut être MULTI-VALUÉE (`class="item-price txt-purple"`,
  r464901570 sur rennes p1) : seul un sélecteur par classe individuelle la
  trouve — un regex strict la raterait ;
* 30 à 64 % des cartes Paris sont SANS code postal exploitable (« Paris 15E »
  nu, pubs partenaires...) : échec FERMÉ _location_ok, et cartes exclues du
  décompte des « nouvelles » — quantifié sur les captures (9/14 sur paris
  filtré p01) ;
* le site sert des prix HORS BORNES quand même (890 €, 900 € < min et 2.050 €
  > max pour un filtre URL 1000-2000, capture paris filtrée p02) :
  _passes_filters rejoue TOUS les critères sur chaque annonce ;
* le plafond de profondeur serveur (25 pages) est confirmé en direct : la
  page 26 ne recycle que du déjà-vu -> arrêt + WARNING _DEPTH_CAP_SUSPECT ;
* le compteur machine de l'attribut `infinite-scroll` (annonces_total /
  annonces_page) fait foi sur les décomptes de chaque page ;
* les localisations se FUSIONNENT dans UN seul bloc g SANS tiret, ids triés
  en ordre NUMÉRIQUE croissant (la canonisation 301 du site sinon — capture
  live 23/08/2026, fix #9) : UNE série par TYPE demandé, le filtrage reçoit
  l'union des périmètres ;
* doublon intra-page réel (r401201732 ×2 sur paris filtré p01) et
  chevauchements inter-pages dès p4-p6 : la dédup vues_ici est nécessaire en
  conditions normales, le set seen global absorbe le recouvrement multi-séries.

Le marqueur « à X km » (_PROXIMITY_RE) et le prix décimal (« 820,30 € »)
n'apparaissent dans AUCUNE capture (SCENARIOS.md, limites honnêtes 1 et 2) :
ces cas sont exercés sur du HTML reconstruit inline au balisage réel.

Aucun appel réseau : PAP passe par curl_cffi (empreinte TLS navigateur), que
le garde-fou global `no_network` ne couvre pas — la session est donc doublée
ici et patchée dans parsers.pap.curl_requests.Session.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from curl_cffi.requests.exceptions import HTTPError as CurlHTTPError
from loguru import logger

from parsers.pap import (
    BASE_URL,
    IMPERSONATE,
    MAX_PAGES,
    PapParser,
    _clean_text,
    _describe,
    _dict_to_listing,
    _fetch_with_retries,
    _location_ok,
    _parse_cards,
    _parse_price,
    _passes_filters,
    _property_type_label,
    _transaction,
    _url_filter_segments,
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

RENNES = make_city_location("Rennes", "35000", "35238")
PARIS_15 = make_city_location("Paris", "75015", "75115")
# Toute la ville de Paris : les captures g439 couvrent les CP 75001..75020.
PARIS_WHOLE = make_whole_city_location(
    "Paris", tuple(f"75{i:03d}" for i in range(1, 21)), "75056"
)
GIRONDE = make_department_location("33", "Gironde")
IDF = make_region_location(
    "11", "Île-de-France", ("75", "77", "78", "91", "92", "93", "94", "95")
)

# ---------------------------------------------------------------------------
# Captures réelles (tests/fixtures/pap/results/ — octets originaux, aucune retouche)
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "pap" / "results"

_RENNES_SERIE = "locations-appartement-rennes-g43618-jusqu-a-900-euros"
_PARIS_FILTRE_SERIE = "locations-appartement-paris-g439-entre-1000-et-2000-euros"


def load_page(name: str) -> str:
    """Une capture réelle de pap.fr, telle que servie par le site."""
    return (FIXTURES_DIR / name).read_text()


RENNES_P1 = load_page(f"{_RENNES_SERIE}_page01.html")
RENNES_P2_PUBS = load_page(f"{_RENNES_SERIE}_page02.html")
RENNES_P3_RECYCLEE = load_page(f"{_RENNES_SERIE}_page03.html")
PARIS_FILTRE_P1 = load_page(f"{_PARIS_FILTRE_SERIE}_page01.html")
PARIS_FILTRE_P2_HORS_BORNES = load_page(f"{_PARIS_FILTRE_SERIE}_page02.html")
PARIS_G439_P24 = load_page("locations-appartement-paris-g439_page24.html")
PARIS_G439_P26_RECYCLEE = load_page("locations-appartement-paris-g439_page26_recyclee.html")

# uids des fiches -r{id} de rennes p1, dans l'ordre du document.
RENNES_UIDS = [
    "454000447", "464901570", "438301499", "464900223", "464601863",
    "462600525", "459800887", "464500800", "464500508", "459702435",
]
# uids des 14 fiches de paris filtré p1 : r401201732 et r464901267 y figurent
# DEUX FOIS (doublon intra-page réel).
PARIS_FILTRE_P1_UIDS = [
    "464801629", "195104104", "464800808", "435300110", "33480",
    "441302329", "401201732", "197104035", "440901681", "464901267",
    "422301226", "401201732", "464901267", "433101687",
]

_SCROLL_RE = re.compile(r'"annonces_total":(\d+),"annonces_page":(\d+)')


def machine_counter(page_html: str) -> tuple[int, int]:
    """(annonces_total, annonces_page) lus dans l'attribut infinite-scroll.

    L'attribut est encodé HTML (&quot;) et porte le décompte machine du site :
    plus fiable que tout grep de texte visible (SCENARIOS.md)."""
    attr = re.search(r'infinite-scroll="([^"]*)"', page_html)
    assert attr, "pas de compteur infinite-scroll sur cette capture"
    m = _SCROLL_RE.search(html.unescape(attr.group(1)))
    assert m, "compteur annonces_total/annonces_page absent"
    return int(m.group(1)), int(m.group(2))


# ---------------------------------------------------------------------------
# Fabriques HTML — reproductions du balisage réel des cartes PAP
# ---------------------------------------------------------------------------

def card(
    uid: str = "111111111",
    *,
    line: str = "Paris 15E (75015)",
    price: str = "1.500 €",
    price_class: str = "item-price",
    tags: tuple[str, ...] = ("2 pièces", "49 m²"),
    description: str = "Une description de carte.",
    image: str | None = "/photos/pap/x-p2.webp",
    href: str | None = None,
) -> str:
    """Une carte d'annonce au balisage réel de PAP (SCENARIOS.md), paramétrable.

    Le titre de carte EST la ligne de localisation ; le prix vit dans
    `.item-price-container`, la description APRÈS le lien (dans .item-body),
    la photo dans un carrousel AVANT lui — c'est ce balisage-là que lit le
    parser, jamais le texte global de la carte."""
    link = href if href is not None else f"/annonces/appartement-paris-15e-75015-r{uid}"
    carousel = (
        '<div class="owl-carousel"><div>'
        f'<img src="{image}" alt="{line}"></div></div>'
        if image
        else ""
    )
    tags_html = "".join(f"<li>{t}</li>" for t in tags)
    return (
        f'<div class="search-list-item-alt">{carousel}<div class="item-body">'
        f'<a class="item-title" href="{link}" name="{uid}">'
        f'<div class="item-price-container"><span class="{price_class}">{price}</span></div>'
        f'<span class="h1">{line}</span>'
        f'<ul class="item-tags">{tags_html}</ul>'
        "</a>"
        f'<p class="item-description">{description}</p>'
        "</div></div>"
    )


def page(*cards: str) -> str:
    return f"<html><body>{''.join(cards)}</body></html>"


EMPTY_PAGE_HTML = "<html><body></body></html>"

# Marqueur proximité JAMAIS observé dans les captures (SCENARIOS.md, limite
# honnête n°1) : ligne h1 reconstruite d'après le commentaire du parser.
PROXIMITY_CARD = card(uid="999000001", line="Vanves (92170) à 2km de Paris 15e")


# ---------------------------------------------------------------------------
# Doubles de session curl_cffi (PAP n'utilise PAS requests : le garde-fou
# global no_network ne le couvre pas, le double est donc obligatoire)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise CurlHTTPError(f"{self.status_code} Server Error")


class FakeSession:
    """Double de `curl_requests.Session` : enregistre les appels, rejoue des pages.

    `pages` est consommée dans l'ordre et la DERNIÈRE réponse est ensuite
    répétée indéfiniment, ce qui laisse la pagination s'arrêter d'elle-même
    sur « aucune annonce inédite ». `handler(url)` prend le dessus pour
    décider réponse par réponse."""

    def __init__(self, pages=None, handler=None):
        self.headers: dict[str, str] = {}
        self.calls: list[dict] = []
        self._responses = [p if isinstance(p, FakeResponse) else FakeResponse(p) for p in (pages or [EMPTY_PAGE_HTML])]
        self._handler = handler

    def get(self, url, timeout=None):
        self.calls.append({"url": url, "timeout": timeout})
        if self._handler is not None:
            return self._handler(url)
        response = self._responses[0]
        if len(self._responses) > 1:
            self._responses.pop(0)
        return response

    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]


def run_scrape(criteria: dict, session: FakeSession, parser: PapParser | None = None):
    """Exécute `scrape()` en substituant `session` à la vraie session curl_cffi."""
    parser = parser or PapParser()
    with patch("parsers.pap.curl_requests.Session", return_value=session):
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


def pap_storage():
    """Un Storage doublé dont le cache géo PAP est câblé.

    `fake_storage()` pré-câble désormais `pap_geo` comme tous les repos géo
    (mock spécifié sur PapGeoRepository, cache neutre). L'override explicite
    est conservé pour afficher l'intention : un cache géo sous contrôle,
    sans dépendre du câblage par défaut du helper."""
    from tests.helpers.fakes import fake_storage

    return fake_storage(pap_geo=MagicMock())


def manual_criteria(geo_id: str = "43618", **overrides) -> dict:
    """Des critères portant un identifiant saisi à la main (aucun storage).

    Par défaut : Rennes <= 900 € en location appartement — l'URL produite est
    alors EXACTEMENT celle des captures rennes page01..03."""
    criteria = {
        "locations": [RENNES],
        "transaction": "rent",
        "propertyTypes": ["apartment"],
        "priceMax": 900,
        "sourceOverrides": {"pap": {"geoIds": [geo_id]}},
    }
    criteria.update(overrides)
    return criteria


def paris_criteria(**overrides) -> dict:
    """Des critères Paris ville entière, bornés 1000-2000 € — l'URL des
    captures paris g439 filtrées."""
    criteria = {
        "locations": [PARIS_WHOLE],
        "transaction": "rent",
        "propertyTypes": ["apartment"],
        "priceMin": 1000,
        "priceMax": 2000,
        "sourceOverrides": {"pap": {"geoIds": ["439"]}},
    }
    criteria.update(overrides)
    return criteria


def fused_criteria(**overrides) -> dict:
    """Des critères portant DEUX localisations, leurs ids saisis à la main
    dans l'ordre des localisations (« 43618 » Rennes puis « 37782 » Paris) :
    l'ordre du bloc g est l'affaire du parser — le tri NUMÉRIQUE mettra
    37782 devant 43618 (canonisation 301 du site sinon)."""
    criteria = {
        "locations": [RENNES, PARIS_15],
        "transaction": "rent",
        "propertyTypes": ["apartment"],
        "sourceOverrides": {"pap": {"geoIds": ["43618", "37782"]}},
    }
    criteria.update(overrides)
    return criteria


# ===========================================================================
# P1 — _parse_price : formats réels + décimal reconstruit
# ===========================================================================


class TestParsePrice:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Captures réelles : rennes p1 (« 510 € ») et paris filtré p1
            # (« 1.527 € » — le POINT sépare les milliers chez PAP).
            ("510 €", 510.0),
            ("450 €", 450.0),
            ("1.527 €", 1527.0),
            ("2.050 €", 2050.0),
            ("649.000 €", 649000.0),
            # Prix décimal JAMAIS capturé (SCENARIOS.md, limite n°2) : la
            # virgule est le séparateur décimal en location.
            ("820,30 €", 820.3),
            ("820,30 € par mois charges comprises", 820.3),
            # Milliers ET décimales mélangés.
            ("1.527,50 €", 1527.5),
            # Aucun montant : pas de prix, pas d'exception.
            ("", None),
            ("Prix : nous consulter", None),
            (None, None),
            # Le symbole sans chiffre : float("") lève, ValueError rattrapée.
            ("€", None),
        ],
    )
    def test_prices(self, text, expected):
        assert _parse_price(text) == expected


# ===========================================================================
# P2 — _property_type_label / _clean_text / _transaction / _describe
# ===========================================================================


class TestPropertyTypeLabel:
    @pytest.mark.parametrize(
        ("href", "expected"),
        [
            ("/annonces/appartement-paris-15e-75015-r123", "Appartement"),
            # Capture réelle r33480 : le type se lit dans le slug d'un lien
            # partenaire externe aussi bien que d'une fiche interne.
            (
                "https://www.acceslogement.fr/annonces/location-paris-19e-appartement-logement-social-r33480",
                "Appartement",
            ),
            ("/annonces/studio-rennes-35000-r124", "Studio"),
            ("/annonces/maison-cestas-33610-r125", "Maison"),
            ("/annonces/terrain-gironde-r126", "Terrain"),
            ("/annonces/parking-paris-r127", "Parking"),
            ("/annonces/garage-paris-r128", "Parking"),
            # « -box- » avec ses tirets (un « box » en début de segment ne
            # porte pas le préfixe).
            ("/annonces/cave-box-paris-r129", "Parking"),
            # Colocation (rennes p1) : aucun token de type dans le slug.
            ("/annonces/colocation-rennes-35000-r454000447", ""),
            ("/annonces/loft-paris-r130", ""),
            ("", ""),
        ],
    )
    def test_the_type_is_read_in_the_detail_slug(self, href, expected):
        assert _property_type_label(href) == expected


class TestCleanText:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Rennes (35000)", "Rennes (35000)"),
            ("510  €", "510 €"),
            ("  espaces   multiples  ", "espaces multiples"),
            (None, ""),
            ("", ""),
        ],
        ids=["insecable", "double_insecable", "espaces", "none", "vide"],
    )
    def test_nbsp_are_flattened(self, text, expected):
        assert _clean_text(text) == expected


class TestTransaction:
    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            ({"transaction": "rent"}, "rent"),
            ({"transaction": "buy"}, "buy"),
            # La location par défaut : premier choix du formulaire.
            ({}, "rent"),
            ({"transaction": "nawak"}, "rent"),
        ],
        ids=["rent", "buy", "absente", "inconnue"],
    )
    def test_the_canonical_transaction(self, criteria, expected):
        assert _transaction(criteria) == expected


class TestDescribe:
    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            (RENNES, "Rennes (35000)"),
            (make_whole_city_location("Poitiers"), "Poitiers (toute la ville)"),
            (GIRONDE, "département Gironde"),
            (IDF, "région Île-de-France"),
            # Sans nom, le code reste lisible dans les logs.
            ({"kind": "department", "code": "33"}, "département 33"),
            ({"kind": "region", "code": "11"}, "région 11"),
        ],
        ids=["commune", "ville_entiere", "departement", "region", "dept_sans_nom", "region_sans_nom"],
    )
    def test_a_perimeter_in_plain_words(self, location, expected):
        assert _describe(location) == expected


# ===========================================================================
# P1 — _parse_cards sur les captures réelles
# ===========================================================================


class TestParseCardsRealCaptures:
    def test_every_field_of_a_real_rennes_card(self):
        """La carte nominale r454000447 de rennes p1 : tous les champs lus
        dans leur élément dédié, photo CDN ABSOLUE non préfixée."""
        by_uid = {c["uid"]: c for c in _parse_cards(RENNES_P1)}
        c = by_uid["454000447"]

        assert c["url"] == f"{BASE_URL}/annonces/colocation-rennes-35000-r454000447"
        assert c["title"] == "Rennes (35000)"
        assert c["price_text"] == "510 €"
        assert c["price_value"] == 510.0
        assert c["city"] == "Rennes"
        assert c["zip_code"] == "35000"
        # Tags réels : « Chambre en colocation », « 4 pièces », « 68 m² » —
        # pas de tag chambres sur cette carte.
        assert c["rooms"] == "4"
        assert c["bedrooms"] == ""
        assert c["surface"] == "68"
        assert c["description"].startswith("Colocation meublée")
        assert c["image_url"] == (
            "https://cdn.pap.fr/photos/pap/c9/d8/c9d827d5264a1cf6c7b088954317cf53/c-p2.webp"
        )
        # Une colocation ne porte aucun type reconnu dans son slug.
        assert c["property_type"] == ""

    def test_document_order_is_preserved_on_rennes_p1(self):
        assert [c["uid"] for c in _parse_cards(RENNES_P1)] == RENNES_UIDS

    def test_the_multi_valued_price_class_is_still_found(self):
        """🔒 r464901570 (rennes p1) porte `class="item-price txt-purple"` :
        seul un sélecteur par classe individuelle trouve ce prix — un regex
        `class="item-price"` strict passerait à côté."""
        assert 'class="item-price txt-purple"' in RENNES_P1, "la capture doit porter la classe multi-valuée"

        cards = {c["uid"]: c for c in _parse_cards(RENNES_P1)}

        assert cards["464901570"]["price_value"] == 450.0
        assert cards["464901570"]["price_text"] == "450 €"

    def test_the_promo_banners_of_the_end_page_yield_nothing(self):
        """🔒 Fin de série réelle : rennes p2 ne contient ZÉRO fiche -r{id},
        uniquement deux bannières promo déguisées (mêmes classes
        search-list-item-alt / a.item-title, href externe) — elles sortent
        naturellement via _DETAIL_PATH_RE."""
        assert 'class="search-list-item-alt"' in RENNES_P2_PUBS, "la capture doit contenir des bannières"

        assert _parse_cards(RENNES_P2_PUBS) == []

    def test_paris_filtered_p1_fiches_and_intra_page_duplicates(self):
        """14 fiches détectées sur paris filtré p1, dont r401201732 et
        r464901267 présents DEUX FOIS chacun (doublon intra-page réel)."""
        cards = _parse_cards(PARIS_FILTRE_P1)

        assert [c["uid"] for c in cards] == PARIS_FILTRE_P1_UIDS
        assert len([u for u in PARIS_FILTRE_P1_UIDS if u == "401201732"]) == 2

    def test_zipless_cards_are_quantified_on_real_paris_page(self):
        """🔒 En live, jusqu'à ~64 % des cartes Paris sont SANS code postal
        exploitable (« Paris 15E » nu, pubs partenaires...) : 9 des 14 fiches
        de paris filtré p1 ont un zip_code vide — chacune sera rejetée par
        l'échec fermé _location_ok."""
        cards = _parse_cards(PARIS_FILTRE_P1)
        zipless = [c for c in cards if not c["zip_code"]]

        assert len(zipless) == 9
        assert len(cards) - len(zipless) == 5
        # Les lignes nues observées : « Paris 15E », « Appartement Paris 19e »...
        assert any(c["title"] == "Paris 15E" and c["uid"] == "464801629" for c in zipless)
        assert any(c["title"] == "Appartement Paris 19e" and c["uid"] == "33480" for c in zipless)

    def test_partner_card_type_comes_from_the_external_slug(self):
        """r33480 pointe acceslogement.fr mais son slug contient
        « appartement » : le type se lit quand même depuis le href."""
        cards = {c["uid"]: c for c in _parse_cards(PARIS_FILTRE_P1)}

        assert cards["33480"]["property_type"] == "Appartement"
        assert cards["33480"]["zip_code"] == ""

    def test_decimal_surface_from_real_tags(self):
        """« 45,50 m² » (r464501636, paris filtré p3) : la virgule décimale
        des tags est conservée telle quelle — comparée plus loin par
        _passes_filters, qui sait la convertir."""
        html_text = load_page(f"{_PARIS_FILTRE_SERIE}_page03.html")
        cards = {c["uid"]: c for c in _parse_cards(html_text)}

        assert cards["464501636"]["surface"] == "45,50"

    def test_machine_counter_matches_served_cards(self):
        """Le compteur `infinite-scroll` fait foi : rennes p1 annonce 10
        cartes servies (annonces_page=10) et le parse en trouve exactement
        10 ; la fin de série en annonce 0 et le parse ne trouve rien."""
        total, served = machine_counter(RENNES_P1)
        assert (total, served) == (12, 10)
        assert len(_parse_cards(RENNES_P1)) == served

        total_fin, served_fin = machine_counter(RENNES_P2_PUBS)
        assert served_fin == 0
        assert _parse_cards(RENNES_P2_PUBS) == []
        assert total_fin == total, "la taille du pool est constante sur toute la série"

    def test_deep_pages_serve_more_fiches_than_the_counter_admits(self):
        """Pages profondes : annonces_page=12 sur g439 p24 mais 15 fiches
        détectées — le compteur compte les cartes SERVIES, le parse voit en
        plus doublons et élargissements (SCENARIOS.md, faits transverses)."""
        total, served = machine_counter(PARIS_G439_P24)

        assert (total, served) == (337, 12)
        assert len(_parse_cards(PARIS_G439_P24)) == 15


class TestParseCardsReconstructed:
    def test_proximity_marked_cards_are_never_parsed(self):
        """Les annonces « proches » intercalées portent une distance dans la
        ligne de localisation (jamais observé en live — SCENARIOS.md, limite
        n°1 — HTML reconstruit au balisage réel)."""
        cards = _parse_cards(page(card(), PROXIMITY_CARD))

        assert [c["uid"] for c in cards] == ["111111111"]

    def test_a_card_without_price_nor_postal_code_still_parses(self):
        """Fail-open sur les données de la carte : une annonce incomplète
        (prix illisible, localisation sans parenthèse) vaut mieux qu'une
        exception qui ferait perdre toute la page — elle sera écartée plus
        loin (échec fermé sur le CP)."""
        parsed = _parse_cards(
            page(card(uid="42", line="Avec Grand Balcon", price="", tags=()))
        )[0]

        assert parsed["uid"] == "42"
        assert parsed["price_value"] is None
        assert parsed["city"] == ""
        assert parsed["zip_code"] == ""
        assert parsed["surface"] == ""
        assert parsed["rooms"] == ""

    def test_a_link_without_a_detail_reference_is_skipped(self):
        html_text = page('<a class="item-title" href="/pass-prioritaire">Pub</a>', card())

        assert [c["uid"] for c in _parse_cards(html_text)] == ["111111111"]

    def test_the_description_is_truncated_to_300_characters(self):
        parsed = _parse_cards(page(card(description="a" * 500)))[0]

        assert parsed["description"] == "a" * 300

    @pytest.mark.parametrize(
        ("image", "expected"),
        [
            # src relatif -> préfixé par le domaine (défensif : les captures
            # réelles ne portent que des URLs CDN déjà absolues).
            ("/photos/pap/x-p2.webp", f"{BASE_URL}/photos/pap/x-p2.webp"),
            # CDN absolu (cas réel rennes p1) -> laissé tel quel, jamais préfixé.
            (
                "https://cdn.pap.fr/photos/pap/c9/d8/x-p2.webp",
                "https://cdn.pap.fr/photos/pap/c9/d8/x-p2.webp",
            ),
            # Placeholder base64 : pas une photo d'annonce.
            ("data:image/gif;base64,R0lGODlh", ""),
            # Absent -> chaîne vide.
            (None, ""),
        ],
        ids=["relatif", "cdn_absolu", "placeholder_data", "absent"],
    )
    def test_image_url_normalisation(self, image, expected):
        parsed = _parse_cards(page(card(uid="1", image=image)))[0]

        assert parsed["image_url"] == expected


# ===========================================================================
# P1 — _location_ok / _passes_filters : le périmètre et le rejeu des critères
# ===========================================================================


class TestLocationOk:
    @pytest.mark.parametrize(
        ("zip_code", "locations", "expected"),
        [
            # Code complet : comparaison stricte via matches_locations.
            ("35000", [RENNES], True),
            ("35000", [PARIS_15], False),
            # Préfixe départemental DANS LES DEUX SENS : un code tronqué (« 33 »)
            # tombe sous les codes complets du département, un code complet
            # (« 33000 ») est couvert par le préfixe départemental.
            ("33", [GIRONDE], True),
            ("33000", [GIRONDE], True),
            ("44", [GIRONDE], False),
            # Recherche multi-périmètres : n'importe lequel suffit.
            ("75015", [PARIS_15, RENNES], True),
            # Code postal illisible : échec FERMÉ — c'est ce qui tient les
            # cartes « Paris 15E » nues hors périmètre.
            ("", [PARIS_WHOLE], False),
            (None, [PARIS_WHOLE], False),
            # Aucun périmètre : rien ne peut correspondre.
            ("35000", [], False),
        ],
        ids=[
            "complet_dans_perimetre", "complet_hors_perimetre",
            "prefixe_dept", "complet_sous_prefixe", "autre_dept",
            "multi_perimetres", "cp_vide", "cp_none", "sans_perimetre",
        ],
    )
    def test_complete_truncated_and_missing_postal_codes(self, zip_code, locations, expected):
        assert _location_ok(zip_code, locations) is expected

    def test_a_whole_city_covers_its_postal_codes(self):
        assert _location_ok("75015", [PARIS_WHOLE]) is True
        assert _location_ok("92100", [PARIS_WHOLE]) is False


class TestPassesFilters:
    def test_a_rejected_location_short_circuits_every_other_filter(self):
        listing = make_listing(zip_code="92100", price_value=1500.0)

        assert _passes_filters(listing, {}, [PARIS_WHOLE]) is False

    @pytest.mark.parametrize(
        ("price_value", "criteria", "expected"),
        [
            # Valeurs réelles de paris filtré p2 : le site sert 890 €, 900 €
            # (< min) et 2.050 € (> max) POUR UN FILTRE URL 1000-2000 —
            # le rejeu local (_passes_filters) est le filet exact.
            (890.0, {"priceMin": 1000, "priceMax": 2000}, False),
            (900.0, {"priceMin": 1000, "priceMax": 2000}, False),
            (2050.0, {"priceMin": 1000, "priceMax": 2000}, False),
            (1850.0, {"priceMin": 1000, "priceMax": 2000}, True),
            (1100.0, {"priceMin": 1000, "priceMax": 2000}, True),
            # Bornes inclusives.
            (1000.0, {"priceMin": 1000, "priceMax": 2000}, True),
            (2000.0, {"priceMin": 1000, "priceMax": 2000}, True),
            # FAIL-OPEN : un prix illisible ne fait pas écarter l'annonce.
            (None, {"priceMax": 1}, True),
        ],
        ids=[
            "890_hors_bornes", "900_hors_bornes", "2050_hors_bornes", "1850_ok",
            "1100_ok", "borne_min", "borne_max", "prix_illisible",
        ],
    )
    def test_price_bounds(self, price_value, criteria, expected):
        listing = make_listing(zip_code="75015", price_value=price_value)

        assert _passes_filters(listing, criteria, [PARIS_WHOLE]) is expected

    @pytest.mark.parametrize(
        ("surface", "criteria", "expected"),
        [
            # « 45,50 m² » réel (paris filtré p3) : la virgule décimale est
            # convertie avant comparaison.
            ("45,50", {"surfaceMin": 40, "surfaceMax": 60}, True),
            ("45,50", {"surfaceMin": 46}, False),
            ("49", {"surfaceMax": 40}, False),
            ("", {"surfaceMin": 1000}, True),
        ],
        ids=["decimale_ok", "decimale_trop_petite", "hors_max", "absente"],
    )
    def test_surface_bounds(self, surface, criteria, expected):
        listing = make_listing(zip_code="75015", surface=surface)

        assert _passes_filters(listing, criteria, [PARIS_WHOLE]) is expected

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
        ids=["dans_liste", "hors_liste", "cinq_et_plus", "sous_cinq", "illisible"],
    )
    def test_room_counts(self, rooms, criteria, expected):
        listing = make_listing(zip_code="75015", rooms=rooms)

        assert _passes_filters(listing, criteria, [PARIS_WHOLE]) is expected


# ===========================================================================
# P2 — _dict_to_listing : la carte vers le schéma commun
# ===========================================================================


class TestDictToListing:
    def test_maps_a_real_card_to_the_common_schema(self):
        data = next(c for c in _parse_cards(RENNES_P1) if c["uid"] == "454000447")

        listing = _dict_to_listing(data)

        assert listing.listing_id == "pap_454000447"
        assert listing.legacy_id == "454000447"
        assert listing.source == "pap"
        assert listing.title == "Rennes (35000)"
        assert listing.price == "510 €"
        assert listing.price_value == 510.0
        assert listing.city == "Rennes"
        assert listing.location == "Rennes"
        assert listing.zip_code == "35000"
        assert listing.rooms == "4"
        assert listing.surface == "68"

    def test_pap_is_private_to_private_with_no_agency(self):
        """PAP est une plateforme de particuliers : aucune agence sur les
        cartes (le blacklist par agence ne s'applique pas à cette source) et
        toutes les annonces sont privées."""
        listing = _dict_to_listing(_parse_cards(page(card()))[0])

        assert listing.agency == ""
        assert listing.is_private is True


# ===========================================================================
# P1 — segments de filtres natifs, séries et paires
# ===========================================================================


class TestUrlFilterSegments:
    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            ({}, []),
            ({"priceMax": 900}, ["jusqu-a-900-euros"]),
            ({"priceMin": 500}, ["a-partir-de-500-euros"]),
            ({"priceMin": 1000, "priceMax": 2000}, ["entre-1000-et-2000-euros"]),
            ({"surfaceMax": 60}, ["jusqu-a-60-m2"]),
            ({"surfaceMin": 20}, ["a-partir-de-20-m2"]),
            ({"surfaceMin": 20, "surfaceMax": 60}, ["entre-20-et-60-m2"]),
            # Prix avant surface, ordre canonique du site.
            (
                {"priceMin": 1000, "priceMax": 2000, "surfaceMin": 20},
                ["entre-1000-et-2000-euros", "a-partir-de-20-m2"],
            ),
        ],
        ids=[
            "rien", "jusqu_a_euros", "a_partir_de_euros", "entre_euros",
            "jusqu_a_m2", "a_partir_de_m2", "entre_m2", "combines",
        ],
    )
    def test_native_segments_follow_the_site_s_own_grammar(self, criteria, expected):
        assert _url_filter_segments(criteria) == expected

    def test_filters_exist_whenever_bounds_do(self):
        """Ces segments ne sont pas optionnels : la pagination HTML plafonne
        à ~25 PAGES côté serveur (~330 annonces, page 26 recyclée vérifiée en
        direct) — sans filtres émis, une recherche large serait tronquée."""
        assert _url_filter_segments({"priceMax": 900}) != []
        assert _url_filter_segments({"surfaceMin": 20}) != []


class TestSeries:
    def test_no_demanded_type_gives_the_generic_segment(self):
        """Sans type demandé, le segment générique de la transaction couvre
        tout — c'est le seul « TOUT TYPES » que le parser émet jamais."""
        criteria = manual_criteria()
        criteria.pop("propertyTypes")

        series = PapParser()._series(criteria, [RENNES])

        assert [(geo_ids, segment) for _, geo_ids, segment in series] == [
            (["43618"], "locations")
        ]

    def test_one_series_per_demanded_type_in_requested_order(self):
        """Une série PAR TYPE demandé, dans l'ordre demandé (mono-ville : le
        bloc g ne porte qu'un id, format identique au mono historique)."""
        criteria = manual_criteria(propertyTypes=["apartment", "house"])

        series = PapParser()._series(criteria, [RENNES])

        assert [(geo_ids, segment) for _, geo_ids, segment in series] == [
            (["43618"], "locations-appartement"),
            (["43618"], "locations-maison"),
        ]

    def test_buy_paths_use_the_site_s_own_plural_forms(self):
        criteria = manual_criteria(transaction="buy", propertyTypes=["apartment", "parking"])

        series = PapParser()._series(criteria, [RENNES])

        assert [(geo_ids, segment) for _, geo_ids, segment in series] == [
            (["43618"], "vente-appartements"),
            (["43618"], "vente-parking"),
        ]

    def test_every_series_carries_all_locations_sorted_numerically(self):
        """🔒 Fusion systématique (issue #9) : chaque série porte TOUTES les
        localisations fusibles — une recherche 2 villes × 1 type produit UNE
        série, pas deux. Les ids sont dédoublonnés puis triés en ordre
        NUMÉRIQUE croissant quel que soit l'ordre résolu/saisi (la
        canonisation 301 du site sinon), et les localisations suivent leur id."""
        locations = [PARIS_15, RENNES]  # saisie : Paris d'abord, ids en désordre numérique
        criteria = {
            "locations": locations,
            "transaction": "rent",
            "propertyTypes": ["apartment"],
            "sourceOverrides": {"pap": {"geoIds": ["37782", "43618"]}},
        }

        series = PapParser()._series(criteria, locations)

        assert [(fused, geo_ids, segment) for fused, geo_ids, segment in series] == [
            ([PARIS_15, RENNES], ["37782", "43618"], "locations-appartement")
        ]

    def test_a_type_without_any_rent_segment_is_refused_not_widened(self, logged):
        """🔒 Comportement mémoire figé par un test explicite : demander
        UNIQUEMENT un type sans recherche dédiée (le terrain en location,
        qui n'existe pas sur PAP) lève plutôt que d'élargir silencieusement
        à TOUT TYPES — un élargissement ferait scraper des biens jamais
        demandés (le site réécrirait le slug en page générique)."""
        criteria = manual_criteria(propertyTypes=["land"])

        with pytest.raises(ValueError, match="PAP ne référence pas"):
            PapParser()._series(criteria, [RENNES])

        assert any(level == "WARNING" and "ignorés : land" in m for level, m in logged)

    def test_a_mixed_request_keeps_expressible_types_and_warns_about_the_rest(self, logged):
        criteria = manual_criteria(propertyTypes=["apartment", "land"])

        series = PapParser()._series(criteria, [RENNES])

        assert [(geo_ids, segment) for _, geo_ids, segment in series] == [
            (["43618"], "locations-appartement")
        ]
        assert any(level == "WARNING" and "ignorés : land" in message for level, message in logged)


class TestPairs:
    def test_manual_geo_ids_short_circuit_every_resolution(self):
        """Les ids collés à la main court-circuitent cache et autocomplete :
        resolve_geo_id ne doit jamais être appelé."""
        locations = [RENNES, PARIS_15]
        criteria = {
            "locations": locations,
            "sourceOverrides": {"pap": {"geoIds": ["43618", "37782"]}},
        }

        with patch("services.pap_geocode.resolve_geo_id") as mock_resolve:
            pairs = PapParser()._pairs(criteria, locations)

        assert pairs == [(RENNES, "43618"), (PARIS_15, "37782")]
        mock_resolve.assert_not_called()

    def test_manual_geo_ids_epouse_what_the_user_gave(self):
        """`zip(strict=False)` : plus ou moins d'ids que de villes, on épouse
        ce qu'il y a, dans l'ordre."""
        locations = [RENNES, PARIS_15]
        criteria = {"locations": locations, "sourceOverrides": {"pap": {"geoIds": ["43618"]}}}

        pairs = PapParser()._pairs(criteria, locations)

        assert pairs == [(RENNES, "43618")]

    def test_without_storage_the_impossibility_is_logged(self, logged):
        pairs = PapParser()._pairs({"locations": [RENNES]}, [RENNES])

        assert pairs == []
        assert any(
            level == "WARNING" and "Aucun storage fourni au parser" in message
            for level, message in logged
        )

    def test_city_and_whole_city_both_resolve_through_the_repo(self):
        """Ville simple ET ville entière passent par le même service de
        résolution, avec le repo injecté explicitement (jamais lu depuis un
        contexte Flask : le scraping tourne sur un thread de fond)."""
        storage = pap_storage()
        poitiers = make_whole_city_location("Poitiers")
        resolved = {RENNES["postalCode"]: "43618", poitiers["city"]: "86194"}

        with patch(
            "services.pap_geocode.resolve_geo_id",
            side_effect=lambda loc, repo: resolved[loc.get("postalCode") or loc.get("city")],
        ) as mock_resolve:
            pairs = PapParser(storage=storage)._pairs(
                {"locations": [RENNES, poitiers]}, [RENNES, poitiers]
            )

        assert pairs == [(RENNES, "43618"), (poitiers, "86194")]
        assert {call.kwargs["repo"] for call in mock_resolve.call_args_list} == {storage.pap_geo}

    def test_an_unresolved_location_is_skipped_with_a_warning(self, logged):
        """Un périmètre dont la résolution échoue est écarté avec un
        avertissement — jamais développé en liste de communes."""
        storage = pap_storage()

        with patch("services.pap_geocode.resolve_geo_id", return_value=None):
            pairs = PapParser(storage=storage)._pairs({"locations": [RENNES]}, [RENNES])

        assert pairs == []
        assert any(level == "WARNING" and "Aucun identifiant résolu" in m for level, m in logged)

    def test_the_repo_comes_from_the_injected_storage(self):
        storage = pap_storage()

        assert PapParser(storage=storage)._geo_repo() is storage.pap_geo
        # Un storage dépourvu de repo pap_geo (accès = AttributeError) est
        # traité comme « pas de storage » : jamais d'exception. `fake_storage()`
        # pré-câblant désormais tous les repos géo, on simule l'absence par un
        # objet qui n'a vraiment pas l'attribut.
        from types import SimpleNamespace

        assert PapParser(storage=SimpleNamespace())._geo_repo() is None
        assert PapParser()._geo_repo() is None


# ===========================================================================
# P1 — build_search_urls : une URL par type, localisations fusionnées
# ===========================================================================


class TestBuildSearchUrls:
    def test_two_cities_are_fused_into_one_url_with_a_sorted_geo_block(self):
        """🔒 Fusion systématique (issue #9) : 2 villes × 1 type = UNE seule
        URL portant les DEUX ids CONCATÉNÉS dans un seul bloc g, trié en
        ordre NUMÉRIQUE croissant quel que soit l'ordre saisi — vérifié en
        direct le 23/08/2026 (g439g43267 -> canonisé puis 200 avec les
        annonces des deux périmètres). Le slug suit la localisation du
        PREMIER id du bloc (Paris ici : 37782 < 43618)."""
        urls = PapParser().build_search_urls(fused_criteria())

        assert urls == [f"{BASE_URL}/annonce/locations-appartement-paris-g37782g43618"]
        assert len(urls) == 1, "une seule série fusionnée, plus une URL par localisation"

    def test_the_geo_block_is_numeric_sorted_not_lexical(self):
        """« 10000 » < « 439 » lexicalement mais 439 < 10000 numériquement :
        un tri lexical émettrait g10000g439 et déclencherait la 301 de
        canonisation du site."""
        criteria = {
            "locations": [RENNES, PARIS_WHOLE],
            "transaction": "buy",
            "propertyTypes": [],
            "sourceOverrides": {"pap": {"geoIds": ["10000", "439"]}},
        }

        url = PapParser().build_search_urls(criteria)[0]

        assert url == f"{BASE_URL}/annonce/vente-immobiliere-paris-g439g10000"

    def test_a_non_numeric_geo_id_sorts_after_the_numeric_ones_without_raising(self):
        """Écart audit n°2 : un identifiant saisi à la main NON numérique
        (faute de frappe, format inattendu) est aberrant mais ne doit PAS faire
        lever le tri int() : il passe après tous les numériques, et l'URL reste
        émise — c'est le site qui la refusera le cas échéant, pas le parser."""
        criteria = {
            "locations": [RENNES, PARIS_WHOLE],
            "transaction": "buy",
            "propertyTypes": [],
            "sourceOverrides": {"pap": {"geoIds": ["nawak", "439"]}},
        }

        url = PapParser().build_search_urls(criteria)[0]

        assert url == f"{BASE_URL}/annonce/vente-immobiliere-paris-g439gnawak"

    def test_several_non_numeric_geo_ids_keep_their_insertion_order(self):
        """Écart audit n°2 (suite) : entre ids aberrants la clé de tri ne
        départage rien (tous « non numériques ») — le tri Python stable garde
        l'ordre de saisie, donc l'URL reste DÉTERMINISTE d'un run à l'autre."""
        criteria = {
            "locations": [RENNES, PARIS_15],
            "transaction": "buy",
            "propertyTypes": [],
            "sourceOverrides": {"pap": {"geoIds": ["zz", "aa"]}},
        }

        url = PapParser().build_search_urls(criteria)[0]

        assert url == f"{BASE_URL}/annonce/vente-immobiliere-rennes-gzzgaa"

    def test_duplicate_geo_ids_are_deduplicated_before_sorting(self):
        """Écart audit n°2 (fin) : deux périmètres portant le MÊME identifiant
        (saisie en double) n'émettent qu'un seul bloc ; setdefault garde la
        PREMIÈRE localisation, dont le nom alimente le slug."""
        criteria = {
            "locations": [RENNES, PARIS_WHOLE],
            "transaction": "buy",
            "propertyTypes": [],
            "sourceOverrides": {"pap": {"geoIds": ["43618", "43618"]}},
        }

        url = PapParser().build_search_urls(criteria)[0]

        assert url == f"{BASE_URL}/annonce/vente-immobiliere-rennes-g43618"

    def test_no_dash_inside_the_fused_block(self):
        """La forme -gA-gB (tiret entre blocs) ne porte que le PREMIER
        périmètre (redirection vérifiée en direct) : elle est interdite par
        construction — verrou négatif explicite."""
        url = PapParser().build_search_urls(fused_criteria())[0]

        assert "-g37782-g43618" not in url
        assert "-g43618-g37782" not in url

    def test_two_types_stay_two_fused_urls(self):
        """La fusion regroupe les PÉRIMÈTRES dans chaque série, jamais les
        types : 2 types demandés restent 2 URLs, chacune portant toutes les
        localisations."""
        urls = PapParser().build_search_urls(
            fused_criteria(propertyTypes=["apartment", "house"])
        )

        assert urls == [
            f"{BASE_URL}/annonce/locations-appartement-paris-g37782g43618",
            f"{BASE_URL}/annonce/locations-maison-paris-g37782g43618",
        ]

    def test_a_single_location_keeps_the_historical_mono_format(self):
        """Non-régression mono-localisation (issue #9, exigence 2) : UN seul
        périmètre produit le bloc g à un seul id — le format d'avant la
        fusion, celui des captures réelles, reste byte-for-byte valide."""
        urls = PapParser().build_search_urls(manual_criteria())

        assert urls == [f"{BASE_URL}/annonce/{_RENNES_SERIE}"]

    def test_the_rent_series_url_matches_the_real_capture_byte_for_byte(self):
        """Critères Rennes <= 900 € : l'URL construite est EXACTEMENT celle
        des captures rennes page01..03 (segment natif de prix compris)."""
        url = PapParser().build_search_urls(manual_criteria())[0]

        assert url == f"{BASE_URL}/annonce/{_RENNES_SERIE}"

    def test_native_filters_are_appended_after_the_geo_id(self):
        assert PapParser().build_search_urls(paris_criteria())[0] == (
            f"{BASE_URL}/annonce/{_PARIS_FILTRE_SERIE}"
        )

    def test_multiple_types_multiply_the_urls(self):
        criteria = manual_criteria(propertyTypes=["apartment", "house"])

        urls = PapParser().build_search_urls(criteria)

        assert urls == [
            f"{BASE_URL}/annonce/{_RENNES_SERIE}",
            f"{BASE_URL}/annonce/locations-maison-rennes-g43618-jusqu-a-900-euros",
        ]

    @pytest.mark.parametrize(
        ("location", "geo_id", "expected_slug"),
        [
            # Slug PUREMENT alphabétique : jamais de chiffre en fin de slug,
            # la réécriture du site ferait perdre le suffixe de pagination.
            (make_city_location("Saint-Étienne", "42000", "42218"), "4218", "saint-etienne"),
            (GIRONDE, "397", "gironde"),
            (IDF, "471", "ile-de-france"),
        ],
        ids=["accents", "departement", "region"],
    )
    def test_every_perimeter_level_gets_a_readable_ascii_slug(self, location, geo_id, expected_slug):
        criteria = {
            "locations": [location],
            "transaction": "rent",
            "propertyTypes": [],
            "sourceOverrides": {"pap": {"geoIds": [geo_id]}},
        }

        url = PapParser().build_search_urls(criteria)[0]

        assert url == f"{BASE_URL}/annonce/locations-{expected_slug}-g{geo_id}"

    def test_build_search_url_returns_the_first_url(self):
        parser = PapParser()
        criteria = manual_criteria(propertyTypes=["apartment", "house"])

        assert parser.build_search_url(criteria) == parser.build_search_urls(criteria)[0]

    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}, {"locations": [{"kind": "city", "city": "Paris"}]}],
        ids=["vide", "liste_vide", "localisation_incomplete"],
    )
    def test_no_url_without_a_location(self, criteria):
        assert PapParser().build_search_urls(criteria) == []
        assert PapParser().build_search_url(criteria) is None

    def test_to_native_is_an_identity(self):
        """Rien à traduire : PAP construit ses URLs directement depuis le
        canonique, et le rejeu des critères (_passes_filters) les lit tel quel."""
        criteria = manual_criteria()

        assert PapParser().to_native(criteria) is criteria


# ===========================================================================
# P1 — parse_manual_override / remember_manual_override / has_valid_criteria
# ===========================================================================


class TestParseManualOverride:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", {}),
            ("   ", {}),
            (None, {}),
            ("43618", {"geoIds": ["43618"]}),
            ("  43618  ", {"geoIds": ["43618"]}),
            ("43618, 37782", {"geoIds": ["43618", "37782"]}),
            ("43618 ,37782", {"geoIds": ["43618", "37782"]}),
            ("43618,,   ,37782", {"geoIds": ["43618", "37782"]}),
            ("43618,", {"geoIds": ["43618"]}),
            (",", {}),
            (" , , ", {}),
        ],
    )
    def test_comma_separated_geo_ids(self, value, expected):
        assert PapParser().parse_manual_override(value) == expected


class TestRememberManualOverride:
    def test_banks_the_ids_against_the_injected_repo(self):
        storage = pap_storage()

        with patch("services.pap_geocode.remember_manual_geo_ids") as mock_remember:
            assert PapParser(storage=storage).remember_manual_override(manual_criteria()) is None

        mock_remember.assert_called_once_with(manual_criteria(), repo=storage.pap_geo)

    @pytest.mark.parametrize(
        "storage", [None, object()], ids=["sans_storage", "storage_sans_repo_geo"]
    )
    def test_without_a_repo_nothing_is_attempted(self, storage):
        with patch("services.pap_geocode.remember_manual_geo_ids") as mock_remember:
            assert PapParser(storage=storage).remember_manual_override(manual_criteria()) is None

        mock_remember.assert_not_called()

    def test_a_failure_is_logged_but_never_raised(self, logged):
        """Ce n'est qu'une optimisation : elle ne doit pas faire échouer la
        création de la recherche."""
        with patch(
            "services.pap_geocode.remember_manual_geo_ids",
            side_effect=RuntimeError("banque indisponible"),
        ):
            assert PapParser(storage=pap_storage()).remember_manual_override(manual_criteria()) is None

        assert any(
            level == "DEBUG" and "non mémorisé" in message and "banque indisponible" in message
            for level, message in logged
        )


class TestHasValidCriteria:
    def test_a_manual_geo_id_is_always_valid(self):
        assert PapParser().has_valid_criteria({"sourceOverrides": {"pap": {"geoIds": ["439"]}}}) is True

    @pytest.mark.parametrize(
        ("location", "case"),
        [
            (RENNES, "commune avec INSEE"),
            (make_whole_city_location("Poitiers"), "ville entière"),
            # Ville tapée à la main sans INSEE : la clé retombe sur le CP.
            ({"kind": "city", "city": "Montrouge", "postalCode": "92120"}, "commune sans INSEE"),
            (GIRONDE, "département"),
            (IDF, "région"),
        ],
        ids=["commune", "ville_entiere", "commune_sans_insee", "departement", "region"],
    )
    def test_a_perimeter_with_a_cache_key_is_valid(self, location, case):
        """TOUS les niveaux canoniques ont un identifiant natif PAP (contrairement
        à bienici/Century 21 où la région s'élargit) : une clé suffit."""
        assert PapParser().has_valid_criteria({"locations": [location]}) is True, case

    @pytest.mark.parametrize(
        "criteria",
        [
            {},
            {"locations": []},
            {"locations": [{"kind": "city", "city": "Paris"}]},
            {"locations": [{"kind": "department", "name": "Nawak"}]},
        ],
        ids=["vide", "liste_vide", "ville_sans_code", "dept_sans_code"],
    )
    def test_everything_else_is_invalid(self, criteria):
        assert PapParser().has_valid_criteria(criteria) is False

    def test_validation_never_resolves_anything(self):
        with patch("services.pap_geocode._resolve_uncached") as mock_resolve:
            assert PapParser().has_valid_criteria({"locations": [RENNES]}) is True

        mock_resolve.assert_not_called()


# ===========================================================================
# P2 — _fetch_with_retries : retries et backoff (aucun sleep réel : fixture slept)
# ===========================================================================


class TestFetchWithRetries:
    def test_a_200_returns_the_body(self, slept):
        session = FakeSession(["<html>ok</html>"])

        assert _fetch_with_retries(session, f"{BASE_URL}/x") == "<html>ok</html>"
        assert slept == [], "un premier essai réussi ne doit rien attendre"

    def test_a_404_is_a_definitive_bad_location(self, slept):
        session = FakeSession([FakeResponse("", status_code=404)])

        with pytest.raises(ValueError, match="localisation invalide"):
            _fetch_with_retries(session, f"{BASE_URL}/x")

        # Aucune retentative : le 404 est définitif.
        assert len(session.calls) == 1
        assert slept == []

    def test_a_403_cloudflare_challenge_is_retried_with_backoff(self, slept):
        """🔒 Cloudflare répond un 403 « Just a moment » aux empreintes non
        navigateur (capture ratée pap.html) ou en pic passager : backoff
        exponentiel avant de conclure, jamais de sleep réel en test."""
        session = FakeSession([
            FakeResponse("", status_code=403),
            FakeResponse("", status_code=403),
            FakeResponse("<html>enfin</html>"),
        ])

        assert _fetch_with_retries(session, f"{BASE_URL}/x") == "<html>enfin</html>"
        assert len(session.calls) == 3
        assert slept == [2.0, 4.0]

    def test_a_challenge_body_on_a_200_is_also_retried(self, slept):
        """Le marqueur « Just a moment » dans les 3000 premiers octets vaut
        challenge, même derrière un 200."""
        session = FakeSession([
            FakeResponse("<html>Just a moment...</html>"),
            FakeResponse("<html>vrai contenu</html>"),
        ])

        assert _fetch_with_retries(session, f"{BASE_URL}/x") == "<html>vrai contenu</html>"
        assert slept == [2.0]

    def test_exhausted_retries_raise_with_a_clear_cloudflare_reason(self, slept):
        session = FakeSession([FakeResponse("", status_code=403)])

        with pytest.raises(ValueError, match=r"page inaccessible après 3 tentatives .*challenge Cloudflare"):
            _fetch_with_retries(session, f"{BASE_URL}/x")

        assert len(session.calls) == 3
        assert slept == [2.0, 4.0]

    def test_a_network_error_is_retried(self, slept):
        def handler(url):
            if len(session.calls) < 3:
                raise CurlHTTPError("réseau coupé")
            return FakeResponse("<html>ok</html>")

        session = FakeSession(handler=handler)

        assert _fetch_with_retries(session, f"{BASE_URL}/x") == "<html>ok</html>"
        assert slept == [2.0, 4.0]

    def test_any_other_http_error_is_raised_immediately(self, slept):
        session = FakeSession([FakeResponse("", status_code=500)])

        with pytest.raises(CurlHTTPError, match="500 Server Error"):
            _fetch_with_retries(session, f"{BASE_URL}/x")

        assert len(session.calls) == 1
        assert slept == []


# ===========================================================================
# P1 — scrape : garde-fous d'entrée et session
# ===========================================================================


class TestScrapeGuards:
    @pytest.mark.parametrize(
        "criteria",
        [{}, {"locations": []}, {"locations": [{"kind": "city", "city": "Paris"}]}],
        ids=["vide", "liste_vide", "localisation_incomplete"],
    )
    def test_no_location_raises_before_any_request(self, criteria):
        with pytest.raises(ValueError, match="nécessite au moins une localisation"):
            PapParser().scrape(criteria)

    def test_no_resolvable_location_raises(self, logged):
        """Sans storage, aucun identifiant ne peut être résolu : le scrape le
        dit plutôt que de chercher à vide."""
        with pytest.raises(ValueError, match="Aucune localisation PAP exploitable"):
            PapParser().scrape({"locations": [RENNES], "transaction": "rent"})

        assert any("Aucun storage fourni au parser" in m for _, m in logged if _ == "WARNING")

    def test_one_bad_series_never_discards_the_others(self, logged):
        """Une série en échec (404) n'annule pas les autres : les annonces des
        séries saines sont gardées, l'échec est tracé."""
        def handler(url):
            if "locations-maison-" in url:
                return FakeResponse("", status_code=404)
            return FakeResponse(RENNES_P1)

        session = FakeSession(handler=handler)
        criteria = manual_criteria(propertyTypes=["apartment", "house"])

        listings = run_scrape(criteria, session)

        assert [li.legacy_id for li in listings] == RENNES_UIDS
        assert any(level == "WARNING" and "Rennes (35000) (g43618)" in message for level, message in logged)

    def test_a_failed_fused_series_reports_every_perimeter_of_the_block(self, logged):
        """Quand LA série fusionnée échoue (404), le message trace TOUS les
        périmètres du bloc g — plus une seule localisation (issue #9)."""
        session = FakeSession([FakeResponse("", status_code=404)])

        with pytest.raises(ValueError, match="localisation invalide"):
            run_scrape(fused_criteria(), session)

        assert any(
            level == "WARNING"
            and "Paris (75015), Rennes (35000) (g37782g43618)" in message
            for level, message in logged
        )

    def test_it_raises_only_when_every_series_fails(self):
        session = FakeSession(handler=lambda url: FakeResponse("", status_code=404))
        criteria = manual_criteria(geo_id="99999")

        with pytest.raises(ValueError, match="localisation invalide"):
            run_scrape(criteria, session)


class TestScrapeSession:
    def test_the_session_announces_a_browser_with_the_right_headers(self):
        """L'empreinte TLS navigateur est passée au constructeur (sans elle,
        Cloudflare répond 403) et la session porte ses headers."""
        session = FakeSession([RENNES_P1])

        with patch("parsers.pap.curl_requests.Session", return_value=session) as ctor:
            PapParser().scrape(manual_criteria())

        assert ctor.call_args.kwargs["impersonate"] == IMPERSONATE
        assert session.headers["Accept"] == "text/html, application/xhtml+xml"
        assert session.headers["Accept-Language"] == "fr-FR,fr;q=0.9"
        assert session.headers["Referer"] == f"{BASE_URL}/"

    def test_every_request_is_bounded_by_a_timeout(self):
        session = FakeSession([RENNES_P1])
        run_scrape(manual_criteria(), session)

        assert session.calls
        assert all(call["timeout"] == 15 for call in session.calls)


# ===========================================================================
# P1 — pagination (_collect_pages) : arrêt, dédup, plafonds
# ===========================================================================


class TestPagination:
    def test_it_stops_on_a_recycled_page_without_any_warning(self, logged):
        """🔒 Fin de série réelle : rennes p1 puis p3 (qui RECYCLE p1 — mêmes
        10 uids) -> zéro inédit dès la 2e page, arrêt SANS warning
        (pages_lues=2 < _DEPTH_CAP_SUSPECT), et 10 annonces retenues."""
        session = FakeSession([RENNES_P1, RENNES_P3_RECYCLEE])

        listings = run_scrape(manual_criteria(), session)

        assert [li.legacy_id for li in listings] == RENNES_UIDS
        assert len(session.calls) == 2
        assert not any(level == "WARNING" for level, _ in logged)

    def test_the_page_url_suffix_starts_at_two_and_matches_the_capture(self):
        session = FakeSession([RENNES_P1])
        run_scrape(manual_criteria(), session)

        base = f"{BASE_URL}/annonce/{_RENNES_SERIE}"
        assert session.urls == [base, f"{base}-2"]

    def test_the_pagination_suffix_applies_to_the_fused_url(self):
        """La pagination -2/-3 s'applique normalement aux URLs fusionnées
        (vérifié en direct, issue #9) : le suffixe suit le bloc g COMPLET."""
        session = FakeSession([page(card(uid="111")), EMPTY_PAGE_HTML])

        run_scrape(fused_criteria(), session)

        base = f"{BASE_URL}/annonce/locations-appartement-paris-g37782g43618"
        assert session.urls == [base, f"{base}-2"]

    def test_the_inter_page_delay_is_paid_once_per_extra_page(self, slept):
        """Le délai anti-burst (0,3 s) ne se paie jamais avant la page 1."""
        session = FakeSession([RENNES_P1])
        run_scrape(manual_criteria(), session)

        assert slept == [0.3]

    def test_overlapping_pages_are_kept_once(self):
        """Chevauchement inter-pages réel (2-6 uids déjà vus par page dès
        p4-p6 sur les captures profondes) : chaque annonce n'apparaît qu'une
        fois, la série continue tant qu'il reste de l'inédit."""
        session = FakeSession([
            page(card(uid="111"), card(uid="222")),
            page(card(uid="222"), card(uid="333")),
            page(card(uid="333")),
        ])

        listings = run_scrape(manual_criteria(geo_id="439", locations=[PARIS_WHOLE], priceMax=None), session)

        assert [li.legacy_id for li in listings] == ["111", "222", "333"]
        assert len(session.calls) == 3

    def test_an_intra_page_duplicate_creates_only_one_listing(self, logged):
        """Doublon intra-page réel (r401201732 ×2 sur paris filtré p1) : la
        2e occurrence est sautée par vues_ici sans créer de Listing. Les
        captures ne doublent que des cartes sans CP — HTML reconstruit avec
        une carte DOUBLÉE portant un code postal."""
        duplicated = card(uid="401201732", line="Paris 19E (75019)")
        session = FakeSession([page(duplicated, card(uid="464801629"), duplicated)])

        listings = run_scrape(
            manual_criteria(geo_id="439", locations=[PARIS_WHOLE], priceMax=None), session
        )

        assert [li.legacy_id for li in listings] == ["401201732", "464801629"]

    def test_cards_without_zip_are_excluded_from_the_new_count(self):
        """Les cartes sans CP (« Paris 15E » nu) sont écartées en aval mais
        AUSSI du décompte des « nouvelles » : une page qui n'alimenterait
        plus que ça doit terminer le parcours."""
        calls: list[int] = []

        def fetch(page_num: int):
            calls.append(page_num)
            if page_num == 1:
                return page(card(uid="111"))
            return page(card(uid="222", line="Paris 15E"), card(uid="223", line="Paris 16E"))

        listings = PapParser()._collect_pages(fetch, {}, [PARIS_WHOLE], set(), "test")

        assert [li.legacy_id for li in listings] == ["111"]
        assert calls == [1, 2], "arrêt dès la page sans aucune nouvelle dans le périmètre"

    def test_nearby_ads_interleaved_then_alone_end_the_walk(self):
        """Arrêt « plus rien d'inédit DANS LE PÉRIMÈTRE » : PAP intercale des
        annonces proches HORS périmètre (marquées « à X km » puis non
        marquées — marqueur jamais observé en live, HTML reconstruit). Elles
        ne comptent pas comme nouvelles ; quand elles sont seules, la série
        se termine au lieu d'errer jusqu'au plafond."""
        vanves = card(uid="555", line="Vanves (92170)", price="1.200 €")
        calls: list[int] = []

        def fetch(page_num: int):
            calls.append(page_num)
            if page_num == 1:
                return page(card(uid="111"))
            if page_num == 2:
                # Intercalées : une proche marquée, une proche non marquée,
                # et UNE vraie nouvelle du périmètre.
                return page(PROXIMITY_CARD, vanves, card(uid="222"))
            return page(vanves, PROXIMITY_CARD)

        listings = PapParser()._collect_pages(fetch, {}, [PARIS_WHOLE], set(), "test")

        assert [li.legacy_id for li in listings] == ["111", "222"]
        assert calls == [1, 2, 3]

    def test_the_recycled_depth_cap_page_warns_about_partial_results(self, logged):
        """🔒 Plafond de profondeur serveur confirmé en direct : après 25 pages,
        la VRAIE capture g439 page26 ne recycle que du déjà-vu -> arrêt +
        WARNING _DEPTH_CAP_SUSPECT (pages_lues >= 20), résultat possiblement
        partiel. Les pages 1..25 sont synthétiques (une carte inédite chacune),
        puis c'est la capture réelle qui est servie en boucle par le double."""
        calls: list[int] = []

        def fetch(page_num: int):
            calls.append(page_num)
            if page_num < 26:
                return page(card(uid=str(100000000 + page_num)))
            return PARIS_G439_P26_RECYCLEE

        listings = PapParser()._collect_pages(fetch, {}, [PARIS_WHOLE], set(), "g439")

        # 25 synthétiques + les cartes à CP inédites servies au premier passage
        # sur la page recyclée (9 sur 14 fiches ; les 5 sans CP restent hors
        # décompte), puis 0 inédit au passage suivant.
        assert len(listings) == 34
        assert len(calls) == 27
        warnings = [m for level, m in logged if level == "WARNING" and "plafond de profondeur" in m]
        assert len(warnings) == 1
        assert "page 27" in warnings[0]
        assert "résultat possiblement partiel" in warnings[0]

    def test_the_max_pages_limit_is_enforced_and_reported(self, logged):
        """MAX_PAGES borne le parcours même si le site sert du neuf pour
        toujours (60 pages ~ 900 cartes chez PAP)."""
        calls: list[int] = []

        def fetch(page_num: int):
            calls.append(page_num)
            return page(card(uid=str(200000000 + page_num)))

        listings = PapParser()._collect_pages(fetch, {}, [PARIS_WHOLE], set(), "test")

        assert len(calls) == MAX_PAGES
        assert len(listings) == MAX_PAGES
        assert any(
            level == "WARNING" and f"limite de {MAX_PAGES} pages atteinte" in message
            for level, message in logged
        )


# ===========================================================================
# P1 — scrape : filtrage et déduplication multi-séries
# ===========================================================================


class TestScrapeFilters:
    def test_out_of_bounds_prices_served_anyway_are_dropped(self):
        """🔒 Capture paris filtrée p2 : le site sert 890 €, 900 € (< min) et
        2.050 € (> max) MALGRÉ le filtre URL -entre-1000-et-2000-euros —
        _passes_filters rejoue les critères et les rejette tous."""
        session = FakeSession([PARIS_FILTRE_P2_HORS_BORNES])

        listings = run_scrape(paris_criteria(), session)

        assert [li.legacy_id for li in listings] == [
            "461200931", "460000098", "464102076", "448403113", "457202584",
        ]
        assert {li.price_value for li in listings} == {1550.0, 1200.0, 1850.0, 1150.0, 1430.0}
        assert all(li.price_value not in (890.0, 900.0, 2050.0) for li in listings)

    def test_out_of_area_cards_are_dropped(self):
        """Sur paris filtré p1 prise comme périmètre Paris 15e SEUL : seules
        les cartes à CP 75015 ressortent, les 75018 sont écartées."""
        session = FakeSession([PARIS_FILTRE_P1])

        listings = run_scrape(paris_criteria(locations=[PARIS_15]), session)

        assert [li.legacy_id for li in listings] == ["464800808", "441302329"]

    def test_a_fused_series_filters_against_every_perimeter_of_the_block(self):
        """La série fusionnée sert TOUS les périmètres du bloc g : les cartes
        de Rennes ET de Paris 15e passent, une carte hors des deux est
        écartée — _passes_filters reçoit l'union complète (issue #9 : rien n'a
        changé côté filtrage, seule l'émission des URLs a fusionné)."""
        cards = page(
            card(uid="111", line="Rennes (35000)"),
            card(uid="222", line="Paris 15E (75015)"),
            card(uid="333", line="Vanves (92170)"),
        )
        session = FakeSession([cards])

        listings = run_scrape(fused_criteria(), session)

        assert [li.zip_code for li in listings] == ["35000", "75015"]


class TestMultiSeriesDedup:
    def test_a_uid_scraped_by_one_type_series_is_never_relisted_by_another(self):
        """Double dédup : vues_ici déduplique À L'INTÉRIEUR d'une série, le
        set seen GLOBAL absorbe le recouvrement ENTRE séries — depuis la
        fusion (#9) les séries se distinguent par TYPE (les localisations,
        elles, vivent ensemble dans un seul bloc g)."""
        serie_appartement = page(card(uid="111"), card(uid="222"))
        serie_maison = page(card(uid="222"), card(uid="333"))

        def handler(url):
            if "locations-maison-" in url:
                return FakeResponse(serie_maison)
            return FakeResponse(serie_appartement)

        session = FakeSession(handler=handler)
        criteria = manual_criteria(
            geo_id="439",
            propertyTypes=["apartment", "house"],
            priceMax=None,
        )
        criteria["locations"] = [PARIS_WHOLE]

        listings = run_scrape(criteria, session)

        assert [li.listing_id for li in listings] == ["pap_111", "pap_222", "pap_333"]


