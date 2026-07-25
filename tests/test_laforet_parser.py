"""Tests for parsers/laforet.py."""
from unittest.mock import MagicMock, patch

import pytest

from models.listing import Listing
from core.geocode import _arrondissement_insee_code
from parsers.laforet import (
    LaforetParser,
    _dict_to_listing,
    _extract_genuine_section,
    _parse_cards,
    _passes_filters,
    _slugify,
)

SAMPLE_PAGE_HTML = """
<html><body>
<script type="application/ld+json">
[{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[]},
 {"@context":"https://schema.org","@type":"ItemList","itemListElement":[
    {"@type":"ListItem","position":1,"url":"/ville/location-appartement-paris-75000"},
    {"@type":"ListItem","position":2,"url":"/ville/location-appartement-paris-75000?page=2"},
    {"@type":"ListItem","position":3,"url":"/ville/location-appartement-paris-75000?page=3"}
 ]}]
</script>
<article>
  <a href="https://www.laforet.com/agence-immobiliere/paris18marxdormoy/louer/paris-18/appartement-1-piece-52811904" target="_blank">photo</a>
  <h3>Appartement <span>1 021 &#8364;/mois</span> <span>PARIS (75018)</span></h3>
  <div>30 m²&nbsp;&bull;&nbsp;1 pi&egrave;ce</div>
</article>
<article>
  <a href="https://www.laforet.com/agence-immobiliere/paris15lourmel/louer/paris-15/appartement-2-pieces-52805433" target="_blank">photo</a>
  <h3>Appartement <span>1 513 &#8364;/mois</span> <span>PARIS (75015)</span></h3>
  <div>50 m²&nbsp;&bull;&nbsp;2 pi&egrave;ces</div>
</article>
</body></html>
"""

# A merged multi-city response (as produced by filter[cities][]) has both
# cities' genuine cards in the same section — unlike SAMPLE_PAGE_HTML, which
# models a single-city page where the 75015 card is backfill noise.
MERGED_PAGE_HTML = """
<html><body>
<article>
  <a href="https://www.laforet.com/agence-immobiliere/paris18marxdormoy/louer/paris-18/appartement-1-piece-52811904" target="_blank">photo</a>
  <h3>Appartement <span>1 021 &#8364;/mois</span> <span>PARIS (75018)</span></h3>
  <div>30 m²&nbsp;&bull;&nbsp;1 pi&egrave;ce</div>
</article>
<article>
  <a href="https://www.laforet.com/agence-immobiliere/lyon7agence/louer/lyon-07/appartement-2-pieces-99999999" target="_blank">photo</a>
  <h3>Appartement <span>900 &#8364;/mois</span> <span>LYON (69007)</span></h3>
  <div>40 m²&nbsp;&bull;&nbsp;2 pi&egrave;ces</div>
</article>
</body></html>
"""

# Reproduces a real bug found live: when a city has thin inventory, Laforet
# backfills the page with "nearby agency office" cards. Their link
# (/agence-immobiliere/lyon-7) has no listing content but still ends in
# "-<digit>" (the arrondissement number in the agency's own slug), which a
# looser regex misidentified as a listing detail URL.
PAGE_WITH_AGENCY_OFFICE_CARD = """
<html><body>
<article>
  <a href="https://www.laforet.com/agence-immobiliere/lyon-7" target="_blank">Agence Laforêt LYON 7</a>
  <div>Fermé — 55 avenue Jean Jaurès, 69007 LYON</div>
</article>
</body></html>
"""


@pytest.fixture(autouse=True)
def _clear_insee_cache():
    from core.geocode import _INSEE_CACHE
    _INSEE_CACHE.clear()
    yield
    _INSEE_CACHE.clear()


class TestSlugify:
    def test_lowercases_and_strips_accents(self):
        assert _slugify("Paris") == "paris"
        assert _slugify("Le Kremlin-Bicêtre") == "le-kremlin-bicetre"
        assert _slugify("Île-de-France") == "ile-de-france"

    def test_collapses_non_alnum_to_single_hyphen(self):
        assert _slugify("Charenton-le-Pont") == "charenton-le-pont"
        assert _slugify("  Saint  Mandé  ") == "saint-mande"


class TestBuildSearchUrl:
    """build_search_url()/build_search_urls() mirror scrape()'s own merge
    strategy (see TestBuildSearchUrls) — every location resolvable to an
    INSEE code produces one combined URL, exactly what's actually fetched,
    not a plain per-location link. Paris/Lyon/Marseille resolve via the
    pure arrondissement formula (no network call needed in these tests)."""

    def test_returns_none_without_city_or_postal_code(self):
        parser = LaforetParser()
        assert parser.build_search_url({}) is None
        assert parser.build_search_url({"city": "Paris"}) is None

    def test_rent_apartment_url_includes_merge_filters(self):
        parser = LaforetParser()
        url = parser.build_search_url({"city": "Paris", "postalCode": "75018"})
        assert url == (
            "https://www.laforet.com/ville/location-appartement-paris-75018"
            "?filter%5Btypes%5D%5B%5D=apartment&filter%5Bcities%5D%5B%5D=75118"
        )

    def test_sale_house_url(self):
        parser = LaforetParser()
        url = parser.build_search_url({
            "city": "Lyon", "postalCode": "69007",
            "transaction": "buy", "propertyTypes": ["house"],
        })
        assert url == (
            "https://www.laforet.com/ville/achat-maison-lyon-69007"
            "?filter%5Btypes%5D%5B%5D=house&filter%5Bcities%5D%5B%5D=69387"
        )

    def test_several_property_types_are_merged_into_one_url(self):
        """filter[types][] est répétable et prime sur le type du slug —
        vérifié en live, une seule requête suffit pour appartements + maisons."""
        parser = LaforetParser()
        url = parser.build_search_url({
            "city": "Lyon", "postalCode": "69007",
            "propertyTypes": ["apartment", "house"],
        })
        assert "filter%5Btypes%5D%5B%5D=apartment" in url
        assert "filter%5Btypes%5D%5B%5D=house" in url

    def test_unsupported_property_type_is_reported_not_raised(self):
        """Régression : un type de bien que Laforet ne référence pas levait
        une ValueError jusque dans la reconstruction d'URL (ce qui renvoyait
        un 500 sur /api/searches/<id>/urls). Il est maintenant annoncé par
        cannot_search_reason() avant tout scrape."""
        parser = LaforetParser()
        criteria = {"city": "Paris", "postalCode": "75018", "propertyTypes": ["parking"]}

        reason = parser.cannot_search_reason(criteria)
        assert reason and "Parking" in reason
        # Plus d'exception, et surtout AUCUNE url : montrer un lien vers des
        # appartements à qui demande un parking serait un faux résultat.
        assert parser.build_search_url(criteria) is None
        assert parser.build_search_urls(criteria) == []

    def test_a_mixed_request_still_searches_the_supported_types(self):
        """« appartement + parking » doit tout de même ramener les
        appartements, pas échouer en entier."""
        parser = LaforetParser()
        url = parser.build_search_url({
            "city": "Paris", "postalCode": "75018",
            "propertyTypes": ["apartment", "parking"],
        })
        assert "filter%5Btypes%5D%5B%5D=apartment" in url
        assert "parking" not in url

    def test_unresolvable_location_falls_back_to_a_plain_url(self):
        """A postal code that can't be resolved to an INSEE code (unknown
        geo API, foreign postal code) gets a plain link instead — matching
        scrape()'s own per-location fallback, not a broken merged link."""
        parser = LaforetParser()
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            url = parser.build_search_url({"city": "Paris", "postalCode": "75018"})
        assert url == "https://www.laforet.com/ville/location-appartement-paris-75018"
        assert "filter" not in url

    def test_build_search_url_returns_the_merged_url_for_several_locations(self):
        parser = LaforetParser()
        url = parser.build_search_url({"locations": [
            {"city": "Paris", "postalCode": "75014"},
            {"city": "Lyon", "postalCode": "69007"},
        ]})
        assert url == (
            "https://www.laforet.com/ville/location-appartement-paris-75014"
            "?filter%5Btypes%5D%5B%5D=apartment&filter%5Bcities%5D%5B%5D=75114&filter%5Bcities%5D%5B%5D=69387"
        )


class TestBuildSearchUrls:
    """A search can span several cities/postal codes — reflects the real
    scraping strategy: resolvable locations become one merged URL (what
    "Voir l'URL" should show, since that's what's actually fetched), only
    an unresolvable one gets its own separate plain URL."""

    def test_resolvable_locations_produce_one_merged_url(self):
        parser = LaforetParser()
        urls = parser.build_search_urls({"locations": [
            {"city": "Paris", "postalCode": "75014"},
            {"city": "Lyon", "postalCode": "69007"},
        ]})
        assert len(urls) == 1
        assert urls[0].startswith("https://www.laforet.com/ville/location-appartement-paris-75014?")
        assert "filter%5Bcities%5D%5B%5D=75114" in urls[0]
        assert "filter%5Bcities%5D%5B%5D=69387" in urls[0]

    def test_single_legacy_location_still_works(self):
        parser = LaforetParser()
        urls = parser.build_search_urls({"city": "Paris", "postalCode": "75018"})
        assert len(urls) == 1
        assert urls[0].startswith("https://www.laforet.com/ville/location-appartement-paris-75018?")

    def test_unresolvable_location_gets_its_own_plain_url(self):
        parser = LaforetParser()
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            urls = parser.build_search_urls({"city": "Paris", "postalCode": "75018"})
        assert urls == ["https://www.laforet.com/ville/location-appartement-paris-75018"]

    def test_mix_of_resolvable_and_unresolvable_locations(self):
        parser = LaforetParser()
        with patch("parsers.laforet._resolve_insee_code", side_effect=_arrondissement_insee_code):
            urls = parser.build_search_urls({"locations": [
                {"city": "Paris", "postalCode": "75014"},   # resolves
                {"city": "Nawak", "postalCode": "99999"},   # doesn't
            ]})
        assert len(urls) == 2
        assert "filter%5Bcities%5D%5B%5D=75114" in urls[0]
        assert urls[1] == "https://www.laforet.com/ville/location-appartement-nawak-99999"

    def test_empty_without_any_location(self):
        assert LaforetParser().build_search_urls({}) == []


class TestHasValidCriteria:
    def test_requires_city_and_postal_code(self):
        parser = LaforetParser()
        assert parser.has_valid_criteria({"city": "Paris", "postalCode": "75018"})
        assert not parser.has_valid_criteria({"city": "Paris"})
        assert not parser.has_valid_criteria({})

    def test_multiple_locations(self):
        parser = LaforetParser()
        assert parser.has_valid_criteria({"locations": [
            {"city": "Paris", "postalCode": "75014"},
            {"city": "Lyon", "postalCode": "69007"},
        ]})


class TestExtractGenuineSection:
    """Laforet always appends a second "Appartements à proximité de {ville}"
    section with backfill noise after the real results — must never be
    parsed as part of the results (verified live: this is what let listings
    from unrelated cities contaminate a search)."""

    def test_truncates_before_the_nearby_marker(self):
        html = "GENUINEproximité de Paris 14</div>NOISE"
        assert _extract_genuine_section(html) == "GENUINE"

    def test_returns_whole_html_when_marker_absent(self):
        html = "<html>no marker here, plenty of native inventory</html>"
        assert _extract_genuine_section(html) == html

    def test_other_listings_heading_must_not_truncate(self):
        """Garde-fou : la page porte aussi un titre « Autres annonces », qui
        ressemble à un début de section de remplissage mais découpe en réalité
        LES RÉSULTATS eux-mêmes en plusieurs blocs.

        Couper dessus paraît prudent et fait perdre la majorité des annonces.
        Mesuré sur 12 recherches réelles, en prenant pour arbitre le compteur
        que le site affiche lui-même :

            recherche             site   "proximité"   "Autres annonces"
            Lille location          15        15 ok            3  faux
            Toulouse achat maison   11        11 ok            1  faux
            Marseille 8e achat      16        16 ok            7  faux

        Ce test échoue si quelqu'un « corrige » à nouveau dans ce sens.
        """
        html = "RESULTAT-BLOC-1<h2>Autres annonces</h2>RESULTAT-BLOC-2"
        assert _extract_genuine_section(html) == html

    def test_real_result_cards_survive_an_other_listings_heading(self):
        """Concrètement : les cartes situées après « Autres annonces » sont des
        résultats et doivent ressortir."""
        html = (
            '<article><a href="https://www.laforet.com/agence-immobiliere/lille/louer/'
            'lille/appartement-2-pieces-11111111">x</a>'
            '<h3>Appartement <span>700 €/mois</span> <span>LILLE (59000)</span></h3>'
            '<div>40 m² • 2 pièces</div></article>'
            '<h2>Autres annonces</h2>'
            '<article><a href="https://www.laforet.com/agence-immobiliere/lille/louer/'
            'lille/appartement-3-pieces-22222222">y</a>'
            '<h3>Appartement <span>900 €/mois</span> <span>LILLE (59000)</span></h3>'
            '<div>60 m² • 3 pièces</div></article>'
        )
        refs = {c["reference"] for c in _parse_cards(_extract_genuine_section(html))}
        assert refs == {"11111111", "22222222"}


class TestParseCards:
    def test_listing_link_with_an_anchor_is_still_recognised(self):
        """Régression : Laforet lie parfois une section de la page de l'annonce
        (`...-52637604#section-video` quand elle a une vidéo). Le motif étant
        ancré sur la fin, ces annonces étaient purement ignorées — constaté en
        live sur Rennes, 6 annonces récupérées pour 7 annoncées par le site."""
        html = (
            '<article><a href="https://www.laforet.com/agence-immobiliere/rennes/acheter/'
            'rennes/maison-11-pieces-52637604#section-video">x</a>'
            '<h3>Maison <span>560 000 €</span> <span>RENNES (35000)</span></h3>'
            '<div>200 m² • 11 pièces</div></article>'
        )
        cards = _parse_cards(html)
        assert len(cards) == 1
        assert cards[0]["reference"] == "52637604"
        # L'ancre ne doit pas rester dans l'URL stockée.
        assert "#" not in cards[0]["url"]

    def test_listing_link_with_a_query_string_is_still_recognised(self):
        html = (
            '<article><a href="https://www.laforet.com/agence-immobiliere/rennes/acheter/'
            'rennes/maison-4-pieces-12345678?utm_source=x">y</a>'
            '<h3>Maison <span>300 000 €</span> <span>RENNES (35000)</span></h3>'
            '<div>90 m² • 4 pièces</div></article>'
        )
        cards = _parse_cards(html)
        assert len(cards) == 1
        assert cards[0]["reference"] == "12345678"

    def test_agency_office_card_is_still_rejected(self):
        """Le nettoyage de l'URL ne doit pas rouvrir la porte aux cartes
        d'agence (bug corrigé par 53d792d) : /agence-immobiliere/lyon-7 finit
        aussi par -<chiffre> mais n'est pas une annonce."""
        cards = _parse_cards(PAGE_WITH_AGENCY_OFFICE_CARD)
        assert cards == []

    def test_extracts_expected_fields(self):
        cards = _parse_cards(SAMPLE_PAGE_HTML)
        assert len(cards) == 2
        assert cards[0]["reference"] == "52811904"
        assert cards[0]["city"] == "PARIS"
        assert cards[0]["zip_code"] == "75018"
        assert cards[0]["surface"] == "30"
        assert cards[0]["rooms"] == "1"
        assert cards[0]["price_value"] == 1021.0
        assert cards[0]["agency"] == "paris18marxdormoy"

    def test_second_card_handles_plural_pieces(self):
        cards = _parse_cards(SAMPLE_PAGE_HTML)
        assert cards[1]["rooms"] == "2"
        assert cards[1]["price_value"] == 1513.0

    def test_ignores_nearby_agency_office_cards(self):
        """A nearby-agency-office filler card must never be mistaken for a
        listing, even though its own link ends in "-<digit>" too (verified
        live against Lyon: this produced a fake "listing" that was just a
        link to the agency's own page, with no price/surface/location)."""
        cards = _parse_cards(PAGE_WITH_AGENCY_OFFICE_CARD)
        assert cards == []


class TestDictToListing:
    def test_maps_to_common_listing_schema(self):
        cards = _parse_cards(SAMPLE_PAGE_HTML)
        listing = _dict_to_listing(cards[0])
        assert listing.listing_id == "lf_52811904"
        assert listing.source == "laforet"
        assert listing.city == "PARIS"
        assert listing.zip_code == "75018"
        assert listing.price_value == 1021.0


class TestWideAreaSearches:
    """Périmètres plus larges qu'une commune. Vérifié en live que Laforet les
    couvre en une seule requête : filter[departments][] est répétable (les 8
    départements d'Île-de-France rendent 741 annonces) et se combine en UNION
    avec filter[cities][] (cities=33063 rend 149 annonces, departments=75 en
    rend 824, les deux ensemble 973)."""

    GIRONDE = {"kind": "department", "name": "Gironde", "code": "33"}
    IDF = {"kind": "region", "name": "Île-de-France", "code": "11",
           "departments": ["75", "77", "78", "91", "92", "93", "94", "95"]}

    def test_department_uses_the_departments_filter(self):
        parser = LaforetParser()
        with patch("parsers.laforet.department_main_city",
                   return_value={"city": "Bordeaux", "postalCode": "33000"}):
            url = parser.build_search_url({"locations": [self.GIRONDE]})
        assert "filter%5Bdepartments%5D%5B%5D=33" in url
        # Le chemin est ancré sur la ville principale du département, pour
        # rester lisible — il n'a aucun effet sur le résultat.
        assert url.startswith("https://www.laforet.com/ville/location-appartement-bordeaux-33000?")
        assert "filter%5Bcities%5D" not in url

    def test_region_is_expressed_as_its_departments(self):
        """Une région est traduite en ses départements. Laforet a bien un filtre
        région natif (filter[regions][]=11, même résultat à l'annonce près) mais
        les départements rendent le périmètre explicite dans l'URL."""
        parser = LaforetParser()
        with patch("parsers.laforet.department_main_city",
                   return_value={"city": "Paris", "postalCode": "75001"}):
            url = parser.build_search_url({"locations": [self.IDF]})
        for dept in self.IDF["departments"]:
            assert f"filter%5Bdepartments%5D%5B%5D={dept}" in url
        assert "filter%5Bregions%5D" not in url

    def test_region_without_stored_departments_is_resolved(self):
        """Une région enregistrée sans ses départements (saisie manuelle, ou
        format antérieur) doit les retrouver plutôt que d'abandonner."""
        parser = LaforetParser()
        with patch("parsers.laforet.region_departments", return_value=["2A", "2B"]) as mock_reg:
            with patch("parsers.laforet.department_main_city",
                       return_value={"city": "Ajaccio", "postalCode": "20000"}):
                url = parser.build_search_url({"locations": [
                    {"kind": "region", "name": "Corse", "code": "94"},
                ]})
        mock_reg.assert_called_with("94")
        assert "filter%5Bdepartments%5D%5B%5D=2A" in url
        assert "filter%5Bdepartments%5D%5B%5D=2B" in url
        assert url.startswith("https://www.laforet.com/ville/location-appartement-ajaccio-20000?")

    def test_whole_city_uses_the_single_commune_code(self):
        """« Paris — toute la ville » doit utiliser LE code de la commune
        (75056), que Laforet comprend directement : vérifié en live qu'il rend
        exactement le même résultat que l'énumération des 20 arrondissements
        (66 annonces dans les deux cas)."""
        parser = LaforetParser()
        url = parser.build_search_url({"locations": [{
            "kind": "whole_city", "city": "Paris", "inseeCode": "75056",
            "postalCodes": ["75001", "75002", "75015"],
        }]})
        assert "filter%5Bcities%5D%5B%5D=75056" in url
        # Aucun code d'arrondissement : on n'énumère plus.
        for insee in ("75101", "75102", "75115"):
            assert insee not in url

    def test_whole_city_falls_back_to_postal_codes_without_a_commune_code(self):
        """Localisation enregistrée sans code de commune : les codes des codes
        postaux restent équivalents, mieux que pas de filtre."""
        parser = LaforetParser()
        url = parser.build_search_url({"locations": [{
            "kind": "whole_city", "city": "Paris",
            "postalCodes": ["75001", "75015"],
        }]})
        assert "filter%5Bcities%5D%5B%5D=75101" in url
        assert "filter%5Bcities%5D%5B%5D=75115" in url

    def test_levels_are_combined_into_a_single_url(self):
        """Communes et départements dans la même recherche : une seule requête,
        puisque le site les combine en union."""
        parser = LaforetParser()
        with patch("parsers.laforet.department_main_city",
                   return_value={"city": "Bordeaux", "postalCode": "33000"}):
            urls = parser.build_search_urls({"locations": [
                self.GIRONDE,
                {"city": "Paris", "postalCode": "75014"},
            ]})
        assert len(urls) == 1
        assert "filter%5Bdepartments%5D%5B%5D=33" in urls[0]
        assert "filter%5Bcities%5D%5B%5D=75114" in urls[0]

    def test_a_department_is_usable_without_any_city(self):
        """Une recherche départementale n'a ni ville ni code postal : elle doit
        rester valide (c'était le contrat par défaut de BaseParser)."""
        parser = LaforetParser()
        criteria = {"locations": [self.GIRONDE]}
        assert parser.has_valid_criteria(criteria) is True
        assert parser.cannot_search_reason(criteria) is None

    def test_no_url_when_the_department_city_cannot_be_found(self):
        """Sans ville pour ancrer le chemin, Laforet renvoie 404 : mieux vaut
        aucune URL qu'un lien mort."""
        parser = LaforetParser()
        with patch("parsers.laforet.department_main_city", return_value=None):
            assert parser.build_search_urls({"locations": [self.GIRONDE]}) == []


class TestPassesFilters:
    def _listing(self, **overrides):
        base = dict(
            listing_id="lf_1", url="https://x", price_value=1000.0,
            surface="50", rooms="2", zip_code="75014",
        )
        base.update(overrides)
        return Listing(**base)

    @staticmethod
    def _at(*postal_codes):
        """Des périmètres au niveau code postal, le cas le plus courant."""
        return [{"kind": "city", "city": "X", "postalCode": cp} for cp in postal_codes]

    def test_price_range(self):
        listing = self._listing(price_value=1000.0)
        allowed = self._at("75014")
        assert _passes_filters(listing, {"priceMin": 900, "priceMax": 1100}, allowed)
        assert not _passes_filters(listing, {"priceMax": 900}, allowed)
        assert not _passes_filters(listing, {"priceMin": 1100}, allowed)

    def test_surface_range(self):
        listing = self._listing(surface="50")
        allowed = self._at("75014")
        assert _passes_filters(listing, {"surfaceMin": 40, "surfaceMax": 60}, allowed)
        assert not _passes_filters(listing, {"surfaceMin": 60}, allowed)

    def test_rooms_exact_match(self):
        listing = self._listing(rooms="2")
        allowed = self._at("75014")
        assert _passes_filters(listing, {"rooms": ["2", "3"]}, allowed)
        assert not _passes_filters(listing, {"rooms": ["3", "4"]}, allowed)

    def test_rooms_five_plus(self):
        listing = self._listing(rooms="6")
        assert _passes_filters(listing, {"rooms": ["5"]}, self._at("75014"))

    def test_missing_data_does_not_exclude(self):
        listing = self._listing(price_value=None, surface="", rooms="")
        assert _passes_filters(listing, {"priceMin": 900, "surfaceMin": 40, "rooms": ["2"]}, self._at("75014"))

    def test_postal_code_must_be_in_allowed_set(self):
        listing = self._listing(zip_code="75014")
        assert _passes_filters(listing, {}, self._at("75014"))
        assert not _passes_filters(listing, {}, self._at("75015"))
        assert not _passes_filters(listing, {}, self._at("94230"))

    def test_multiple_allowed_postal_codes(self):
        """A merged multi-location search allows any of several postal codes."""
        allowed = self._at("75014", "92120")
        assert _passes_filters(self._listing(zip_code="75014"), {}, allowed)
        assert _passes_filters(self._listing(zip_code="92120"), {}, allowed)
        assert not _passes_filters(self._listing(zip_code="75015"), {}, allowed)

    def test_missing_zip_code_excludes(self):
        """Fail closed, not open: unlike price/surface/rooms, location
        correctness can't be waived just because a card's postal code
        couldn't be parsed — verified live this is exactly how a fake
        agency-office "listing" (no zip_code at all) slipped through."""
        listing = self._listing(zip_code="")
        assert not _passes_filters(listing, {}, {"75014"})


class TestScrape:
    """_resolve_insee_code is patched to the pure arrondissement formula in
    most of these (no network, deterministic) — postal codes outside
    Paris/Lyon/Marseille resolve to None, taking the per-location fallback
    path, exactly like a real geo-API miss would."""

    def test_raises_on_invalid_location(self):
        parser = LaforetParser()
        with pytest.raises(ValueError):
            parser.scrape({})

    def test_scrape_paginates_and_dedupes(self):
        parser = LaforetParser()
        page1_resp = MagicMock(status_code=200, text=SAMPLE_PAGE_HTML)
        page1_resp.raise_for_status.return_value = None

        with patch("requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.return_value = page1_resp
            mock_session_cls.return_value = mock_session
            listings = parser.scrape({"city": "Paris", "postalCode": "75018"})

        # SAMPLE_PAGE_HTML has one card in 75018 and one in 75015 — the
        # strict postal-code filter must keep only the requested one.
        assert len(listings) == 1
        assert listings[0].listing_id == "lf_52811904"

    def test_scrape_raises_clear_error_on_404(self):
        parser = LaforetParser()
        resp = MagicMock(status_code=404)
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.get.return_value = resp
                mock_session_cls.return_value = mock_session
                with pytest.raises(ValueError):
                    parser.scrape({"city": "Nawak", "postalCode": "99999"})

    def test_scrape_merges_multiple_locations_into_one_request(self):
        """Verified live: filter[cities][] genuinely merges several
        cities/postal codes into one correctly-scoped request+pagination —
        les périmètres partent ensemble, pas une requête par ville.

        Le nombre d'appels n'est pas 1 mais 2 : la pagination lit une page de
        plus pour constater qu'il n'y a rien de nouveau (voir _collect_pages).
        Ce qui compte est que TOUS les périmètres soient dans la même requête,
        ce que vérifient les paramètres transmis.
        """
        parser = LaforetParser()
        resp = MagicMock(status_code=200, text=MERGED_PAGE_HTML)
        resp.raise_for_status.return_value = None

        with patch("requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.return_value = resp
            mock_session_cls.return_value = mock_session
            listings = parser.scrape({"locations": [
                {"city": "Paris", "postalCode": "75018"},
                {"city": "Lyon", "postalCode": "69007"},
            ]})

        zip_codes = {l.zip_code for l in listings}
        assert zip_codes == {"75018", "69007"}
        assert len(listings) == 2

        # Une seule URL de base, portant les deux villes.
        urls = {call.args[0] for call in mock_session.get.call_args_list}
        assert len(urls) == 1
        cities = [
            value for call in mock_session.get.call_args_list
            for key, value in call.kwargs["params"] if key == "filter[cities][]"
        ]
        assert set(cities) == {"75118", "69387"}

    def test_pagination_continues_while_new_listings_appear(self):
        """Régression : la boucle s'arrêtait après la première page parce que le
        nombre de pages était lu dans un bloc JSON-LD ItemList qui DISPARAÎT dès
        qu'un filtre est envoyé.

        Constaté en live sur une recherche Île-de-France à 850-870 € et
        25-30 m² : 2 annonces retenues au lieu de 4 (les 97 résultats annoncés
        par le site tenaient sur 3 pages, on n'en lisait qu'une).
        """
        parser = LaforetParser()

        def card(ref, zip_code="75018"):
            return (
                f'<article><a href="https://www.laforet.com/agence-immobiliere/x/louer/'
                f'paris-18/appartement-1-piece-{ref}">p</a>'
                f'<h3>Appartement <span>900 €/mois</span> <span>PARIS ({zip_code})</span></h3>'
                f'<div>30 m² • 1 pièce</div></article>'
            )

        # Trois pages qui apportent chacune du neuf, puis une quatrième vide :
        # aucun ItemList nulle part, comme sur le vrai site avec des filtres.
        pages = [
            f"<html><body>{card('111')}{card('222')}</body></html>",
            f"<html><body>{card('222')}{card('333')}</body></html>",
            f"<html><body>{card('444')}</body></html>",
            f"<html><body>{card('444')}</body></html>",
        ]
        responses = []
        for html in pages:
            r = MagicMock(status_code=200, text=html)
            r.raise_for_status.return_value = None
            responses.append(r)

        with patch("requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.side_effect = responses
            mock_session_cls.return_value = mock_session
            listings = parser.scrape({"city": "Paris", "postalCode": "75018"})

        # Les quatre annonces, dont celles des pages 2 et 3.
        assert {li.legacy_id for li in listings} == {"111", "222", "333", "444"}
        # Et la boucle s'arrête à la page qui n'apporte plus rien.
        assert mock_session.get.call_count == 4

    def test_merged_request_sends_insee_codes_and_type_filter(self):
        parser = LaforetParser()
        resp = MagicMock(status_code=200, text=MERGED_PAGE_HTML)
        resp.raise_for_status.return_value = None

        with patch("requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.return_value = resp
            mock_session_cls.return_value = mock_session
            parser.scrape({"locations": [
                {"city": "Paris", "postalCode": "75018"},
                {"city": "Lyon", "postalCode": "69007"},
            ]})

        args, kwargs = mock_session.get.call_args
        assert args[0] == "https://www.laforet.com/ville/location-appartement-paris-75018"
        query_pairs = kwargs["params"]
        assert ("filter[types][]", "apartment") in query_pairs
        assert ("filter[cities][]", "75118") in query_pairs
        assert ("filter[cities][]", "69387") in query_pairs

    def test_falls_back_to_per_location_when_insee_resolution_fails(self):
        """A location whose postal code can't be resolved to an INSEE code
        (unknown geo API, foreign postal code) must not be silently
        dropped — it gets its own separate request instead."""
        parser = LaforetParser()
        page1_resp = MagicMock(status_code=200, text=SAMPLE_PAGE_HTML)
        page1_resp.raise_for_status.return_value = None

        with patch("parsers.laforet._resolve_insee_code", side_effect=_arrondissement_insee_code):
            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.get.return_value = page1_resp
                mock_session_cls.return_value = mock_session
                listings = parser.scrape({"locations": [
                    {"city": "Poitiers", "postalCode": "86000"},  # not special-cased -> None here
                ]})

        assert len(listings) == 0  # SAMPLE_PAGE_HTML has no 86000 card
        # falls back to _scrape_location (single-URL string, no params=)
        args, kwargs = mock_session.get.call_args
        assert "params" not in kwargs
        assert "poitiers-86000" in args[0]

    def test_rejected_card_in_fallback_path_is_not_blacklisted_from_another_location(self):
        """Real bug found live: a listing rejected as backfill noise while
        scraping one (fallback) location's own page must not be
        permanently blacklisted from being counted on a different
        location's page later in the same scrape."""
        parser = LaforetParser()

        loc_a_resp = MagicMock(status_code=200, text=SAMPLE_PAGE_HTML)
        loc_a_resp.raise_for_status.return_value = None
        loc_b_html = """
        <html><body>
        <article>
          <a href="https://www.laforet.com/agence-immobiliere/paris15lourmel/louer/paris-15/appartement-2-pieces-52805433" target="_blank">photo</a>
          <h3>Appartement <span>1 513 &#8364;/mois</span> <span>PARIS (75015)</span></h3>
          <div>50 m²&nbsp;&bull;&nbsp;2 pi&egrave;ces</div>
        </article>
        </body></html>
        """
        loc_b_resp = MagicMock(status_code=200, text=loc_b_html)
        loc_b_resp.raise_for_status.return_value = None

        def fake_get(url, timeout=15):
            return loc_a_resp if "75014" in url else loc_b_resp

        # Force both locations through the per-location fallback path (as if
        # neither resolved to an INSEE code) so this exercises _scrape_location.
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.get.side_effect = fake_get
                mock_session_cls.return_value = mock_session
                listings = parser.scrape({"locations": [
                    {"city": "Paris", "postalCode": "75014"},
                    {"city": "Paris", "postalCode": "75015"},
                ]})

        assert len(listings) == 1
        assert listings[0].listing_id == "lf_52805433"
        assert listings[0].zip_code == "75015"

    def test_scrape_one_bad_location_does_not_lose_the_others(self):
        """A 404 on one city/postal code must not discard results already
        found for the others in the same multi-location search."""
        parser = LaforetParser()

        paris_resp = MagicMock(status_code=200, text=SAMPLE_PAGE_HTML)
        paris_resp.raise_for_status.return_value = None
        not_found_resp = MagicMock(status_code=404)

        def fake_get(url, timeout=15):
            return not_found_resp if "nawak" in url else paris_resp

        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.get.side_effect = fake_get
                mock_session_cls.return_value = mock_session
                listings = parser.scrape({"locations": [
                    {"city": "Paris", "postalCode": "75018"},
                    {"city": "Nawak", "postalCode": "99999"},
                ]})

        assert len(listings) == 1
        assert listings[0].zip_code == "75018"

    def test_scrape_raises_only_when_every_location_fails(self):
        parser = LaforetParser()
        resp = MagicMock(status_code=404)
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            with patch("requests.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session.get.return_value = resp
                mock_session_cls.return_value = mock_session
                with pytest.raises(ValueError):
                    parser.scrape({"locations": [
                        {"city": "Nawak", "postalCode": "99999"},
                        {"city": "Bidule", "postalCode": "88888"},
                    ]})

    def test_merged_request_failure_falls_back_to_per_location(self):
        """If the combined multi-location request itself fails (network
        error, unexpected response), every location that was part of it
        must still get its own separate attempt rather than being lost."""
        parser = LaforetParser()

        def side_effect(*args, **kwargs):
            if "params" in kwargs:
                raise ConnectionError("merged request boom")
            resp = MagicMock(status_code=200, text=SAMPLE_PAGE_HTML)
            resp.raise_for_status.return_value = None
            return resp

        with patch("requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.side_effect = side_effect
            mock_session_cls.return_value = mock_session
            listings = parser.scrape({"city": "Paris", "postalCode": "75018"})

        # Merged attempt (with params=) raised, fell back to per-location
        # (no params=) which succeeds via SAMPLE_PAGE_HTML.
        assert len(listings) == 1
        assert listings[0].zip_code == "75018"
