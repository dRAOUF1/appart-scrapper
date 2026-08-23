"""Tests unitaires de parsers/laforet.py.

Laforêt n'a pas d'API : tout se joue sur du HTML rendu côté serveur, une URL
dont le chemin porte la ville et dont la query porte les filtres. Presque tout
le module est donc constitué de fonctions PURES (slug, extraction de section,
parsing de cartes, filtrage) — c'est là que se trouve la valeur de ces tests.

Les invariants figés ici correspondent chacun à une panne réellement observée
en production, et sont commentés à l'endroit du test :

* `_extract_genuine_section` coupe sur « proximité de » et JAMAIS sur « Autres
  annonces » (couper là fait perdre plus de 80 % des annonces — chiffres réels
  dans TestExtractGenuineSection) ;
* `_DETAIL_LINK_RE` doit rejeter les cartes d'agence (`/agence-immobiliere/
  lyon-7`), qui finissent aussi par `-<chiffre>` ;
* `_listing_path` nettoie ancre et query, sans quoi les annonces liées vers une
  section (`...#section-video`) étaient purement ignorées ;
* les filtres prix/surface/pièces ne partent QU'AVEC un filtre de périmètre,
  sinon le site bascule en recherche nationale ;
* la pagination s'arrête sur « page sans annonce inédite », pas sur le nombre
  de pages annoncé par le site (qui disparaît dès qu'un filtre est présent) ;
* un scrape multi-périmètres n'échoue que si TOUT échoue ;
* département/région s'ancrent sur leurs pages CANONIQUES (/departement/…,
  /region/…) — vérifiées en live le 2026-08-23. L'ancienne ancre « ville
  principale du département + premier CP » redirigeait (301) sur 37
  départements sur 101, et saint-denis-97400 rebasculait vers la recherche
  NATIONALE : le repli sans filtres balayait alors tout le stock français
  pour n'en retenir rien, silencieusement ;
* une page HTTP 200 qui ne rend AUCUNE carte sur une requête portant des
  filtres de périmètre ALERTE bruyamment au lieu de présenter un succès vide
  comme une vérité (impossible de distinguer « périmètre vraiment vide »
  d'« HTML inattendu » sans voir la page) ;
* les périmètres d'outre-mer (971–974, 976), que le moteur Laforêt ne couvre
  pas du tout, sont annoncés AVANT le scrape plutôt qu'en 0 résultat muet.

Les tests de régression de tests/_legacy/test_laforet_parser.py sont tous
repris, réorganisés et paramétrés (le doublon exact
`test_agency_office_card_is_still_rejected` /
`test_ignores_nearby_agency_office_cards` n'a été gardé qu'une fois).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import requests

from core.geocode import _arrondissement_insee_code
from parsers.laforet import (
    _DETAIL_LINK_RE,
    BASE_URL,
    DESKTOP_UA,
    DOM_ROM_DEPARTMENTS,
    MAX_PAGES,
    LaforetParser,
    _canonical_slug,
    _card_photos,
    _city_insee_codes,
    _department_codes,
    _describe,
    _dict_to_listing,
    _dom_rom_departments,
    _extract_genuine_section,
    _listing_path,
    _parse_cards,
    _parse_price,
    _passes_filters,
    _property_type_from_url,
    _property_types,
    _slugify,
    _transaction,
)
from tests.helpers.factories import (
    make_city_location,
    make_department_location,
    make_listing,
    make_region_location,
    make_whole_city_location,
)

# ---------------------------------------------------------------------------
# Périmètres réutilisés — ne jamais muter (partagés par tout le module)
# ---------------------------------------------------------------------------

# Les codes INSEE d'arrondissement viennent de la formule pure de core.geocode
# (75018 -> 75118, 69007 -> 69387), donc aucun appel réseau n'est nécessaire.
PARIS_18 = make_city_location("Paris", "75018", "75118")
PARIS_14 = make_city_location("Paris", "75014", "75114")
LYON_7 = make_city_location("Lyon", "69007", "69387")
GIRONDE = make_department_location("33", "Gironde")
AUBE = make_department_location("10", "Aube")
IDF = make_region_location("11", "Île-de-France", ("75", "77", "78", "91", "92", "93", "94", "95"))
PACS = make_region_location("93", "Provence-Alpes-Côte d'Azur", ("13",))
# Outre-mer : le moteur Laforêt n'y référence aucun bien (issue #13, vérifié
# en live) — ces périmètres servent à figer l'avertissement.
SAINT_DENIS_REUNION = make_city_location("Saint-Denis", "97400", "97411")
REUNION = make_department_location("974", "La Réunion")
GUADELOUPE_REGION = make_region_location("01", "Guadeloupe", ("971", "972"))
PARIS_WHOLE = make_whole_city_location("Paris", ("75001", "75002", "75015"), "75056")
BORDEAUX = {"city": "Bordeaux", "postalCode": "33000"}

# ---------------------------------------------------------------------------
# Blobs HTML
#
# Recopiés depuis de vraies pages laforet.com (capturées en live pendant la
# mise au point du scraper) et réduits à ce qui compte. Ils reproduisent trois
# formes de page distinctes, chacune à l'origine d'un bug réel — voir le
# commentaire de chaque constante.
# ---------------------------------------------------------------------------

# Page de résultats d'UNE ville : la carte 75018 est un vrai résultat, la carte
# 75015 est du remplissage voisin. Le bloc JSON-LD ItemList annonce 3 pages —
# il est là exprès : la pagination ne doit PAS s'y fier (il disparaît dès qu'un
# filtre est envoyé, ce qui arrêtait la boucle après la première page).
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

# Réponse d'une requête fusionnée (filter[cities][] sur deux villes) : les
# cartes des DEUX villes sont de vrais résultats, dans la même section — à la
# différence de SAMPLE_PAGE_HTML, où la seconde est du remplissage.
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

# Bug réel : quand une ville a peu de stock, Laforêt complète la page avec des
# cartes d'AGENCE (le lien pointe le bureau, pas une annonce). « lyon-7 » finit
# aussi par « -<chiffre> », ce qu'un motif trop laxiste prenait pour une
# référence d'annonce — vérifié en live, ça produisait une fausse annonce sans
# prix, sans surface, sans pièces et sans code postal.
PAGE_WITH_AGENCY_OFFICE_CARD = """
<html><body>
<article>
  <a href="https://www.laforet.com/agence-immobiliere/lyon-7" target="_blank">Agence Laforêt LYON 7</a>
  <div>Fermé — 55 avenue Jean Jaurès, 69007 LYON</div>
</article>
</body></html>
"""

# Les annonces Laforêt n'avaient aucune image là où SeLoger en remontait : la
# vignette de la liste restait vide. La query string des `src` porte une
# SIGNATURE (`&s=...`) sans laquelle le serveur d'images répond 403.
CARD_WITH_PHOTOS_HTML = """
<html><body><article>
  <a href="https://www.laforet.com/agence-immobiliere/poitiers/louer/poitiers/appartement-2-pieces-52829947">p</a>
  <h3>Appartement <span>370 &#8364;/mois</span> <span>POITIERS (86000)</span></h3>
  <div>17 m² • 2 pièces</div>
  <img loading="lazy" src="/glide/office9/lf/catalog/images/pr_p/5/52829947a.jpg?w=400&amp;s=aaa" alt="Séjour">
  <img loading="lazy" src="/glide/office9/lf/catalog/images/pr_p/5/52829947b.jpg?w=400&amp;s=bbb" alt="Cuisine">
  <img loading="lazy" src="/glide/office9/lf/catalog/images/pr_p/5/52829947a.jpg?w=400&amp;s=aaa" alt="Séjour">
</article></body></html>
"""

PHOTO_A = f"{BASE_URL}/glide/office9/lf/catalog/images/pr_p/5/52829947a.jpg?w=400&s=aaa"
PHOTO_B = f"{BASE_URL}/glide/office9/lf/catalog/images/pr_p/5/52829947b.jpg?w=400&s=bbb"


# ---------------------------------------------------------------------------
# Fabriques de HTML — pour tout ce qui n'a pas besoin d'un blob réaliste
# ---------------------------------------------------------------------------

def card(
    reference: str,
    *,
    city: str = "PARIS",
    zip_code: str = "75018",
    price: str = "900 €/mois",
    surface: str = "30 m²",
    rooms: str = "1 pièce",
    transaction: str = "louer",
    type_slug: str = "appartement",
    agency: str = "paris18marxdormoy",
    city_slug: str = "paris-18",
    suffix: str = "1-piece",
    href: str | None = None,
    photos: tuple[str, ...] = (),
) -> str:
    """Une carte d'annonce au balisage de Laforêt, paramétrable."""
    link = href if href is not None else (
        f"{BASE_URL}/agence-immobiliere/{agency}/{transaction}/{city_slug}/"
        f"{type_slug}-{suffix}-{reference}"
    )
    images = "".join(f'<img loading="lazy" src="{src}" alt="">' for src in photos)
    return (
        "<article>"
        f'<a href="{link}" target="_blank">photo</a>'
        f"<h3>Bien <span>{price}</span> <span>{city} ({zip_code})</span></h3>"
        f"<div>{surface} • {rooms}</div>{images}</article>"
    )


def page(*cards: str) -> str:
    return f"<html><body>{''.join(cards)}</body></html>"


EMPTY_PAGE_HTML = page()


# ---------------------------------------------------------------------------
# Double de requests.Session
# ---------------------------------------------------------------------------

class FakeResponse:
    """Réponse HTTP minimale : `text`, `status_code`, `raise_for_status()`."""

    def __init__(self, text: str = "", status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error")


class FakeSession:
    """Double de `requests.Session` : enregistre les appels, rejoue des réponses.

    `pages` est consommée dans l'ordre et la DERNIÈRE réponse est ensuite
    répétée indéfiniment : c'est ce qui laisse la pagination s'arrêter d'elle-
    même sur « aucune annonce inédite » sans que le test ait à compter les
    pages. `handler(url, params)` prend le dessus quand le test veut décider
    réponse par réponse (404 sur une ville, exception sur la requête
    fusionnée...).
    """

    def __init__(self, pages=None, handler=None):
        self.headers: dict[str, str] = {}
        self.calls: list[dict] = []
        self._responses = [
            p if isinstance(p, FakeResponse) else FakeResponse(p) for p in (pages or [EMPTY_PAGE_HTML])
        ]
        self._handler = handler

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if self._handler is not None:
            return self._handler(url, params)
        response = self._responses[0]
        if len(self._responses) > 1:
            self._responses.pop(0)
        return response

    # -- lecture des appels enregistrés ------------------------------------
    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]

    @property
    def merged_calls(self) -> list[dict]:
        """Les appels de la requête fusionnée : eux seuls passent `params=`."""
        return [call for call in self.calls if call["params"] is not None]

    @property
    def plain_calls(self) -> list[dict]:
        """Les appels de repli par périmètre : URL nue, sans `params=`."""
        return [call for call in self.calls if call["params"] is None]

    def param_values(self, key: str) -> list[str]:
        return [
            value
            for call in self.merged_calls
            for param_key, value in call["params"]
            if param_key == key
        ]


def run_scrape(criteria: dict, session: FakeSession, parser: LaforetParser | None = None):
    """Exécute `scrape()` en substituant `session` à la vraie requests.Session."""
    parser = parser or LaforetParser()
    with patch("requests.Session", return_value=session):
        return parser.scrape(criteria)


@pytest.fixture
def logged():
    """Les messages loguru émis pendant le test, sous forme (niveau, message).

    `caplog` ne voit pas loguru : il faut brancher un sink. Il est retiré à la
    fin du test (et le socle en attraperait la fuite de toute façon).
    """
    from loguru import logger

    records: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
    )
    yield records
    logger.remove(sink_id)


def at(*postal_codes: str) -> list[dict]:
    """Des périmètres au niveau code postal, le cas le plus courant."""
    return [make_city_location("Ville", postal_code, "00000") for postal_code in postal_codes]


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------

class TestSlugify:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Paris", "paris"),
            # Le cas d'école : accents ET tirets déjà présents.
            ("Le Kremlin-Bicêtre", "le-kremlin-bicetre"),
            ("Île-de-France", "ile-de-france"),
            ("Saint-Étienne", "saint-etienne"),
            ("Sainte-Foy-lès-Lyon", "sainte-foy-les-lyon"),
            ("Charenton-le-Pont", "charenton-le-pont"),
            ("Aix-en-Provence", "aix-en-provence"),
            # Apostrophe : remplacée par un tiret, comme tout non-alphanumérique.
            ("Bois-d'Arcy", "bois-d-arcy"),
            ("L'Haÿ-les-Roses", "l-hay-les-roses"),
            # Espaces multiples et bords : un seul tiret, rien qui dépasse.
            ("  Saint  Mandé  ", "saint-mande"),
            ("---", ""),
            ("", ""),
            ("75018", "75018"),
            # NFKD ne décompose pas les ligatures : Œ est simplement perdu.
            # Aucune commune française concernée, mais autant le savoir.
            ("Œuf", "uf"),
        ],
    )
    def test_slugs(self, text, expected):
        assert _slugify(text) == expected

    @pytest.mark.parametrize("text", ["Paris", "Le Kremlin-Bicêtre", "  Saint  Mandé  "])
    def test_a_slug_never_starts_or_ends_with_a_hyphen(self, text):
        """Un tiret en bord de slug donnerait une URL en 404."""
        slug = _slugify(text)
        assert not slug.startswith("-")
        assert not slug.endswith("-")
        assert "--" not in slug


class TestCanonicalSlug:
    """Le slug des pages canoniques /departement/ et /region/, tel que le site
    l'écrit lui-même (voir _canonical_slug)."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Gironde", "gironde"),
            ("Aube", "aube"),
            ("Île-de-France", "ile-de-france"),
            # L'apostrophe est SUPPRIMÉE, pas convertie en tiret : c'est ce que
            # fait le site (vérifié en live le 2026-08-23,
            # /region/location-appartement-provence-alpes-cote-dazur rend 86
            # annonces ; la forme ...-cote-d-azur, elle, n'existe pas).
            ("Provence-Alpes-Côte d'Azur", "provence-alpes-cote-dazur"),
            # Apostrophe typographique (’) : même traitement que la droite.
            ("Provence-Alpes-Côte d\u2019Azur", "provence-alpes-cote-dazur"),
        ],
    )
    def test_slugs(self, name, expected):
        assert _canonical_slug(name) == expected


# ---------------------------------------------------------------------------
# _transaction
# ---------------------------------------------------------------------------

class TestTransaction:
    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            ({"transaction": "rent"}, "rent"),
            ({"transaction": "buy"}, "buy"),
            # LA LOCATION PAR DÉFAUT, assumée : c'est ce que le formulaire
            # propose en premier, et une recherche sans transaction explicite
            # n'a jamais voulu dire « achat ».
            ({}, "rent"),
            ({"transaction": None}, "rent"),
            ({"transaction": ""}, "rent"),
            # Valeurs hors vocabulaire canonique : jamais devinées.
            ({"transaction": "sale"}, "rent"),
            ({"transaction": "Rent"}, "rent"),
            ({"transaction": "location"}, "rent"),
        ],
    )
    def test_transaction_defaults_to_rent(self, criteria, expected):
        assert _transaction(criteria) == expected


# ---------------------------------------------------------------------------
# _property_types
# ---------------------------------------------------------------------------

class TestPropertyTypes:
    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            # Rien de demandé -> appartement, le défaut du formulaire.
            (None, ["apartment"]),
            ([], ["apartment"]),
            (["apartment"], ["apartment"]),
            (["house"], ["house"]),
            # L'ordre demandé est conservé (il décide du slug du chemin).
            (["apartment", "house"], ["apartment", "house"]),
            (["house", "apartment"], ["house", "apartment"]),
            # Mixte : les types hors capacités sont retirés SANS exception —
            # « appartement + parking » doit ramener les appartements.
            (["apartment", "parking"], ["apartment"]),
            (["parking", "house", "land"], ["house"]),
            # UNIQUEMENT des types hors capacités -> liste vide, et SURTOUT
            # PAS le défaut appartement : renvoyer des appartements à qui
            # demande un parking serait un faux résultat.
            (["parking"], []),
            (["parking", "land"], []),
            (["yacht"], []),
        ],
    )
    def test_supported_types_only(self, requested, expected):
        assert _property_types({"propertyTypes": requested}) == expected

    @pytest.mark.parametrize("requested", [["parking"], ["parking", "land"], ["yacht"]])
    def test_only_unsupported_types_produce_no_url_and_no_scrape(self, requested):
        """La conséquence directe de la liste vide, aux deux points d'entrée."""
        parser = LaforetParser()
        criteria = {"locations": [PARIS_18], "propertyTypes": requested}

        assert parser.build_search_urls(criteria) == []
        assert parser.build_search_url(criteria) is None
        with pytest.raises(ValueError, match="ne référence aucun des types de bien demandés"):
            parser.scrape(criteria)

    def test_an_unsupported_type_is_announced_before_any_scrape(self):
        """Régression : un type que Laforêt ne référence pas levait une
        ValueError jusque dans la reconstruction d'URL (soit un 500 sur
        /api/searches/<id>/urls). Il est désormais annoncé en amont."""
        parser = LaforetParser()
        criteria = {"locations": [PARIS_18], "propertyTypes": ["parking"]}

        reason = parser.cannot_search_reason(criteria)
        assert reason == "Laforêt ne référence pas les biens de type « Parking »"


# ---------------------------------------------------------------------------
# _describe
# ---------------------------------------------------------------------------

class TestDescribe:
    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            (IDF, "région Île-de-France"),
            ({"kind": "region", "code": "94"}, "région 94"),
            (GIRONDE, "département Gironde"),
            ({"kind": "department", "code": "33"}, "département 33"),
            (PARIS_WHOLE, "Paris (toute la ville)"),
            (PARIS_18, "Paris 75018"),
            # Sans `kind`, c'est une commune : le format d'avant les périmètres
            # larges reste lisible.
            ({"city": "Paris", "postalCode": "75018"}, "Paris 75018"),
        ],
    )
    def test_human_readable_perimeters(self, location, expected):
        assert _describe(location) == expected


# ---------------------------------------------------------------------------
# _city_insee_codes
# ---------------------------------------------------------------------------

class TestCityInseeCodes:
    def test_a_city_uses_the_insee_code_from_the_autocomplete(self):
        """Le code fourni par l'autocomplete est utilisé tel quel : aucune
        résolution, donc aucun appel réseau."""
        with patch("parsers.laforet._resolve_insee_code") as mock_resolve:
            assert _city_insee_codes(PARIS_18) == ["75118"]
        mock_resolve.assert_not_called()

    def test_a_city_without_an_insee_code_is_resolved_from_its_postal_code(self):
        with patch("parsers.laforet._resolve_insee_code", return_value="33063") as mock_resolve:
            assert _city_insee_codes({"kind": "city", "city": "Bordeaux", "postalCode": "33000"}) == ["33063"]
        mock_resolve.assert_called_once_with("33000")

    def test_an_unresolvable_city_has_no_code(self):
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            assert _city_insee_codes({"kind": "city", "city": "Nawak", "postalCode": "99999"}) == []

    def test_a_whole_city_is_a_single_commune_code(self):
        """« Paris — toute la ville » utilise LE code de la commune (75056), que
        Laforêt comprend directement : vérifié en live qu'il rend exactement le
        même résultat que l'énumération des 20 arrondissements (66 annonces dans
        les deux cas). Inutile de les développer."""
        assert _city_insee_codes(PARIS_WHOLE) == ["75056"]

    def test_a_whole_city_falls_back_to_its_postal_codes(self):
        """Localisation enregistrée sans code de commune : les codes déduits des
        codes postaux restent équivalents, et valent mieux que pas de filtre."""
        location = {"kind": "whole_city", "city": "Paris", "postalCodes": ["75001", "75015"]}
        with patch("parsers.laforet._resolve_insee_code", side_effect=_arrondissement_insee_code):
            assert _city_insee_codes(location) == ["75101", "75115"]

    def test_the_postal_code_fallback_dedupes_and_keeps_order(self):
        location = {"kind": "whole_city", "city": "Bordeaux", "postalCodes": ["33000", "33800", "33300"]}
        with patch("parsers.laforet._resolve_insee_code", side_effect=["33063", "33063", "33065"]):
            assert _city_insee_codes(location) == ["33063", "33065"]

    @pytest.mark.parametrize(
        "location",
        [
            {"kind": "whole_city", "city": "Nulle part", "postalCodes": []},
            {"kind": "whole_city", "city": "Nulle part"},
        ],
        ids=["codes_postaux_vides", "sans_codes_postaux"],
    )
    def test_a_whole_city_without_anything_to_resolve(self, location):
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            assert _city_insee_codes(location) == []

    @pytest.mark.parametrize("location", [GIRONDE, IDF], ids=["departement", "region"])
    def test_wide_perimeters_have_no_commune_code(self, location):
        """Ils ont leur propre filtre (filter[departments][])."""
        assert _city_insee_codes(location) == []


# ---------------------------------------------------------------------------
# _department_codes
# ---------------------------------------------------------------------------

class TestDepartmentCodes:
    def test_a_department_is_its_own_code(self):
        assert _department_codes(GIRONDE) == ["33"]

    def test_a_department_without_a_code_has_none(self):
        assert _department_codes({"kind": "department", "name": "Gironde"}) == []

    def test_a_region_is_the_set_of_its_departments(self):
        """Laforêt a bien un filtre région natif (filter[regions][]=11, même
        résultat à l'annonce près : 741 annonces pour l'Île-de-France) mais les
        départements rendent le périmètre explicite dans l'URL et ne dépendent
        pas d'un découpage propre au site."""
        assert _department_codes(IDF) == ["75", "77", "78", "91", "92", "93", "94", "95"]

    def test_the_returned_list_is_a_copy(self):
        """Les critères sont partagés entre les sources : la liste rendue ne doit
        pas être celle stockée dans la localisation."""
        location = make_region_location("11", "Île-de-France", ("75", "77"))
        codes = _department_codes(location)

        codes.append("99")
        assert location["departments"] == ["75", "77"]

    def test_a_region_without_its_departments_resolves_them(self):
        """Région enregistrée sans ses départements (saisie manuelle, ou format
        antérieur) : les retrouver plutôt que d'abandonner le périmètre."""
        with patch("parsers.laforet.region_departments", return_value=["2A", "2B"]) as mock_region:
            assert _department_codes({"kind": "region", "name": "Corse", "code": "94"}) == ["2A", "2B"]
        mock_region.assert_called_once_with("94")

    def test_a_region_without_departments_nor_code(self):
        with patch("parsers.laforet.region_departments") as mock_region:
            assert _department_codes({"kind": "region", "name": "Corse"}) == []
        mock_region.assert_not_called()

    def test_a_region_whose_departments_cannot_be_resolved(self):
        with patch("parsers.laforet.region_departments", return_value=[]):
            assert _department_codes({"kind": "region", "code": "94"}) == []

    @pytest.mark.parametrize(
        "location",
        [PARIS_18, PARIS_WHOLE, {"city": "Paris", "postalCode": "75018"}],
        ids=["commune", "ville_entiere", "sans_kind"],
    )
    def test_narrow_perimeters_have_no_department_code(self, location):
        assert _department_codes(location) == []


# ---------------------------------------------------------------------------
# Outre-mer : des périmètres que le moteur Laforêt ne couvre PAS DU TOUT
# ---------------------------------------------------------------------------

class TestDomRomDepartments:
    """Le moteur Laforêt ne référence aucun bien outre-mer (vérifié en live le
    2026-08-23 : filter[departments][]=974 et filter[cities][]=97411 rendent
    0 annonce, même avec des filtres). La détection couvre tous les niveaux de
    périmètre, pour pouvoir prévenir l'utilisateur AVANT le scrape plutôt que
    de laisser un 0 annonce silencieux passer pour un succès (issue #13)."""

    def test_the_known_outre_mer_departments(self):
        """Mayotte (976) y figure ; la Corse (2A/2B, codes postaux 20xxx),
        elle, est en métropole."""
        assert DOM_ROM_DEPARTMENTS == ("971", "972", "973", "974", "976")

    def test_a_department_perimeter(self):
        assert _dom_rom_departments([REUNION]) == ["974"]

    def test_a_region_through_its_departments(self):
        assert _dom_rom_departments([GUADELOUPE_REGION]) == ["971", "972"]

    def test_a_city_through_its_postal_code_and_insee(self):
        assert _dom_rom_departments([SAINT_DENIS_REUNION]) == ["974"]

    def test_a_whole_city_through_its_postal_codes(self):
        fort_de_france = {
            "kind": "whole_city",
            "city": "Fort-de-France",
            "postalCodes": ["97200", "97234"],
            "inseeCode": "97209",
        }
        assert _dom_rom_departments([fort_de_france]) == ["972"]

    def test_metropolitan_perimeters_are_never_flagged(self):
        assert _dom_rom_departments([PARIS_18, LYON_7, GIRONDE, IDF, PARIS_WHOLE]) == []

    def test_mixed_perimeters_are_deduplicated_and_ordered(self):
        assert _dom_rom_departments([GIRONDE, REUNION, SAINT_DENIS_REUNION]) == ["974"]


class TestDomRomWarning:
    @pytest.mark.parametrize(
        "criteria",
        [
            {"locations": [SAINT_DENIS_REUNION]},
            {"locations": [REUNION]},
            {"locations": [IDF, REUNION]},
        ],
        ids=["commune_dom", "departement_dom", "mixte_metropole_et_dom"],
    )
    def test_an_outre_mer_perimeter_is_announced_before_scraping(self, criteria, logged):
        session = FakeSession([EMPTY_PAGE_HTML])
        run_scrape(criteria, session)

        assert any(
            level == "WARNING" and "Départements d'outre-mer couverts" in message
            for level, message in logged
        )

    def test_one_warning_for_the_whole_scrape_even_with_several_dom_locations(self, logged):
        session = FakeSession([EMPTY_PAGE_HTML])
        run_scrape({"locations": [REUNION, SAINT_DENIS_REUNION]}, session)

        warnings = [m for level, m in logged if level == "WARNING" and "outre-mer" in m]
        assert len(warnings) == 1

    def test_a_metropolitan_search_stays_silent_about_outre_mer(self, logged):
        session = FakeSession([MERGED_PAGE_HTML])
        run_scrape({"locations": [PARIS_18, LYON_7]}, session)

        assert not any("outre-mer" in message for _, message in logged)


# ---------------------------------------------------------------------------
# _extract_genuine_section
# ---------------------------------------------------------------------------

class TestExtractGenuineSection:
    """Laforêt ajoute toujours derrière les vrais résultats une section
    « Appartements à proximité de {ville} » alimentée par les communes voisines,
    au même balisage de carte. Tout ce qui parse la page entière ramasse ce
    bruit (vérifié en live : c'est ainsi que des annonces de villes sans rapport
    contaminaient une recherche)."""

    def test_truncates_before_the_nearby_marker(self):
        assert _extract_genuine_section("GENUINEproximité de Paris 14</div>NOISE") == "GENUINE"

    def test_returns_the_whole_page_when_the_marker_is_absent(self):
        html = "<html>pas de marqueur, du stock local en quantité</html>"
        assert _extract_genuine_section(html) is html

    def test_only_the_first_occurrence_matters(self):
        assert _extract_genuine_section("A proximité de B proximité de C") == "A "

    def test_a_page_that_is_only_noise_yields_nothing(self):
        assert _extract_genuine_section("proximité de Paris") == ""

    def test_an_other_listings_heading_must_never_truncate(self):
        """GARDE-FOU. La page porte aussi un titre « Autres annonces », qui
        ressemble à un début de section de remplissage mais découpe en réalité
        LES RÉSULTATS eux-mêmes en plusieurs blocs.

        Couper dessus paraît prudent et fait perdre la majorité des annonces.
        Mesuré sur 12 recherches réelles, arbitre = le compteur que le site
        affiche lui-même (« N annonces à louer/vendre ») :

            recherche             site   coupe « proximité »   coupe « Autres annonces »
            Lille location          15          15  ✓                   3  ✗
            Toulouse achat maison   11          11  ✓                   1  ✗
            Marseille 8e achat      16          16  ✓                   7  ✗
            Boulogne location       11          11  ✓                   5  ✗

        Ce test échoue si quelqu'un « corrige » à nouveau dans ce sens.
        """
        html = "RESULTAT-BLOC-1<h2>Autres annonces</h2>RESULTAT-BLOC-2"
        assert _extract_genuine_section(html) == html

    def test_result_cards_placed_after_an_other_listings_heading_survive(self):
        """Concrètement : les cartes situées après « Autres annonces » sont des
        résultats et doivent ressortir."""
        html = page(
            card("11111111", city="LILLE", zip_code="59000", city_slug="lille", agency="lille"),
            "<h2>Autres annonces</h2>",
            card("22222222", city="LILLE", zip_code="59000", city_slug="lille", agency="lille"),
        )
        references = {c["reference"] for c in _parse_cards(_extract_genuine_section(html))}
        assert references == {"11111111", "22222222"}

    def test_the_noise_section_cards_are_dropped(self):
        """Le pendant du test précédent : ce qui suit « proximité de » disparaît."""
        html = page(
            card("11111111", city="LILLE", zip_code="59000", city_slug="lille", agency="lille"),
            "<h2>Appartements à proximité de Lille</h2>",
            card("22222222", city="ROUBAIX", zip_code="59100", city_slug="roubaix", agency="roubaix"),
        )
        references = {c["reference"] for c in _parse_cards(_extract_genuine_section(html))}
        assert references == {"11111111"}


# ---------------------------------------------------------------------------
# _parse_price
# ---------------------------------------------------------------------------

class TestParsePrice:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1 021 €/mois", 1021.0),
            # Espace insécable : le balisage réel de Laforêt (&nbsp;).
            ("1 513 €", 1513.0),
            # Espace insécable étroit, l'autre séparateur de milliers vu en ligne.
            ("1 000 €", 1000.0),
            ("560 000 €", 560000.0),
            ("2 350 000 €", 2350000.0),
            ("370 €", 370.0),
            ("0 €", 0.0),
            # Le premier prix de la carte gagne (loyer avant charges).
            ("1 021 €/mois puis 2 000 € de charges", 1021.0),
            # Aucun montant : pas de prix, pas d'exception.
            ("Prix : nous consulter", None),
            ("", None),
            # Le symbole sans chiffre : le motif capture l'espace, float("")
            # lève, et la ValueError est rattrapée -> None.
            ("Nous consulter €", None),
            # Le symbole avant le montant n'est pas la convention française.
            ("€ 1021", None),
        ],
    )
    def test_prices(self, text, expected):
        assert _parse_price(text) == expected

    @pytest.mark.parametrize(
        ("text", "misread_as"),
        [
            ("1.021 €", 21.0),
            ("12,5 €", 5.0),
        ],
    )
    def test_a_non_space_separator_is_misread(self, text, misread_as):
        """Divergence connue : seuls les chiffres et les espaces sont capturés,
        donc un point ou une virgule tronque le montant. Laforêt n'affiche que
        des entiers séparés par des espaces (insécables), le cas ne se produit
        pas en ligne — figé ici pour que la limite soit explicite si le site
        change de format."""
        assert _parse_price(text) == misread_as


# ---------------------------------------------------------------------------
# _listing_path et _DETAIL_LINK_RE
# ---------------------------------------------------------------------------

class TestListingPath:
    @pytest.mark.parametrize(
        ("href", "expected"),
        [
            # Régression : Laforêt lie parfois directement une section de la
            # page de l'annonce (`...-52637604#section-video` quand elle a une
            # vidéo). _DETAIL_LINK_RE étant ancré sur `(\\d+)$`, ces liens ne
            # correspondaient à rien et l'annonce était purement ignorée —
            # constaté en live sur Rennes, 6 annonces pour 7 annoncées.
            ("/a/appartement-1-piece-1#section-video", "/a/appartement-1-piece-1"),
            ("/a/appartement-1-piece-1?utm_source=x", "/a/appartement-1-piece-1"),
            ("/a/appartement-1-piece-1/", "/a/appartement-1-piece-1"),
            ("/a/appartement-1-piece-1//", "/a/appartement-1-piece-1"),
            ("/a/appartement-1-piece-1/?utm=x#video", "/a/appartement-1-piece-1"),
            ("/a/appartement-1-piece-1#video?utm=x", "/a/appartement-1-piece-1"),
            # Rien à nettoyer : la chaîne ressort intacte.
            ("/a/appartement-1-piece-1", "/a/appartement-1-piece-1"),
            ("", ""),
        ],
    )
    def test_cleans_anchor_query_and_trailing_slash(self, href, expected):
        assert _listing_path(href) == expected


class TestDetailLinkRegex:
    @pytest.mark.parametrize(
        ("path", "reference"),
        [
            (
                f"{BASE_URL}/agence-immobiliere/paris18marxdormoy/louer/paris-18/appartement-1-piece-52811904",
                "52811904",
            ),
            ("/agence-immobiliere/rennes/acheter/rennes/maison-11-pieces-52637604", "52637604"),
            ("/agence-immobiliere/lyon7agence/louer/lyon-07/appartement-2-pieces-99999999", "99999999"),
        ],
    )
    def test_accepts_a_full_listing_detail_path(self, path, reference):
        match = _DETAIL_LINK_RE.search(path)
        assert match is not None
        assert match.group(1) == reference

    @pytest.mark.parametrize(
        "path",
        [
            # LE cas qui compte : une carte d'agence, dont le lien finit aussi
            # par « -<chiffre> » (le numéro d'arrondissement du slug).
            "/agence-immobiliere/lyon-7",
            "/agence-immobiliere/paris-18",
            # Type de bien que Laforêt ne référence pas.
            "/agence-immobiliere/x/louer/paris-18/parking-1-place-123",
            # Transaction hors vocabulaire du site.
            "/agence-immobiliere/x/vendre/paris-18/appartement-1-piece-123",
            # Segment de ville manquant : la forme complète est imposée.
            "/agence-immobiliere/x/louer/appartement-1-piece-123",
            # Aucun segment entre le type et la référence.
            "/agence-immobiliere/x/louer/paris-18/appartement-52811904",
            # Pas de référence numérique en fin de chemin.
            "/agence-immobiliere/x/louer/paris-18/appartement-1-piece-abc",
            # Ancre/query non nettoyées : d'où _listing_path().
            "/agence-immobiliere/x/louer/paris-18/appartement-1-piece-123#section-video",
            "/agence-immobiliere/x/louer/paris-18/appartement-1-piece-123?utm=x",
            # Hors du préfixe /agence-immobiliere/.
            "/annonces/appartement-1-piece-123",
            "",
        ],
    )
    def test_rejects_everything_that_is_not_a_listing(self, path):
        assert _DETAIL_LINK_RE.search(path) is None


# ---------------------------------------------------------------------------
# _card_photos
# ---------------------------------------------------------------------------

class TestCardPhotos:
    def test_photos_are_absolute_and_keep_their_signature(self):
        """La query string porte une signature (`&s=...`) sans laquelle le
        serveur d'images répond 403 : elle ne doit surtout pas être coupée,
        contrairement à ce que fait _listing_path() sur les liens d'annonces."""
        photos = _parse_cards(CARD_WITH_PHOTOS_HTML)[0]["photos"]
        assert photos[0] == PHOTO_A
        assert "?w=400&s=aaa" in photos[0]

    def test_duplicates_are_collapsed_and_display_order_is_kept(self):
        """Les cartes portent plusieurs photos (a, b, c...), les suivantes
        masquées pour un défilement côté client : toutes sont gardées, dans
        l'ordre, une seule fois chacune."""
        assert _parse_cards(CARD_WITH_PHOTOS_HTML)[0]["photos"] == [PHOTO_A, PHOTO_B]

    @pytest.mark.parametrize(
        ("sources", "expected"),
        [
            # `src` relatif -> préfixé par le domaine.
            (["/glide/1.jpg?s=a"], [f"{BASE_URL}/glide/1.jpg?s=a"]),
            # Déjà absolu -> laissé tel quel.
            (["https://cdn.laforet.com/1.jpg?s=a"], ["https://cdn.laforet.com/1.jpg?s=a"]),
            # Les placeholders base64 ne sont pas des photos d'annonce.
            (["data:image/gif;base64,R0lGODlh"], []),
            (["data:image/gif;base64,R0lGODlh", "/glide/1.jpg"], [f"{BASE_URL}/glide/1.jpg"]),
            # `src` vide ou blanc : ignoré.
            ([""], []),
            (["   "], []),
            # Dédoublonnage en préservant l'ordre d'affichage.
            (["/a.jpg", "/b.jpg", "/a.jpg", "/c.jpg"],
             [f"{BASE_URL}/a.jpg", f"{BASE_URL}/b.jpg", f"{BASE_URL}/c.jpg"]),
            ([], []),
        ],
    )
    def test_photo_normalisation(self, sources, expected):
        html = page(card("1", photos=tuple(sources)))
        assert _parse_cards(html)[0]["photos"] == expected

    def test_an_img_without_a_src_attribute_is_ignored(self):
        from bs4 import BeautifulSoup

        article = BeautifulSoup('<article><img alt="rien"></article>', "lxml").find("article")
        assert _card_photos(article) == []

    def test_a_relative_src_without_a_leading_slash_is_left_alone(self):
        """Divergence connue : seul un `src` commençant par « / » est préfixé par
        le domaine. Laforêt n'émet que des chemins absolus (`/glide/...`), mais
        un chemin relatif ressortirait inutilisable plutôt qu'écarté."""
        assert _parse_cards(page(card("1", photos=("glide/1.jpg",))))[0]["photos"] == ["glide/1.jpg"]


# ---------------------------------------------------------------------------
# _parse_cards
# ---------------------------------------------------------------------------

class TestParseCards:
    def test_extracts_every_field_of_a_real_card(self):
        cards = _parse_cards(SAMPLE_PAGE_HTML)

        assert len(cards) == 2
        assert cards[0] == {
            "reference": "52811904",
            "url": (
                f"{BASE_URL}/agence-immobiliere/paris18marxdormoy/louer/paris-18/"
                "appartement-1-piece-52811904"
            ),
            "price_value": 1021.0,
            "city": "PARIS",
            "zip_code": "75018",
            "surface": "30",
            "rooms": "1",
            "agency": "paris18marxdormoy",
            "photos": [],
        }

    def test_document_order_is_preserved_and_plurals_are_understood(self):
        cards = _parse_cards(SAMPLE_PAGE_HTML)
        assert [c["reference"] for c in cards] == ["52811904", "52805433"]
        # « 2 pièces » au pluriel comme « 1 pièce » au singulier.
        assert cards[1]["rooms"] == "2"
        assert cards[1]["price_value"] == 1513.0

    def test_the_agency_is_the_segment_after_agence_immobiliere(self):
        cards = _parse_cards(page(card("1", agency="bordeaux-chartrons")))
        assert cards[0]["agency"] == "bordeaux-chartrons"

    def test_the_first_agency_link_of_the_card_is_the_listing_link(self):
        """La carte contient d'autres liens (favoris, agence) : c'est le premier
        contenant /agence-immobiliere/ qui est retenu."""
        html = (
            "<article>"
            '<a href="/favoris/ajouter?id=1">favori</a>'
            f'<a href="{BASE_URL}/agence-immobiliere/x/louer/paris-18/appartement-1-piece-111">photo</a>'
            f'<a href="{BASE_URL}/agence-immobiliere/y/louer/paris-18/appartement-1-piece-222">titre</a>'
            "<h3>Bien <span>900 €</span> <span>PARIS (75018)</span></h3></article>"
        )
        cards = _parse_cards(html)
        assert [c["reference"] for c in cards] == ["111"]
        assert cards[0]["agency"] == "x"

    def test_an_agency_office_card_is_rejected(self):
        """Le nettoyage de l'URL par _listing_path ne doit pas rouvrir la porte
        aux cartes d'agence : /agence-immobiliere/lyon-7 finit aussi par
        « -<chiffre> » mais n'est pas une annonce (vérifié en live : ça
        produisait une fausse annonce sans prix, surface, pièces ni code
        postal).

        NB : l'ancienne suite contenait ce test EN DOUBLE
        (test_agency_office_card_is_still_rejected et
        test_ignores_nearby_agency_office_cards, entrée et assertion
        identiques) — une seule copie est conservée ici.
        """
        assert _parse_cards(PAGE_WITH_AGENCY_OFFICE_CARD) == []

    @pytest.mark.parametrize(
        ("href", "expected_reference"),
        [
            # Ancre de section (annonce avec vidéo) : nettoyée puis reconnue.
            (
                f"{BASE_URL}/agence-immobiliere/rennes/acheter/rennes/maison-11-pieces-52637604#section-video",
                "52637604",
            ),
            (
                f"{BASE_URL}/agence-immobiliere/rennes/acheter/rennes/maison-4-pieces-12345678?utm_source=x",
                "12345678",
            ),
            (
                f"{BASE_URL}/agence-immobiliere/rennes/acheter/rennes/maison-4-pieces-12345678/",
                "12345678",
            ),
        ],
    )
    def test_a_dirty_href_is_cleaned_before_matching(self, href, expected_reference):
        cards = _parse_cards(page(card("ignored", href=href)))
        assert len(cards) == 1
        assert cards[0]["reference"] == expected_reference
        # Ni ancre ni query ne restent dans l'URL stockée.
        assert "#" not in cards[0]["url"]
        assert "?" not in cards[0]["url"]

    @pytest.mark.parametrize(
        "html",
        [
            # Aucun lien du tout.
            "<article><h3>Bien <span>900 €</span> <span>PARIS (75018)</span></h3></article>",
            # Un lien, mais pas vers /agence-immobiliere/.
            '<article><a href="/ville/location-appartement-paris-75018">voir</a></article>',
            # Un lien d'agence qui n'est pas une annonce.
            '<article><a href="/agence-immobiliere/lyon-7">agence</a></article>',
            # Pas d'<article> du tout.
            "<html><body><div>rien</div></body></html>",
            "",
        ],
    )
    def test_anything_that_is_not_a_listing_card_is_skipped(self, html):
        assert _parse_cards(html) == []

    def test_a_card_missing_its_facts_still_parses(self):
        """Fail-open sur les données de la carte : mieux vaut une annonce
        incomplète (elle sera filtrée plus loin) qu'une exception qui ferait
        perdre toute la page."""
        html = (
            "<article>"
            f'<a href="{BASE_URL}/agence-immobiliere/x/louer/paris-18/appartement-1-piece-777">p</a>'
            "<h3>Bien sans aucun détail</h3></article>"
        )
        assert _parse_cards(html) == [{
            "reference": "777",
            "url": f"{BASE_URL}/agence-immobiliere/x/louer/paris-18/appartement-1-piece-777",
            "price_value": None,
            "city": "",
            "zip_code": "",
            "surface": "",
            "rooms": "",
            "agency": "x",
            "photos": [],
        }]

    @pytest.mark.parametrize(
        ("text", "expected_city", "expected_zip"),
        [
            ("PARIS (75018)", "PARIS", "75018"),
            ("LE KREMLIN-BICETRE (94270)", "LE KREMLIN-BICETRE", "94270"),
            ("SAINT-MANDÉ (94160)", "SAINT-MANDÉ", "94160"),
            # La ville doit commencer par une majuscule : les cartes du site
            # sont en capitales, une ville en minuscules ne serait pas lue.
            ("paris (75018)", "", ""),
            # Un chiffre dans le nom coupe la reconnaissance.
            ("Paris 15e (75015)", "", ""),
            # Code postal seul : pas de ville, donc pas de correspondance.
            ("(75018)", "", ""),
            # Code postal mal formé.
            ("PARIS (7501)", "", ""),
        ],
    )
    def test_city_and_postal_code_extraction(self, text, expected_city, expected_zip):
        html = (
            "<article>"
            f'<a href="{BASE_URL}/agence-immobiliere/x/louer/paris-18/appartement-1-piece-1">p</a>'
            f"<h3>Bien <span>900 €</span> <span>{text}</span></h3></article>"
        )
        parsed = _parse_cards(html)[0]
        assert parsed["city"] == expected_city
        assert parsed["zip_code"] == expected_zip

    @pytest.mark.parametrize(
        ("surface_text", "expected"),
        [("30 m²", "30"), ("30m²", "30"), ("30,5 m²", "30,5"), ("30.5 m²", "30.5"), ("sans surface", "")],
    )
    def test_surface_extraction(self, surface_text, expected):
        html = page(card("1", surface=surface_text))
        assert _parse_cards(html)[0]["surface"] == expected

    @pytest.mark.parametrize(
        ("rooms_text", "expected"),
        [("1 pièce", "1"), ("2 pièces", "2"), ("11 pieces", "11"), ("2pièces", "2"), ("studio", "")],
    )
    def test_rooms_extraction(self, rooms_text, expected):
        html = page(card("1", rooms=rooms_text))
        assert _parse_cards(html)[0]["rooms"] == expected


# ---------------------------------------------------------------------------
# _property_type_from_url
# ---------------------------------------------------------------------------

class TestPropertyTypeFromUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("/agence-immobiliere/x/louer/paris-18/appartement-1-piece-1", "Appartement"),
            ("/agence-immobiliere/x/acheter/rennes/maison-11-pieces-1", "Maison"),
            # Type inconnu ou absent : pas de type deviné.
            ("/agence-immobiliere/x/louer/paris-18/loft-1-piece-1", ""),
            ("/agence-immobiliere/lyon-7", ""),
            # Le slug doit être en début de segment : « grande-appartement » n'en
            # est pas un.
            ("/agence-immobiliere/x/louer/paris-18/grande-appartement-1", ""),
            # Les deux slugs présents : l'appartement gagne (ordre de TYPE_SLUGS).
            ("/agence-immobiliere/x/louer/maison-alfort/appartement-2-pieces-1", "Appartement"),
        ],
    )
    def test_the_type_is_read_in_the_listing_url(self, url, expected):
        """Une même requête peut mélanger les types (filter[types][] est
        répétable et prime sur le slug du chemin — vérifié en live : une page
        « achat-appartement-bordeaux-33000 » avec types=apartment&types=house
        rend bien 17 appartements et 18 maisons). Le type ne peut donc pas être
        déduit de ce qui a été demandé, il doit être lu par annonce."""
        assert _property_type_from_url(url) == expected

    def test_a_mixed_request_labels_each_listing_from_its_own_url(self):
        html = page(
            card("111", type_slug="appartement", suffix="2-pieces"),
            card("222", type_slug="maison", suffix="5-pieces"),
        )
        listings = [_dict_to_listing(c) for c in _parse_cards(html)]
        assert [li.property_type for li in listings] == ["Appartement", "Maison"]


# ---------------------------------------------------------------------------
# _dict_to_listing
# ---------------------------------------------------------------------------

class TestDictToListing:
    def test_maps_a_card_to_the_common_listing_schema(self):
        listing = _dict_to_listing(_parse_cards(SAMPLE_PAGE_HTML)[0])

        assert listing.listing_id == "lf_52811904"
        assert listing.legacy_id == "52811904"
        assert listing.source == "laforet"
        assert listing.title == "Appartement PARIS"
        assert listing.price == "1021 €"
        assert listing.price_value == 1021.0
        assert listing.surface == "30"
        assert listing.rooms == "1"
        assert listing.city == "PARIS"
        assert listing.location == "PARIS"
        assert listing.zip_code == "75018"
        assert listing.agency == "paris18marxdormoy"
        assert listing.property_type == "Appartement"

    def test_the_identifier_is_prefixed_by_the_source(self):
        """`lf_` garantit qu'aucune référence ne peut collisionner avec un
        identifiant SeLoger (`sl_`)."""
        assert _dict_to_listing(_card_dict(reference="42")).listing_id == "lf_42"

    @pytest.mark.parametrize(
        ("price_value", "expected"),
        [
            (1021.0, "1021 €"),
            (0.0, "0 €"),
            (560000.0, "560000 €"),
            # `int()` tronque : les centimes disparaissent (Laforêt n'en affiche
            # pas, et `price_value` reste la valeur exacte pour les filtres).
            (1021.9, "1021 €"),
            # Prix illisible : chaîne vide plutôt qu'un « None € » affiché.
            (None, ""),
        ],
    )
    def test_the_displayed_price_is_formatted_from_the_value(self, price_value, expected):
        listing = _dict_to_listing(_card_dict(price_value=price_value))
        assert listing.price == expected
        assert listing.price_value == price_value

    @pytest.mark.parametrize(
        ("url", "city", "expected_title"),
        [
            ("/agence-immobiliere/x/louer/paris-18/appartement-1-piece-1", "PARIS", "Appartement PARIS"),
            ("/agence-immobiliere/x/acheter/rennes/maison-4-pieces-1", "RENNES", "Maison RENNES"),
            # Type illisible : le titre se réduit à la ville, sans espace en tête.
            ("/agence-immobiliere/lyon-7", "LYON", "LYON"),
            # Ville illisible : le titre se réduit au type.
            ("/agence-immobiliere/x/louer/paris-18/appartement-1-piece-1", "", "Appartement"),
            ("/agence-immobiliere/lyon-7", "", ""),
        ],
    )
    def test_the_title_is_the_type_and_the_city(self, url, city, expected_title):
        assert _dict_to_listing(_card_dict(url=url, city=city)).title == expected_title

    def test_photos_have_the_same_shape_as_seloger(self):
        """Même contrat que SeLoger : `image_url` pour la vignette, `photos` en
        JSON avec les clés url/alt/key (voir parsers/seloger.py)."""
        listing = _dict_to_listing(_parse_cards(CARD_WITH_PHOTOS_HTML)[0])

        assert listing.image_url == PHOTO_A
        assert json.loads(listing.photos) == [
            {"url": PHOTO_A, "alt": "", "key": ""},
            {"url": PHOTO_B, "alt": "", "key": ""},
        ]

    @pytest.mark.parametrize("photos", [[], None], ids=["liste_vide", "clé_absente"])
    def test_a_card_without_photos_stays_valid(self, photos):
        data = _card_dict()
        data["photos"] = photos
        listing = _dict_to_listing(data)

        assert listing.image_url == ""
        assert listing.photos == "[]"

    def test_the_location_label_is_the_city(self):
        """Laforêt ne donne pas de quartier : `location` et `city` coïncident."""
        listing = _dict_to_listing(_card_dict(city="BORDEAUX"))
        assert listing.location == "BORDEAUX" == listing.city


def _card_dict(**overrides) -> dict:
    """Un dict de carte tel que `_parse_cards` le produit."""
    data = {
        "reference": "52811904",
        "url": f"{BASE_URL}/agence-immobiliere/x/louer/paris-18/appartement-1-piece-52811904",
        "price_value": 1021.0,
        "city": "PARIS",
        "zip_code": "75018",
        "surface": "30",
        "rooms": "1",
        "agency": "x",
        "photos": [],
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# _passes_filters — le cœur du filtrage, aux sémantiques MIXTES
# ---------------------------------------------------------------------------

class TestPassesFiltersLocation:
    """La localisation échoue FERMÉ : contrairement au prix ou à la surface, une
    annonce dont on n'a pas su lire le code postal n'est jamais supposée
    correspondre — c'est exactement comme ça qu'une carte de remplissage sans
    code postal était autrefois remontée comme une fausse annonce."""

    @pytest.mark.parametrize(
        ("zip_code", "locations", "expected"),
        [
            ("75014", at("75014"), True),
            ("75014", at("75015"), False),
            ("75014", at("94230"), False),
            # Recherche multi-périmètres : n'importe lequel suffit.
            ("75014", at("75014", "92120"), True),
            ("92120", at("75014", "92120"), True),
            ("75015", at("75014", "92120"), False),
            # Code postal illisible : refusé, jamais toléré.
            ("", at("75014"), False),
            (None, at("75014"), False),
            # Aucun périmètre : rien ne peut correspondre.
            ("75014", [], False),
        ],
    )
    def test_the_postal_code_must_fall_inside_a_perimeter(self, zip_code, locations, expected):
        listing = make_listing(zip_code=zip_code)
        assert _passes_filters(listing, {}, locations) is expected

    @pytest.mark.parametrize(
        ("zip_code", "location", "expected"),
        [
            # Un département couvre tous ses codes postaux…
            ("33000", GIRONDE, True),
            ("33800", GIRONDE, True),
            ("75014", GIRONDE, False),
            # … une région, ceux de tous ses départements.
            ("75014", IDF, True),
            ("93100", IDF, True),
            ("33000", IDF, False),
            # Une ville entière, ses propres codes postaux.
            ("75015", PARIS_WHOLE, True),
            ("75018", PARIS_WHOLE, False),
        ],
    )
    def test_wide_perimeters_are_honoured(self, zip_code, location, expected):
        listing = make_listing(zip_code=zip_code)
        assert _passes_filters(listing, {}, [location]) is expected

    def test_a_rejected_location_short_circuits_every_other_filter(self):
        """Hors périmètre = False, quels que soient prix, surface et pièces."""
        listing = make_listing(zip_code="99999", price_value=1000.0, surface="50", rooms="2")
        criteria = {"priceMin": 0, "priceMax": 10000, "surfaceMin": 1, "rooms": [2]}
        assert _passes_filters(listing, criteria, at("75014")) is False


class TestPassesFiltersPrice:
    @pytest.mark.parametrize(
        ("price_value", "criteria", "expected"),
        [
            (1000.0, {"priceMin": 900, "priceMax": 1100}, True),
            (1000.0, {"priceMax": 900}, False),
            (1000.0, {"priceMin": 1100}, False),
            # Bornes inclusives.
            (1000.0, {"priceMin": 1000, "priceMax": 1000}, True),
            # FAIL-OPEN : un prix illisible ne fait pas écarter l'annonce (les
            # cartes « nous consulter » restent visibles).
            (None, {"priceMin": 900, "priceMax": 1100}, True),
            (None, {"priceMax": 1}, True),
            # `if price_min and ...` : ZÉRO EST IGNORÉ. Une borne à 0 ne filtre
            # rien — inoffensif pour un minimum, mais un priceMax=0 laisse tout
            # passer au lieu de tout écarter.
            (1000.0, {"priceMin": 0}, True),
            (1000.0, {"priceMax": 0}, True),
            (0.0, {"priceMin": 1}, False),
            (0.0, {"priceMin": 0}, True),
            # Bornes absentes : aucun filtrage.
            (1000.0, {}, True),
            (1000.0, {"priceMin": None, "priceMax": None}, True),
        ],
    )
    def test_price_bounds(self, price_value, criteria, expected):
        listing = make_listing(zip_code="75014", price_value=price_value)
        assert _passes_filters(listing, criteria, at("75014")) is expected


class TestPassesFiltersSurface:
    @pytest.mark.parametrize(
        ("surface", "criteria", "expected"),
        [
            ("50", {"surfaceMin": 40, "surfaceMax": 60}, True),
            ("50", {"surfaceMin": 60}, False),
            ("50", {"surfaceMax": 40}, False),
            ("50", {"surfaceMin": 50, "surfaceMax": 50}, True),
            # La virgule décimale du site est convertie avant comparaison.
            ("50,5", {"surfaceMin": 51}, False),
            ("50,5", {"surfaceMin": 50}, True),
            ("50.5", {"surfaceMin": 51}, False),
            # Surface illisible : le filtre n'est pas appliqué (fail-open).
            ("abc", {"surfaceMin": 1000}, True),
            ("", {"surfaceMin": 1000}, True),
            # Zéro ignoré, comme pour le prix.
            ("50", {"surfaceMin": 0}, True),
            ("50", {"surfaceMax": 0}, True),
            ("50", {}, True),
        ],
    )
    def test_surface_bounds(self, surface, criteria, expected):
        listing = make_listing(zip_code="75014", surface=surface)
        assert _passes_filters(listing, criteria, at("75014")) is expected


class TestPassesFiltersRooms:
    @pytest.mark.parametrize(
        ("rooms", "criteria", "expected"),
        [
            ("2", {"rooms": [2, 3]}, True),
            ("3", {"rooms": [2, 3]}, True),
            ("2", {"rooms": [3, 4]}, False),
            ("2", {"rooms": ["2", "3"]}, True),
            # « 5 » signifie « 5 et plus » dans le vocabulaire partagé
            # (voir templates/search_edit.html : la dernière case s'affiche 5+).
            ("5", {"rooms": [5]}, True),
            ("6", {"rooms": [5]}, True),
            ("11", {"rooms": [5]}, True),
            ("4", {"rooms": [5]}, False),
            # Un critère combinant petit et grand accepte les deux bouts.
            ("2", {"rooms": [2, 5]}, True),
            ("9", {"rooms": [2, 5]}, True),
            # Filtre absent ou vide : aucun filtrage.
            ("2", {}, True),
            ("2", {"rooms": []}, True),
            ("2", {"rooms": None}, True),
            # Nombre de pièces illisible sur la carte : pas de filtrage.
            ("", {"rooms": [2]}, True),
        ],
    )
    def test_room_counts(self, rooms, criteria, expected):
        listing = make_listing(zip_code="75014", rooms=rooms)
        assert _passes_filters(listing, criteria, at("75014")) is expected

    @pytest.mark.parametrize(
        ("rooms", "rooms_filter"),
        [
            # Le nombre de pièces de l'annonce n'est pas un entier.
            ("deux", [2]),
            ("2,5", [2]),
            # Le critère lui-même est illisible.
            ("2", ["deux"]),
            ("2", [None]),
            ("2", [{"n": 2}]),
        ],
    )
    def test_an_unparseable_room_count_fails_open(self, rooms, rooms_filter):
        """FAIL-OPEN EXPLICITE : `except (ValueError, TypeError): return True`.
        L'annonce est gardée, à charge pour l'utilisateur d'écarter ce qui ne lui
        convient pas — l'inverse (tout jeter) ferait disparaître des annonces
        valides sur un simple libellé inattendu."""
        listing = make_listing(zip_code="75014", rooms=rooms)
        assert _passes_filters(listing, {"rooms": rooms_filter}, at("75014")) is True

    def test_asking_for_more_than_five_rooms_also_accepts_five(self):
        """Quirk de la règle « >= 5 vaut 5+ » : elle s'applique à TOUTE valeur
        >= 5, donc demander 6 pièces accepte aussi un 5 pièces. Sans effet
        aujourd'hui (le formulaire ne propose que 1 à 5), mais à savoir si la
        liste des cases à cocher s'allonge."""
        listing = make_listing(zip_code="75014", rooms="5")
        assert _passes_filters(listing, {"rooms": [6]}, at("75014")) is True


class TestPassesFiltersCombined:
    def test_a_fully_compliant_listing_passes_every_filter(self):
        listing = make_listing(zip_code="75014", price_value=1000.0, surface="50", rooms="2")
        criteria = {
            "priceMin": 900, "priceMax": 1100,
            "surfaceMin": 40, "surfaceMax": 60,
            "rooms": [2, 3],
        }
        assert _passes_filters(listing, criteria, at("75014")) is True

    def test_missing_data_never_excludes_except_the_location(self):
        """Le contraste tient en une ligne : prix, surface et pièces illisibles
        laissent passer, un code postal illisible non."""
        criteria = {"priceMin": 900, "surfaceMin": 40, "rooms": [2]}
        blurry = make_listing(zip_code="75014", price_value=None, surface="", rooms="")
        assert _passes_filters(blurry, criteria, at("75014")) is True

        without_zip = make_listing(zip_code="", price_value=1000.0, surface="50", rooms="2")
        assert _passes_filters(without_zip, criteria, at("75014")) is False


# ---------------------------------------------------------------------------
# to_native : rien à traduire
# ---------------------------------------------------------------------------

class TestToNative:
    def test_the_canonical_criteria_are_used_as_is(self):
        """Laforêt construit ses URLs directement depuis le canonique et applique
        les bornes côté scraper : aucune traduction, et surtout aucune copie
        modifiée qui divergerait de ce que lit _passes_filters."""
        criteria = {"locations": [PARIS_18], "priceMax": 1200}
        assert LaforetParser().to_native(criteria) is criteria


# ---------------------------------------------------------------------------
# _path_anchor / _base_path
# ---------------------------------------------------------------------------

class TestPathAnchor:
    """Le tronçon de chemin identifiant le périmètre : un niveau et un slug.

    Les communes passent par les pages /ville/ (inchangées). Départements et
    régions passent par leurs pages CANONIQUES, vérifiées en live le
    2026-08-23 — elles existent pour tous les départements et portent tous les
    filtres. L'ancienne ancre « ville principale + premier CP » est bannie :
    sur 37 départements sur 101 la page /ville/ n'existait pas et le site
    redirigeait (301), pire saint-denis-97400 rebasculait vers la recherche
    NATIONALE — le repli sans filtres balayait alors tout le stock français
    pour n'en retenir rien, silencieusement.
    """

    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            # Commune : page /ville/, slug + code postal — INCHANGÉ.
            (PARIS_18, {"level": "ville", "slug": "paris-75018"}),
            (
                {"city": "Paris", "postalCode": "75018"},
                {"level": "ville", "slug": "paris-75018"},
            ),
            # Ville entière : le plus petit code postal, pour un chemin stable.
            (PARIS_WHOLE, {"level": "ville", "slug": "paris-75001"}),
            (
                make_whole_city_location("Bordeaux", ("33800", "33000"), "33063"),
                {"level": "ville", "slug": "bordeaux-33000"},
            ),
        ],
    )
    def test_narrow_perimeters_anchor_on_their_own_city(self, location, expected):
        assert LaforetParser()._path_anchor(location) == expected

    def test_a_whole_city_without_postal_codes_has_no_anchor(self):
        assert LaforetParser()._path_anchor({"kind": "whole_city", "city": "Paris"}) is None

    def test_a_department_anchors_on_its_canonical_page(self):
        """Plus aucune dépendance à department_main_city : le slug vient du NOM
        du département (« Gironde »), pas d'une ville déduite."""
        assert LaforetParser()._path_anchor(GIRONDE) == {"level": "departement", "slug": "gironde"}

    def test_another_department_anchors_on_its_canonical_page(self):
        """Second département figé (Aube) : le slug ne doit rien devoir à une
        table de villes principales."""
        assert LaforetParser()._path_anchor(AUBE) == {"level": "departement", "slug": "aube"}

    def test_a_region_anchors_on_its_canonical_page(self):
        """L'ancre région est la page canonique, pas l'artefact paris-75001 qui
        paraissait ne pas filtrer."""
        assert LaforetParser()._path_anchor(IDF) == {
            "level": "region",
            "slug": "ile-de-france",
        }

    def test_a_composite_region_slug_is_built_from_the_name(self):
        """« Provence-Alpes-Côte d'Azur » -> provence-alpes-cote-dazur : slug
        composé vérifié en live (/region/location-appartement-provence-alpes-
        cote-dazur + dept13 -> 86 annonces)."""
        assert LaforetParser()._path_anchor(PACS) == {
            "level": "region",
            "slug": "provence-alpes-cote-dazur",
        }

    @pytest.mark.parametrize(
        ("location", "described"),
        [
            ({"kind": "department", "code": "33"}, "département 33"),
            ({"kind": "department", "name": "", "code": "33"}, "département 33"),
            ({"kind": "region", "name": "", "code": "94"}, "région 94"),
        ],
        ids=["departement_sans_nom", "nom_vide", "region_nom_vide"],
    )
    def test_no_anchor_without_a_name_and_a_loud_warning(self, location, described, logged):
        """Le code seul (« /departement/location-appartement-33 ») ne correspond
        à aucune page réelle : plutôt que d'inventer un lien mort, on renonce à
        l'ancre EN ALERTANT — l'ancien comportement échouait en silence."""
        assert LaforetParser()._path_anchor(location) is None
        assert any(
            level == "WARNING"
            and f"{described} : pas de nom pour construire l'URL canonique" in message
            for level, message in logged
        )

    def test_an_unknown_kind_has_no_anchor(self):
        """Un kind inattendu n'est jamais deviné (comportement de l'ancien code :
        il tombait dans _department_codes -> liste vide -> None)."""
        assert LaforetParser()._path_anchor({"kind": "zone", "city": "Paris"}) is None


class TestBasePath:
    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            ({}, f"{BASE_URL}/ville/location-appartement-paris-75018"),
            ({"transaction": "buy"}, f"{BASE_URL}/ville/achat-appartement-paris-75018"),
            (
                {"transaction": "buy", "propertyTypes": ["house"]},
                f"{BASE_URL}/ville/achat-maison-paris-75018",
            ),
            # Le slug porte le PREMIER type demandé — sans effet réel, puisque
            # filter[types][] prime sur lui (vérifié en live).
            ({"propertyTypes": ["house", "apartment"]}, f"{BASE_URL}/ville/location-maison-paris-75018"),
            # Les types hors capacités sont retirés avant de choisir le slug.
            ({"propertyTypes": ["parking", "house"]}, f"{BASE_URL}/ville/location-maison-paris-75018"),
        ],
    )
    def test_the_path_carries_the_transaction_the_type_and_the_city(self, criteria, expected):
        assert LaforetParser()._base_path(criteria, PARIS_18) == expected

    def test_the_city_is_slugified(self):
        location = make_city_location("Le Kremlin-Bicêtre", "94270", "94043")
        assert LaforetParser()._base_path({}, location) == (
            f"{BASE_URL}/ville/location-appartement-le-kremlin-bicetre-94270"
        )

    def test_no_path_without_an_anchor(self):
        """Un département sans nom n'a pas de page canonique constructible :
        plutôt qu'un lien mort (404), aucun chemin."""
        assert LaforetParser()._base_path({}, {"kind": "department", "code": "33"}) is None

    def test_a_department_path_is_the_canonical_page(self):
        assert LaforetParser()._base_path({}, GIRONDE) == (
            f"{BASE_URL}/departement/location-appartement-gironde"
        )

    def test_only_unsupported_types_would_break_the_path(self):
        """Fragilité latente, inatteignable par l'API publique : `_base_path`
        indexe `_property_types(criteria)[0]` sans garde. Les deux appelants
        (build_search_urls et scrape) vérifient la liste AVANT, ce que prouvent
        TestPropertyTypes.test_only_unsupported_types_produce_no_url_and_no_scrape
        et le test ci-dessous."""
        with pytest.raises(IndexError):
            LaforetParser()._base_path({"propertyTypes": ["parking"]}, PARIS_18)


# ---------------------------------------------------------------------------
# _type_filters / _criteria_filters / _location_filters
# ---------------------------------------------------------------------------

class TestTypeFilters:
    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            ({}, [("filter[types][]", "apartment")]),
            ({"propertyTypes": ["house"]}, [("filter[types][]", "house")]),
            # Le paramètre est répétable et c'est LUI qui gouverne le résultat.
            (
                {"propertyTypes": ["apartment", "house"]},
                [("filter[types][]", "apartment"), ("filter[types][]", "house")],
            ),
            ({"propertyTypes": ["apartment", "parking"]}, [("filter[types][]", "apartment")]),
            ({"propertyTypes": ["parking"]}, []),
        ],
    )
    def test_one_filter_per_requested_type(self, criteria, expected):
        assert LaforetParser()._type_filters(criteria) == expected


class TestCriteriaFilters:
    """Sémantiques vérifiées côté site :
        filter[min]/filter[max]  bornes de prix
        filter[surface]          surface MINIMUM (aucun filtre de maximum)
        filter[rooms]            nombre de pièces MINIMUM, pas une égalité
                                 (rooms=3 rend du 3, 4, 5 et 6 pièces)
    """

    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            ({}, []),
            ({"priceMin": 800}, [("filter[min]", "800")]),
            ({"priceMax": 1200}, [("filter[max]", "1200")]),
            # L'ordre est stable : min, max, surface, pièces.
            (
                {"priceMin": 800, "priceMax": 1200},
                [("filter[min]", "800"), ("filter[max]", "1200")],
            ),
            # Il n'existe AUCUN filtre de surface maximale sur le site : seule
            # la borne minimale part (le reste est appliqué côté client).
            ({"surfaceMin": 30}, [("filter[surface]", "30")]),
            ({"surfaceMax": 80}, []),
            ({"surfaceMin": 30, "surfaceMax": 80}, [("filter[surface]", "30")]),
            # Le paramètre étant un MINIMUM, seul le plus petit nombre de pièces
            # demandé peut partir sans risquer d'exclure une annonce voulue.
            ({"rooms": [3, 4]}, [("filter[rooms]", "3")]),
            ({"rooms": [5]}, [("filter[rooms]", "5")]),
            ({"rooms": [4, 2, 3]}, [("filter[rooms]", "2")]),
            # Un minimum de 1 n'écarte rien : autant ne pas l'envoyer.
            ({"rooms": [1]}, []),
            ({"rooms": [1, 2, 3]}, []),
            ({"rooms": []}, []),
            ({"rooms": [0]}, []),
            # Valeurs nulles ou fausses : rien n'est envoyé.
            ({"priceMin": 0, "priceMax": 0, "surfaceMin": 0}, []),
            ({"priceMin": None, "priceMax": None, "surfaceMin": None, "rooms": None}, []),
            # Tout ensemble, dans l'ordre.
            (
                {"priceMin": 800, "priceMax": 1200, "surfaceMin": 30, "surfaceMax": 80, "rooms": [3, 4]},
                [
                    ("filter[min]", "800"),
                    ("filter[max]", "1200"),
                    ("filter[surface]", "30"),
                    ("filter[rooms]", "3"),
                ],
            ),
        ],
    )
    def test_price_surface_and_rooms_filters(self, criteria, expected):
        assert LaforetParser()._criteria_filters(criteria) == expected

    def test_room_counts_stored_as_strings_crash_the_comparison(self):
        """BUG : `min(rooms) > 1` compare l'élément le plus petit à un ENTIER.
        Avec des pièces en chaînes — le format de l'ancien vocabulaire, qui
        venait directement des cases à cocher — la comparaison lève un TypeError
        et fait échouer tout le scrape.

        core.criteria.normalize_criteria() convertit `rooms` en entiers et est
        appliqué à la lecture des recherches (SearchRepository), donc le chemin
        nominal est protégé ; tout appelant qui passe des critères NON normalisés
        (un test, un script, une future API) déclenche le crash. Le correctif
        serait de convertir en entiers ici aussi.
        """
        with pytest.raises(TypeError, match="not supported between instances of 'str' and 'int'"):
            LaforetParser()._criteria_filters({"rooms": ["2", "3"]})


class TestLocationFilters:
    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            # Chaque niveau passe par le filtre que Laforêt lui destine.
            (PARIS_18, [("filter[cities][]", "75118")]),
            (PARIS_WHOLE, [("filter[cities][]", "75056")]),
            (GIRONDE, [("filter[departments][]", "33")]),
            (
                make_region_location("11", "Île-de-France", ("75", "92")),
                [("filter[departments][]", "75"), ("filter[departments][]", "92")],
            ),
        ],
    )
    def test_one_filter_family_per_perimeter_level(self, location, expected):
        assert LaforetParser()._location_filters(location) == expected

    def test_a_perimeter_without_any_code_has_no_filter(self):
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            location = {"kind": "city", "city": "Nawak", "postalCode": "99999"}
            assert LaforetParser()._location_filters(location) == []


# ---------------------------------------------------------------------------
# _split_locations
# ---------------------------------------------------------------------------

class TestSplitLocations:
    def test_every_resolvable_perimeter_goes_into_the_merged_request(self):
        parser = LaforetParser()
        filterable, filters, plain = parser._split_locations(
            {"locations": [PARIS_18, LYON_7, GIRONDE]}
        )

        assert filterable == [PARIS_18, LYON_7, GIRONDE]
        assert filters == [
            ("filter[cities][]", "75118"),
            ("filter[cities][]", "69387"),
            ("filter[departments][]", "33"),
        ]
        assert plain == []

    def test_an_unfilterable_perimeter_is_set_aside_with_a_warning(self, logged):
        """Une commune dont le code INSEE n'a pas pu être résolu prend sa propre
        requête sur la page ville nue, plutôt que d'être silencieusement
        abandonnée."""
        parser = LaforetParser()
        nawak = {"kind": "city", "city": "Nawak", "postalCode": "99999"}

        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            filterable, filters, plain = parser._split_locations({"locations": [nawak]})

        assert (filterable, filters) == ([], [])
        assert plain == [nawak]
        assert any(
            level == "WARNING" and "Périmètre non filtrable (Nawak 99999)" in message
            for level, message in logged
        )

    def test_a_mixed_search_is_partitioned_in_order(self):
        parser = LaforetParser()
        nawak = {"kind": "city", "city": "Nawak", "postalCode": "99999"}

        with patch("parsers.laforet._resolve_insee_code", side_effect=_arrondissement_insee_code):
            filterable, filters, plain = parser._split_locations(
                {"locations": [PARIS_14, nawak, LYON_7]}
            )

        assert filterable == [PARIS_14, LYON_7]
        assert filters == [("filter[cities][]", "75114"), ("filter[cities][]", "69387")]
        assert plain == [nawak]

    def test_no_location_gives_three_empty_buckets(self):
        assert LaforetParser()._split_locations({}) == ([], [], [])


# ---------------------------------------------------------------------------
# build_search_url / build_search_urls
# ---------------------------------------------------------------------------

class TestBuildSearchUrls:
    """« Voir l'URL » doit montrer la vérité : build_search_urls() reproduit
    exactement la stratégie de scrape() — une URL fusionnée pour tous les
    périmètres filtrables, plus une URL nue par périmètre qui ne l'est pas."""

    def test_the_full_url_of_a_complete_search(self):
        """Assertion sur l'URL COMPLÈTE : c'est l'ordre des paramètres
        (types, puis périmètres, puis critères) qui est figé ici, pas seulement
        leur présence."""
        criteria = {
            "locations": [PARIS_14, GIRONDE],
            "transaction": "rent",
            "propertyTypes": ["apartment"],
            "priceMin": 800,
            "priceMax": 1200,
            "surfaceMin": 30,
            "surfaceMax": 80,
            "rooms": [3, 4],
        }
        urls = LaforetParser().build_search_urls(criteria)

        assert urls == [
            f"{BASE_URL}/ville/location-appartement-paris-75014"
            "?filter%5Btypes%5D%5B%5D=apartment"
            "&filter%5Bcities%5D%5B%5D=75114"
            "&filter%5Bdepartments%5D%5B%5D=33"
            "&filter%5Bmin%5D=800"
            "&filter%5Bmax%5D=1200"
            "&filter%5Bsurface%5D=30"
            "&filter%5Brooms%5D=3"
        ]

    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            (
                {"city": "Paris", "postalCode": "75018"},
                f"{BASE_URL}/ville/location-appartement-paris-75018"
                "?filter%5Btypes%5D%5B%5D=apartment&filter%5Bcities%5D%5B%5D=75118",
            ),
            (
                {
                    "city": "Lyon", "postalCode": "69007",
                    "transaction": "buy", "propertyTypes": ["house"],
                },
                f"{BASE_URL}/ville/achat-maison-lyon-69007"
                "?filter%5Btypes%5D%5B%5D=house&filter%5Bcities%5D%5B%5D=69387",
            ),
            (
                {"city": "Lyon", "postalCode": "69007", "propertyTypes": ["apartment", "house"]},
                f"{BASE_URL}/ville/location-appartement-lyon-69007"
                "?filter%5Btypes%5D%5B%5D=apartment&filter%5Btypes%5D%5B%5D=house"
                "&filter%5Bcities%5D%5B%5D=69387",
            ),
            # « appartement + parking » doit tout de même chercher les
            # appartements, et ne jamais mentionner le parking.
            (
                {"city": "Paris", "postalCode": "75018", "propertyTypes": ["apartment", "parking"]},
                f"{BASE_URL}/ville/location-appartement-paris-75018"
                "?filter%5Btypes%5D%5B%5D=apartment&filter%5Bcities%5D%5B%5D=75118",
            ),
        ],
        ids=["location_appartement", "achat_maison", "deux_types", "type_mixte"],
    )
    def test_single_city_urls(self, criteria, expected):
        """Le code INSEE d'arrondissement vient de la formule pure de
        core.geocode : aucun appel réseau dans ces cas."""
        assert LaforetParser().build_search_urls(criteria) == [expected]

    def test_several_locations_are_merged_into_one_url(self):
        urls = LaforetParser().build_search_urls({"locations": [PARIS_14, LYON_7]})
        assert urls == [
            f"{BASE_URL}/ville/location-appartement-paris-75014"
            "?filter%5Btypes%5D%5B%5D=apartment"
            "&filter%5Bcities%5D%5B%5D=75114&filter%5Bcities%5D%5B%5D=69387"
        ]

    def test_perimeter_levels_are_combined_into_a_single_url(self):
        """Communes et départements dans la même recherche : une seule requête,
        puisque le site combine les filtres en UNION (vérifié en live :
        cities=33063 rend 149 annonces, departments=75 en rend 824, les deux
        ensemble 973)."""
        urls = LaforetParser().build_search_urls({"locations": [GIRONDE, PARIS_14]})

        assert len(urls) == 1
        assert "filter%5Bdepartments%5D%5B%5D=33" in urls[0]
        assert "filter%5Bcities%5D%5B%5D=75114" in urls[0]
        # Le chemin est ancré sur la page canonique du premier périmètre.
        assert urls[0].startswith(f"{BASE_URL}/departement/location-appartement-gironde?")

    def test_a_department_url(self):
        """Ancre canonique /departement/ : la page existe pour tous les
        départements (vérifié en live) et porte tous les filtres — l'ancienne
        ancre ville-principale redirigeait (301) sur 37 départements sur 101."""
        urls = LaforetParser().build_search_urls({"locations": [GIRONDE]})

        assert urls == [
            f"{BASE_URL}/departement/location-appartement-gironde"
            "?filter%5Btypes%5D%5B%5D=apartment&filter%5Bdepartments%5D%5B%5D=33"
        ]
        # Surtout pas `filter[department]` au singulier : il ne filtre rien et
        # renvoie le flux national (2545 annonces, de l'Ain aux Pyrénées).
        assert "filter%5Bdepartment%5D=" not in urls[0]
        assert "filter%5Bcities%5D" not in urls[0]

    def test_a_region_becomes_all_its_departments(self):
        urls = LaforetParser().build_search_urls({"locations": [IDF]})

        # Ancre canonique /region/, vérifiée en live (931 annonces avec ses
        # 8 départements).
        assert urls[0].startswith(f"{BASE_URL}/region/location-appartement-ile-de-france?")
        for department in IDF["departments"]:
            assert f"filter%5Bdepartments%5D%5B%5D={department}" in urls[0]
        # Le filtre région natif existe et donne le même résultat, mais les
        # départements rendent le périmètre explicite dans l'URL.
        assert "filter%5Bregions%5D" not in urls[0]

    def test_a_region_without_stored_departments_is_resolved(self):
        with patch("parsers.laforet.region_departments", return_value=["2A", "2B"]) as mock_region:
            urls = LaforetParser().build_search_urls(
                {"locations": [{"kind": "region", "name": "Corse", "code": "94"}]}
            )

        mock_region.assert_called_with("94")
        # Le slug vient du NOM (« corse »), plus aucune ville principale déduite.
        assert urls[0].startswith(f"{BASE_URL}/region/location-appartement-corse?")
        assert "filter%5Bdepartments%5D%5B%5D=2A" in urls[0]
        assert "filter%5Bdepartments%5D%5B%5D=2B" in urls[0]

    def test_a_whole_city_uses_the_single_commune_code(self):
        urls = LaforetParser().build_search_urls({"locations": [PARIS_WHOLE]})

        assert "filter%5Bcities%5D%5B%5D=75056" in urls[0]
        # Aucun code d'arrondissement : on n'énumère plus.
        for insee in ("75101", "75102", "75115"):
            assert insee not in urls[0]

    def test_a_whole_city_falls_back_to_its_postal_codes(self):
        criteria = {"locations": [{"kind": "whole_city", "city": "Paris", "postalCodes": ["75001", "75015"]}]}
        with patch("parsers.laforet._resolve_insee_code", side_effect=_arrondissement_insee_code):
            urls = LaforetParser().build_search_urls(criteria)

        assert "filter%5Bcities%5D%5B%5D=75101" in urls[0]
        assert "filter%5Bcities%5D%5B%5D=75115" in urls[0]

    def test_an_unresolvable_location_gets_a_plain_url(self):
        """Un code postal irrésoluble (API géo muette, code étranger) reçoit un
        lien nu — c'est ce que scrape() fera aussi, plutôt qu'un lien fusionné
        cassé."""
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            urls = LaforetParser().build_search_urls({"city": "Paris", "postalCode": "75018"})

        assert urls == [f"{BASE_URL}/ville/location-appartement-paris-75018"]
        assert "filter" not in urls[0]

    def test_a_plain_url_never_carries_the_criteria_filters(self):
        """L'INVARIANT du module : filter[min]/[max]/[surface]/[rooms] ne
        partent QU'AVEC un filtre de périmètre. Seuls, ils font basculer le site
        en recherche NATIONALE et le cadrage du chemin est perdu — vérifié en
        live, une page /paris-75014 avec un filtre prix rend 39 annonces dont 39
        hors du 75014 (Bordeaux, Lyon, Chambéry...)."""
        criteria = {
            "city": "Paris", "postalCode": "75014",
            "priceMin": 800, "priceMax": 1200, "surfaceMin": 30, "rooms": [3],
        }
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            urls = LaforetParser().build_search_urls(criteria)

        assert urls == [f"{BASE_URL}/ville/location-appartement-paris-75014"]
        assert "?" not in urls[0]

    def test_the_criteria_filters_do_go_out_with_a_perimeter(self):
        """Le pendant du test précédent : avec un périmètre, le cadrage tient et
        le gain est net (sur la Gironde, la première page passe de 18 à 39
        annonces effectivement dans le budget demandé)."""
        urls = LaforetParser().build_search_urls({
            "locations": [PARIS_14], "priceMax": 1200, "surfaceMin": 30, "rooms": [3],
        })
        assert "filter%5Bcities%5D%5B%5D=75114" in urls[0]
        assert "filter%5Bmax%5D=1200" in urls[0]
        assert "filter%5Bsurface%5D=30" in urls[0]
        assert "filter%5Brooms%5D=3" in urls[0]

    def test_a_mix_of_resolvable_and_unresolvable_locations(self):
        criteria = {"locations": [PARIS_14, {"city": "Nawak", "postalCode": "99999"}]}
        with patch("parsers.laforet._resolve_insee_code", side_effect=_arrondissement_insee_code):
            urls = LaforetParser().build_search_urls(criteria)

        assert len(urls) == 2
        assert "filter%5Bcities%5D%5B%5D=75114" in urls[0]
        assert urls[1] == f"{BASE_URL}/ville/location-appartement-nawak-99999"

    @pytest.mark.parametrize(
        "criteria",
        [{}, {"city": "Paris"}, {"postalCode": "75018"}, {"locations": []}],
        ids=["vide", "sans_code_postal", "sans_ville", "liste_vide"],
    )
    def test_no_url_without_a_location(self, criteria):
        assert LaforetParser().build_search_urls(criteria) == []
        assert LaforetParser().build_search_url(criteria) is None

    def test_no_url_when_the_department_has_no_name(self):
        """Sans nom, pas de page canonique constructible — et un chemin inventé
        renvoie 404 : mieux vaut aucune URL qu'un lien mort."""
        assert LaforetParser().build_search_urls({"locations": [{"kind": "department", "code": "33"}]}) == []

    def test_an_unanchorable_perimeter_does_not_hide_the_others(self):
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            urls = LaforetParser().build_search_urls({
                "locations": [{"kind": "department", "code": "33"}, {"city": "Paris", "postalCode": "75014"}],
            })
        assert urls == [f"{BASE_URL}/ville/location-appartement-paris-75014"]

    def test_build_search_url_returns_the_first_url(self):
        criteria = {"locations": [PARIS_14, {"city": "Nawak", "postalCode": "99999"}]}
        with patch("parsers.laforet._resolve_insee_code", side_effect=_arrondissement_insee_code):
            parser = LaforetParser()
            assert parser.build_search_url(criteria) == parser.build_search_urls(criteria)[0]


# ---------------------------------------------------------------------------
# has_valid_criteria / cannot_search_reason
# ---------------------------------------------------------------------------

class TestValidity:
    @pytest.mark.parametrize(
        ("criteria", "expected"),
        [
            ({"city": "Paris", "postalCode": "75018"}, True),
            ({"locations": [PARIS_18]}, True),
            ({"locations": [PARIS_18, LYON_7]}, True),
            ({"locations": [PARIS_WHOLE]}, True),
            # Une recherche départementale ou régionale n'a ni ville ni code
            # postal : elle doit rester valide.
            ({"locations": [GIRONDE]}, True),
            ({"locations": [IDF]}, True),
            # Ville tapée à la main sans code INSEE : valide quand même, le code
            # sera résolu depuis le code postal au moment du scrape.
            ({"locations": [{"city": "Bordeaux", "postalCode": "33000"}]}, True),
            ({"city": "Paris"}, False),
            ({"locations": []}, False),
            ({}, False),
        ],
    )
    def test_a_city_and_a_postal_code_are_enough(self, criteria, expected):
        """Laforêt ne surcharge pas has_valid_criteria : le contrat par défaut de
        BaseParser suffit, le code INSEE dont filter[cities][] a besoin étant
        résolu depuis le code postal quand l'autocomplete ne l'a pas fourni."""
        assert LaforetParser().has_valid_criteria(criteria) is expected

    @pytest.mark.parametrize(
        "criteria",
        [{"locations": [PARIS_18]}, {"locations": [GIRONDE]}, {"city": "Bordeaux", "postalCode": "33000"}],
        ids=["commune", "departement", "sans_insee"],
    )
    def test_validation_never_resolves_anything(self, criteria):
        """Créer une recherche ne doit dépendre ni de l'API géo ni de Laforêt."""
        with patch("parsers.laforet._resolve_insee_code") as mock_resolve:
            assert LaforetParser().has_valid_criteria(criteria) is True
            assert LaforetParser().cannot_search_reason(criteria) is None

        mock_resolve.assert_not_called()

    def test_no_location_is_reported_before_the_capabilities(self):
        assert LaforetParser().cannot_search_reason({"propertyTypes": ["parking"]}) == (
            "aucune localisation exploitable (ville + code postal requis)"
        )

    @pytest.mark.parametrize(
        ("property_types", "expected"),
        [
            (["parking"], "Laforêt ne référence pas les biens de type « Parking »"),
            (["land"], "Laforêt ne référence pas les biens de type « Terrain »"),
            (
                ["parking", "land"],
                "Laforêt ne référence pas les biens de type « Parking » "
                "ni les biens de type « Terrain »",
            ),
            (["apartment", "house"], None),
        ],
    )
    def test_unsupported_property_types_are_named(self, property_types, expected):
        criteria = {"locations": [PARIS_18], "propertyTypes": property_types}
        assert LaforetParser().cannot_search_reason(criteria) == expected

    def test_both_transactions_are_supported(self):
        for transaction in ("rent", "buy"):
            criteria = {"locations": [PARIS_18], "transaction": transaction}
            assert LaforetParser().cannot_search_reason(criteria) is None


# ---------------------------------------------------------------------------
# URL_NOTE — la note affichée à côté du lien « Voir l'URL » (routes/api.py)
# ---------------------------------------------------------------------------

class TestUrlNote:
    """Issue #13 : trois limites du site doivent être dites à l'utilisateur
    dans la note, puisqu'aucune n'est corrigeable par une URL."""

    def test_the_note_explains_the_communal_granularity(self):
        """P1 : un code postal ne peut pas être isolé de sa commune — Laforêt
        ne scopre qu'à la commune, le scraper re-filtre ensuite côté serveur."""
        note = LaforetParser.URL_NOTE
        assert "commune" in note
        assert "code postal" in note

    def test_the_note_warns_about_the_site_map_losing_the_perimeter(self):
        """P4 : bug front chez l'éditeur — searchOnMove() jette cities et
        sérialise mal les tableaux ; la carte peut induire en erreur."""
        note = LaforetParser.URL_NOTE
        assert "carte" in note and "périmètre" in note

    def test_the_note_names_the_uncovered_outre_mer_departements(self):
        """P5 : les DOM-ROM ne sont pas couverts par le moteur du site."""
        note = LaforetParser.URL_NOTE
        assert "outre-mer" in note
        for code in DOM_ROM_DEPARTMENTS:
            assert code in note

    def test_the_existing_caveats_are_still_documented(self):
        """Non-régression : surface max absente et pièces au minimum."""
        note = LaforetParser.URL_NOTE
        assert "surface maximale" in note
        assert "minimum" in note


# ---------------------------------------------------------------------------
# scrape : garde-fous d'entrée et session
# ---------------------------------------------------------------------------

class TestScrapeGuards:
    @pytest.mark.parametrize(
        "criteria",
        [{}, {"city": "Paris"}, {"locations": []}, {"locations": [{"city": "Paris"}]}],
        ids=["vide", "sans_code_postal", "liste_vide", "localisation_incomplete"],
    )
    def test_no_location_raises_before_any_request(self, criteria):
        with pytest.raises(ValueError, match="nécessite au moins une localisation"):
            LaforetParser().scrape(criteria)

    def test_only_unsupported_types_raises_and_names_what_is_covered(self):
        with pytest.raises(ValueError, match=r"uniquement \['apartment', 'house'\]"):
            LaforetParser().scrape({"locations": [PARIS_18], "propertyTypes": ["parking"]})

    def test_the_session_announces_a_desktop_browser(self):
        session = FakeSession([SAMPLE_PAGE_HTML])
        run_scrape({"locations": [PARIS_18]}, session)

        assert session.headers["User-Agent"] == DESKTOP_UA
        assert session.headers["Accept"] == "text/html, application/xhtml+xml"

    def test_every_request_is_bounded_by_a_timeout(self):
        """Sans timeout, un scrape peut rester bloqué indéfiniment et tenir le
        thread de fond du scheduler."""
        session = FakeSession([SAMPLE_PAGE_HTML])
        run_scrape({"locations": [PARIS_18]}, session)

        assert session.calls
        assert all(call["timeout"] == 15 for call in session.calls)


# ---------------------------------------------------------------------------
# scrape : requête fusionnée
# ---------------------------------------------------------------------------

class TestScrapeMerged:
    def test_all_perimeters_travel_in_one_request(self):
        """Vérifié en live : filter[cities][] fusionne réellement plusieurs
        villes en une requête correctement cadrée.

        Le nombre d'appels n'est pas 1 mais 2 : la pagination lit une page de
        plus pour constater qu'il n'y a rien de nouveau (voir _collect_pages).
        Ce qui compte est que TOUS les périmètres soient dans la même requête.
        """
        session = FakeSession([MERGED_PAGE_HTML])
        listings = run_scrape({"locations": [PARIS_18, LYON_7]}, session)

        assert {li.zip_code for li in listings} == {"75018", "69007"}
        assert len(listings) == 2
        # Une seule URL de base, portant les deux villes.
        assert set(session.urls) == {f"{BASE_URL}/ville/location-appartement-paris-75018"}
        assert set(session.param_values("filter[cities][]")) == {"75118", "69387"}
        assert session.plain_calls == []

    def test_the_merged_request_carries_the_types_and_the_criteria(self):
        session = FakeSession([MERGED_PAGE_HTML])
        run_scrape(
            {"locations": [PARIS_18, LYON_7], "priceMax": 1200, "surfaceMin": 25, "rooms": [2, 3]},
            session,
        )

        params = session.merged_calls[0]["params"]
        assert params == [
            ("filter[types][]", "apartment"),
            ("filter[cities][]", "75118"),
            ("filter[cities][]", "69387"),
            ("filter[max]", "1200"),
            ("filter[surface]", "25"),
            ("filter[rooms]", "2"),
        ]

    def test_the_page_parameter_is_appended_only_from_page_two(self):
        session = FakeSession([
            page(card("111")),
            page(card("222")),
            page(card("222")),
        ])
        run_scrape({"locations": [PARIS_18]}, session)

        pages = [
            [value for key, value in call["params"] if key == "page"]
            for call in session.merged_calls
        ]
        assert pages == [[], [2], [3]]

    def test_a_404_on_the_merged_request_has_a_dedicated_message(self):
        """Un chemin de ville inventé renvoie 404 : le dire clairement plutôt que
        laisser remonter un HTTPError opaque."""
        session = FakeSession([FakeResponse("", status_code=404)])
        with pytest.raises(ValueError, match=r"requête fusionnée: URL de base invalide"):
            run_scrape({"locations": [PARIS_18]}, session)

    def test_any_other_http_error_is_raised_by_raise_for_status(self):
        session = FakeSession([FakeResponse("", status_code=500)])
        with pytest.raises(ValueError, match="500 Server Error"):
            run_scrape({"locations": [PARIS_18]}, session)

    def test_no_anchor_for_the_merged_url_fails_loudly(self):
        """Un périmètre filtrable mais sans ancre possible (département sans
        nom) : la requête fusionnée échoue, et le repli par périmètre échoue de
        même — donc tout échoue, bruyamment, au lieu d'un succès vide."""
        session = FakeSession([SAMPLE_PAGE_HTML])
        with pytest.raises(ValueError, match="aucune page pour ancrer l'URL"):
            run_scrape({"locations": [{"kind": "department", "code": "33"}]}, session)
        assert session.calls == []


# ---------------------------------------------------------------------------
# scrape : pagination (_collect_pages)
# ---------------------------------------------------------------------------

class TestPagination:
    def test_it_stops_on_the_first_page_without_a_new_listing(self):
        """SAMPLE_PAGE_HTML annonce 3 pages dans son bloc JSON-LD ItemList : la
        boucle l'IGNORE et s'arrête d'elle-même dès qu'une page n'apporte plus
        rien d'inédit (2 requêtes : la page 1 puis la page 2 qui répète)."""
        session = FakeSession([SAMPLE_PAGE_HTML])
        listings = run_scrape({"locations": [PARIS_18]}, session)

        assert len(session.calls) == 2
        # Une seule des deux cartes est dans le périmètre demandé.
        assert [li.listing_id for li in listings] == ["lf_52811904"]

    def test_pagination_continues_while_new_listings_appear(self):
        """Régression : la boucle s'arrêtait après la première page parce que le
        nombre de pages était lu dans un bloc JSON-LD ItemList qui DISPARAÎT dès
        qu'un filtre est envoyé.

        Constaté en live sur une recherche Île-de-France à 850-870 € et
        25-30 m² : 2 annonces retenues au lieu de 4 (les 97 résultats annoncés
        par le site tenaient sur 3 pages, on n'en lisait qu'une). Les deux
        manquantes, à Vitry-sur-Seine et Suresnes, attendaient en page 2.
        """
        # Trois pages qui apportent chacune du neuf, puis une quatrième sans
        # rien d'inédit. Aucun ItemList nulle part, comme sur le vrai site dès
        # qu'un filtre est présent.
        session = FakeSession([
            page(card("111"), card("222")),
            page(card("222"), card("333")),
            page(card("444")),
            page(card("444")),
        ])
        listings = run_scrape({"locations": [PARIS_18]}, session)

        assert {li.legacy_id for li in listings} == {"111", "222", "333", "444"}
        assert len(session.calls) == 4

    def test_overlapping_pages_are_absorbed(self):
        """Compter les annonces inédites plutôt que les pages absorbe le
        chevauchement entre pages consécutives (page 2 rend 40 cartes dont
        seulement 20 nouvelles)."""
        session = FakeSession([
            page(card("111"), card("222")),
            page(card("111"), card("222"), card("333")),
            page(card("111"), card("222"), card("333")),
        ])
        listings = run_scrape({"locations": [PARIS_18]}, session)

        assert [li.legacy_id for li in listings] == ["111", "222", "333"]
        assert len(session.calls) == 3

    def test_a_page_full_of_rejected_cards_still_counts_as_progress(self):
        """`vues_ici` inclut les annonces ÉCARTÉES par les filtres : une page
        entièrement hors périmètre est bien « nouvelle », donc la pagination
        continue au lieu de s'arrêter avant les vrais résultats."""
        session = FakeSession([
            page(card("111", city="ROUBAIX", zip_code="59100")),
            page(card("222", city="PARIS", zip_code="75018")),
            page(card("222", city="PARIS", zip_code="75018")),
        ])
        listings = run_scrape({"locations": [PARIS_18]}, session)

        assert [li.legacy_id for li in listings] == ["222"]
        assert len(session.calls) == 3

    def test_an_empty_first_page_stops_immediately(self):
        session = FakeSession([EMPTY_PAGE_HTML])
        assert run_scrape({"locations": [PARIS_18]}, session) == []
        assert len(session.calls) == 1

    def test_the_page_limit_is_enforced_and_reported(self, logged):
        """MAX_PAGES borne le parcours : une recherche large sans filtre serré
        pourrait sinon enchaîner les requêtes très longtemps. Le résultat est
        alors possiblement partiel, et doit le dire."""
        session = FakeSession([page(card(str(i))) for i in range(1, MAX_PAGES + 1)])
        listings = run_scrape({"locations": [PARIS_18]}, session)

        assert len(session.calls) == MAX_PAGES
        assert len(listings) == MAX_PAGES
        assert any(
            level == "WARNING" and f"limite de {MAX_PAGES} pages atteinte" in message
            for level, message in logged
        )

    def test_the_noise_section_is_excluded_page_after_page(self):
        """_extract_genuine_section est appliqué à CHAQUE page, pas seulement à
        la première."""
        noisy = page(card("111")) + "proximité de Paris" + page(card("999"))
        session = FakeSession([noisy])
        listings = run_scrape({"locations": [PARIS_18]}, session)

        assert [li.legacy_id for li in listings] == ["111"]


class TestLoudEmptyPage:
    """Une page HTTP 200 qui ne rend AUCUNE carte sur une requête portant des
    filtres de périmètre est SUSPECTE : soit le périmètre est vraiment vide,
    soit la page n'est pas celle attendue (redirection 301 vers le national,
    changement de balisage). Impossible de trancher sans voir la page — donc on
    alerte au lieu d'un succès vide silencieux. C'est ce silence qui a rendu si
    longs à détecter saint-denis-97400 rebasculant en recherche nationale
    (issue #13) : 0 annonce présenté comme un succès."""

    def test_an_empty_first_page_with_scope_filters_warns(self, logged):
        session = FakeSession([EMPTY_PAGE_HTML])
        listings = run_scrape({"locations": [PARIS_18]}, session)

        assert listings == []
        assert len(session.calls) == 1
        assert any(
            level == "WARNING"
            and "requête fusionnée : page 1 en HTTP 200 mais aucune carte parsée" in message
            for level, message in logged
        )

    def test_an_empty_page_without_scope_filters_stays_silent(self, logged):
        """Le repli plain SANS filtre de périmètre est supposé pouvoir être
        vide (c'est voulu : seuls, les critères prix basculeraient en national) :
        pas d'alerte dessus."""
        session = FakeSession([EMPTY_PAGE_HTML])
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            listings = run_scrape({"city": "Nawak", "postalCode": "99999"}, session)

        assert listings == []
        assert not any("aucune carte parsée" in message for _, message in logged)

    def test_a_legitimate_end_of_pagination_does_not_warn(self, logged):
        """Page 1 remplie puis page 2 vide : fin normale d'une pagination, rien
        à signaler."""
        session = FakeSession([page(card("111")), EMPTY_PAGE_HTML])
        listings = run_scrape({"locations": [PARIS_18]}, session)

        assert [li.legacy_id for li in listings] == ["111"]
        assert not any("aucune carte parsée" in message for _, message in logged)

    def test_a_last_page_of_overlapping_cards_does_not_warn(self, logged):
        """Fin par chevauchement (page suivante = cartes déjà vues) : la page
        n'est PAS vide, elle n'a juste plus rien d'inédit — aucun avertissement."""
        session = FakeSession([page(card("111")), page(card("111"))])
        listings = run_scrape({"locations": [PARIS_18]}, session)

        assert [li.legacy_id for li in listings] == ["111"]
        assert not any("aucune carte parsée" in message for _, message in logged)


# ---------------------------------------------------------------------------
# scrape : repli par périmètre (_scrape_location) et dégradation partielle
# ---------------------------------------------------------------------------

class TestScrapePerLocationFallback:
    def test_an_unresolvable_location_gets_its_own_plain_request(self):
        """Une localisation dont le code postal ne se résout pas (API géo
        muette, code étranger) ne doit pas être silencieusement abandonnée."""
        session = FakeSession([SAMPLE_PAGE_HTML])
        with patch("parsers.laforet._resolve_insee_code", side_effect=_arrondissement_insee_code):
            listings = run_scrape({"locations": [{"city": "Poitiers", "postalCode": "86000"}]}, session)

        # SAMPLE_PAGE_HTML ne contient aucune annonce à Poitiers.
        assert listings == []
        assert session.merged_calls == []
        assert session.plain_calls[0]["url"] == f"{BASE_URL}/ville/location-appartement-poitiers-86000"

    def test_the_plain_path_paginates_with_a_query_string(self):
        session = FakeSession([page(card("111")), page(card("222")), page(card("222"))])
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            run_scrape({"city": "Paris", "postalCode": "75018"}, session)

        base = f"{BASE_URL}/ville/location-appartement-paris-75018"
        assert session.urls == [base, f"{base}?page=2", f"{base}?page=3"]

    def test_a_404_on_a_plain_request_has_its_own_message(self):
        session = FakeSession([FakeResponse("", status_code=404)])
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            with pytest.raises(ValueError, match=r"ville/code postal invalide"):
                run_scrape({"city": "Nawak", "postalCode": "99999"}, session)

    def test_a_rejected_card_is_not_blacklisted_from_another_location(self):
        """Bug réel : une annonce écartée comme remplissage pendant le scrape
        d'une localisation ne doit pas être définitivement interdite de compter
        pour une AUTRE localisation plus loin dans le même scrape. D'où deux
        ensembles distincts : `vues_ici` (par requête, inclut les rejetées) et
        `seen` (partagé, retenues seulement)."""
        paris_15_page = page(
            card("52805433", city="PARIS", zip_code="75015", city_slug="paris-15", agency="paris15lourmel")
        )

        def handler(url, params):
            return FakeResponse(SAMPLE_PAGE_HTML if "75014" in url else paris_15_page)

        session = FakeSession(handler=handler)
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            listings = run_scrape(
                {"locations": [
                    {"city": "Paris", "postalCode": "75014"},
                    {"city": "Paris", "postalCode": "75015"},
                ]},
                session,
            )

        # La carte 75015, écartée en scrapant le 75014 (remplissage), est bien
        # retenue quand c'est le 75015 qui est demandé.
        assert [li.listing_id for li in listings] == ["lf_52805433"]
        assert listings[0].zip_code == "75015"

    def test_the_same_listing_found_twice_is_kept_once(self):
        """`seen` est partagé entre les requêtes d'un même scrape."""
        shared = page(card("52811904", city="PARIS", zip_code="75018"))
        session = FakeSession(handler=lambda url, params: FakeResponse(shared))

        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            listings = run_scrape(
                {"locations": [
                    {"city": "Paris", "postalCode": "75018"},
                    {"city": "Saint-Ouen", "postalCode": "75018"},
                ]},
                session,
            )

        assert [li.listing_id for li in listings] == ["lf_52811904"]

    def test_a_region_without_departments_still_gets_its_own_canonical_request(self):
        """Une région dont les départements sont introuvables n'a aucun filtre,
        mais son nom suffit désormais à l'ancre canonique : elle part en requête
        nue sur SA page (plutôt qu'échouer sans espoir, comme avant)."""
        session = FakeSession([EMPTY_PAGE_HTML])
        with patch("parsers.laforet.region_departments", return_value=[]):
            listings = run_scrape({"locations": [{"kind": "region", "name": "Corse", "code": "94"}]}, session)

        assert listings == []
        assert session.plain_calls[0]["url"] == f"{BASE_URL}/region/location-appartement-corse"
        # Repli SANS filtre de périmètre : il est supposé pouvoir être vide, pas
        # d'alerte « aucune carte parsée » (voir TestLoudEmptyPage).

    def test_a_region_without_any_name_fails_alone(self):
        """Sans départements résolus ET sans nom, rien n'est possible : elle
        échoue, avec un message qui la nomme."""
        session = FakeSession([SAMPLE_PAGE_HTML])
        with patch("parsers.laforet.region_departments", return_value=[]):
            with pytest.raises(ValueError, match=r"région 94: aucune page pour ancrer l'URL"):
                run_scrape({"locations": [{"kind": "region", "name": "", "code": "94"}]}, session)
        assert session.calls == []


class TestScrapePartialFailures:
    def test_the_merged_request_failing_sends_every_perimeter_to_the_plain_path(self):
        """Si la requête combinée échoue (réseau, réponse inattendue), chaque
        périmètre qui en faisait partie doit tout de même avoir sa propre
        tentative plutôt que d'être perdu."""
        def handler(url, params):
            if params is not None:
                raise ConnectionError("requête fusionnée boum")
            return FakeResponse(SAMPLE_PAGE_HTML)

        session = FakeSession(handler=handler)
        listings = run_scrape({"city": "Paris", "postalCode": "75018"}, session)

        assert [li.zip_code for li in listings] == ["75018"]
        assert len(session.merged_calls) == 1
        assert session.plain_calls

    def test_one_bad_location_never_discards_the_others(self):
        """Un 404 sur une ville ne doit pas jeter ce qui a déjà été trouvé pour
        les autres : un succès partiel reste un succès."""
        def handler(url, params):
            if "nawak" in url:
                return FakeResponse("", status_code=404)
            return FakeResponse(SAMPLE_PAGE_HTML)

        session = FakeSession(handler=handler)
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            listings = run_scrape(
                {"locations": [
                    {"city": "Paris", "postalCode": "75018"},
                    {"city": "Nawak", "postalCode": "99999"},
                ]},
                session,
            )

        assert [li.zip_code for li in listings] == ["75018"]

    def test_it_raises_only_when_every_attempt_fails(self):
        session = FakeSession(handler=lambda url, params: FakeResponse("", status_code=404))
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            with pytest.raises(ValueError, match=r"ville/code postal invalide") as excinfo:
                run_scrape(
                    {"locations": [
                        {"city": "Nawak", "postalCode": "99999"},
                        {"city": "Bidule", "postalCode": "88888"},
                    ]},
                    session,
                )

        # Toutes les erreurs sont rapportées, séparées par « ; ».
        message = str(excinfo.value)
        assert "Nawak 99999: ville/code postal invalide" in message
        assert "Bidule 88888: ville/code postal invalide" in message
        assert "; " in message

    def test_the_merged_failure_is_reported_alongside_the_plain_ones(self):
        session = FakeSession(handler=lambda url, params: FakeResponse("", status_code=404))
        with pytest.raises(ValueError, match=r"^requête fusionnée: ") as excinfo:
            run_scrape({"locations": [PARIS_18]}, session)

        message = str(excinfo.value)
        assert message.startswith("requête fusionnée: URL de base invalide")
        assert "Paris 75018: ville/code postal invalide" in message

    def test_a_partial_success_is_still_a_success(self, logged):
        """attempts=2, failures=1 : pas d'exception, et le compte final est
        logué."""
        def handler(url, params):
            if "nawak" in url:
                raise ConnectionError("boum")
            return FakeResponse(SAMPLE_PAGE_HTML)

        session = FakeSession(handler=handler)
        with patch("parsers.laforet._resolve_insee_code", return_value=None):
            listings = run_scrape(
                {"locations": [
                    {"city": "Paris", "postalCode": "75018"},
                    {"city": "Nawak", "postalCode": "99999"},
                ]},
                session,
            )

        assert len(listings) == 1
        assert any(
            level == "INFO" and "Scraping terminé : 1 annonces uniques" in message
            for level, message in logged
        )
        assert any(level == "WARNING" and "Nawak 99999: boum" in message for level, message in logged)


# ---------------------------------------------------------------------------
# scrape : cohérence avec build_search_urls
# ---------------------------------------------------------------------------

class TestScrapeMatchesTheAdvertisedUrls:
    @pytest.mark.parametrize(
        "criteria",
        [
            {"locations": [PARIS_18]},
            {"locations": [PARIS_18, LYON_7], "priceMax": 1200, "rooms": [3]},
            {"locations": [GIRONDE]},
        ],
        ids=["une_ville", "deux_villes_filtrees", "departement"],
    )
    def test_the_first_request_is_exactly_the_advertised_url(self, criteria):
        """« Voir l'URL » ne doit pas mentir : la première requête réellement
        émise doit être l'URL annoncée, paramètres compris."""
        from urllib.parse import urlencode

        expected = LaforetParser().build_search_urls(criteria)[0]
        session = FakeSession([EMPTY_PAGE_HTML])
        run_scrape(criteria, session)

        call = session.calls[0]
        assert f"{call['url']}?{urlencode(call['params'])}" == expected
