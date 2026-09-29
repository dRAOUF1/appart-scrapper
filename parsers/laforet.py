"""Laforet.com listing scraper.

Site server-rendered (Symfony/Turbo + UX Live Component), sans API JSON
publique. Le chemin de l'URL porte le périmètre : pages /ville/ pour les
communes (slug lisible + code postal), pages canoniques pour les niveaux
larges — vérifiées en live le 2026-08-23, elles existent et portent tous
les filtres :

    https://www.laforet.com/ville/location-appartement-paris-75018
    https://www.laforet.com/departement/location-appartement-gironde
    https://www.laforet.com/region/location-appartement-ile-de-france

Cette page rend toujours deux sections : les vrais résultats, correctement
cadrés, puis une section « Appartements à proximité de {ville} » alimentée par
les communes/arrondissements voisins. Les deux utilisent le même balisage de
carte, donc tout ce qui parse la page entière ramasse ce bruit — voir
_extract_genuine_section().

Les résultats eux-mêmes peuvent être découpés en plusieurs blocs, dont un
titré « Autres annonces ». Ce titre ressemble à un début de remplissage mais
n'en est pas un : couper dessus fait perdre la majeure partie des annonces
(voir le commentaire de _NEARBY_SECTION_MARKER, chiffres à l'appui).

Multiple locations in one search are merged server-side via
`filter[cities][]=<INSEE code>` query params (verified live against the
site's own "add a city" filter UI, network-captured with Playwright) — but
only within the *first*, genuine section; the noise section is unaffected
by the filter and must still be excluded the same way. `filter[cities][]`
takes INSEE-style commune codes, not postal codes, and Paris/Lyon/Marseille
need their arrondissement-specific code (not the whole-city INSEE code) —
c'est core.geocode qui s'en charge, partagé avec les autres sources.

Plusieurs types de bien tiennent aussi dans une seule requête :
filter[types][] est répétable et prime sur le type inscrit dans le slug du
chemin (vérifié en live — une page "achat-appartement-bordeaux-33000" avec
filter[types][]=apartment&filter[types][]=house rend bien 17 appartements
et 18 maisons). Le type d'une annonce est donc lu dans son URL, pas déduit
de ce qui a été demandé — voir _property_type_from_url().

Les filtres prix/surface/pièces (filter[min], filter[max], filter[surface],
filter[rooms]) sont eux aussi envoyés, MAIS uniquement en présence d'un filtre
de périmètre : seuls, ils font basculer le site en recherche nationale et le
cadrage du chemin est perdu. Voir _criteria_filters(), qui détaille leurs
sémantiques et le gain mesuré. Le filtrage reste appliqué intégralement côté
client par _passes_filters() : ces filtres dégrossissent sans être exacts.
"""

from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from loguru import logger

from core.criteria import APARTMENT, BUY, HOUSE, RENT, matches_locations
from core.geocode import (
    CITY,
    DEPARTMENT,
    REGION,
    WHOLE_CITY,
    region_departments,
)
from core.geocode import (
    resolve_insee_code as _resolve_insee_code,
)
from models.listing import Listing
from parsers._dates import DATE_INCONNUE
from parsers.base import BaseParser, ParserRegistry, get_locations

BASE_URL = "https://www.laforet.com"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

MAX_PAGES = 30

# Path uses French slugs; the filter[...] query params use English values —
# verified live these are genuinely two different vocabularies on this site.
# Les clés sont le vocabulaire canonique (core.criteria).
TRANSACTION_SLUGS = {RENT: "location", BUY: "achat"}
TRANSACTION_FILTER_VALUES = {RENT: "rent", BUY: "buy"}
TYPE_SLUGS = {APARTMENT: "appartement", HOUSE: "maison"}
TYPE_FILTER_VALUES = {APARTMENT: "apartment", HOUSE: "house"}
# Le libellé affiché dans le titre des annonces trouvées.
TYPE_DISPLAY_NAMES = {APARTMENT: "Appartement", HOUSE: "Maison"}

# Marque où les vrais résultats s'arrêtent et où commence le remplissage que
# Laforet ajoute derrière (voir le docstring du module).
#
# ATTENTION — ne pas ajouter "Autres annonces" ici. La page porte aussi ce
# titre, et il ressemble à s'y tromper à un début de section de remplissage,
# mais c'est un simple sous-titre QUI DÉCOUPE LES RÉSULTATS EUX-MÊMES en
# plusieurs blocs. Couper dessus fait perdre la majorité des annonces —
# mesuré sur 12 recherches réelles en comparant au compteur que le site
# affiche ("N annonces à louer/vendre") :
#
#     recherche             site   coupe "proximité"   coupe "Autres annonces"
#     Lille location          15         15  ✓                  3  ✗
#     Toulouse achat maison   11         11  ✓                  1  ✗
#     Marseille 8e achat      16         16  ✓                  7  ✗
#     Boulogne location       11         11  ✓                  5  ✗
#
# Le compteur du site est l'arbitre : couper sur ce seul marqueur le retrouve
# exactement sur les 9 recherches dont le stock tient dans une page.
_NEARBY_SECTION_MARKER = "proximité de"

# Must match the full listing-detail path shape, not just "ends in -<digits>".
# When a city has thin inventory Laforet backfills the results page with
# "nearby agency office" cards (e.g. an <a href="/agence-immobiliere/lyon-7">
# linking to the office itself, not a listing). "lyon-7" alone also ends in
# "-<digit>", so a looser pattern misidentifies these office cards as real
# listings (verified live: this returned a fake "listing" whose url was just
# the agency's own page, with no price/surface/rooms/location at all).
#
# L'ancrage final sur (\d+)$ impose de nettoyer l'URL avant de la confronter au
# motif : voir _listing_path().
_DETAIL_LINK_RE = re.compile(
    r"/agence-immobiliere/[^/]+/(?:louer|acheter)/[^/]+/(?:appartement|maison)-[^/]+-(\d+)$"
)
_PRICE_RE = re.compile(r"([\d\s ]+)\s*€")
_CITY_ZIP_RE = re.compile(r"([A-ZÀ-Ü][A-Za-zÀ-ÿ' \-]*?)\s*\((\d{5})\)")
_SURFACE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m²")
_ROOMS_RE = re.compile(r"(\d+)\s*pi[eè]ce")

def _slugify(text: str) -> str:
    """Lowercase, strip accents, non-alnum -> '-' (e.g. 'Le Kremlin-Bicêtre' -> 'le-kremlin-bicetre')."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()


def _canonical_slug(name: str) -> str:
    """Le slug tel que le site l'écrit sur ses pages canoniques
    /departement/ et /region/.

    Différence subtile avec _slugify : Laforêt SUPPRIME l'apostrophe au lieu
    de la convertir en tiret — « Provence-Alpes-Côte d'Azur » s'écrit
    provence-alpes-cote-dazur (vérifié en live le 2026-08-23), pas
    provence-alpes-cote-d-azur, dont la page n'existe pas. Les pages /ville/
    des communes, elles, continuent de passer par _slugify : leurs slugs
    fonctionnent tels quels aujourd'hui, inutile de les bousculer.
    """
    return _slugify(name.replace("'", "").replace("\u2019", ""))


def _transaction(criteria: dict) -> str:
    """La transaction canonique demandée. La location par défaut : c'est ce
    que le formulaire propose en premier, et une recherche sans transaction
    explicite n'a jamais voulu dire "achat"."""
    transaction = criteria.get("transaction")
    return transaction if transaction in TRANSACTION_SLUGS else RENT


def _property_types(criteria: dict) -> list[str]:
    """Les types de bien canoniques que Laforet sait traiter parmi ceux
    demandés.

    - Aucun type demandé -> appartement, le défaut du formulaire.
    - Types demandés dont certains sont hors capacités (parking, terrain) ->
      on garde les autres sans lever d'exception : une recherche mixte
      « appartement + parking » doit tout de même ramener les appartements.
    - Uniquement des types hors capacités -> liste vide, et surtout PAS le
      défaut appartement : renvoyer des appartements à qui demande un parking
      serait un faux résultat. Les appelants traitent ce cas comme
      « rien à chercher » (voir build_search_urls et scrape).
    """
    requested = criteria.get("propertyTypes") or []
    if not requested:
        return [APARTMENT]
    return [t for t in requested if t in TYPE_SLUGS]


def _describe(location: dict) -> str:
    """Un périmètre en clair, pour les logs et les messages d'erreur."""
    kind = location.get("kind", CITY)
    if kind == REGION:
        return f"région {location.get('name') or location.get('code')}"
    if kind == DEPARTMENT:
        return f"département {location.get('name') or location.get('code')}"
    if kind == WHOLE_CITY:
        return f"{location.get('city')} (toute la ville)"
    return f"{location.get('city')} {location.get('postalCode')}"


def _city_insee_codes(location: dict) -> list[str]:
    """Les codes INSEE de commune couverts par une localisation, pour
    filter[cities][].

    - city : son propre code INSEE, fourni par l'autocomplete ou résolu depuis
      le code postal via core.geocode (le point de vérité partagé).
    - whole_city : LE code INSEE de la commune, un seul. Laforet comprend
      directement le code de la commune entière (75056 pour Paris) et rend
      exactement le même résultat qu'en énumérant ses 20 arrondissements
      (vérifié : 66 annonces dans les deux cas) — inutile de les développer.
    - region/department : rien ici, ils ont leurs propres filtres.
    """
    kind = location.get("kind", CITY)
    if kind == CITY:
        code = location.get("inseeCode") or _resolve_insee_code(location["postalCode"])
        return [code] if code else []
    if kind == WHOLE_CITY:
        insee = location.get("inseeCode")
        if insee:
            return [insee]
        # Pas de code de commune (localisation enregistrée sans) : se rabattre
        # sur les codes des codes postaux, moins direct mais équivalent.
        codes = []
        for postal_code in location.get("postalCodes") or []:
            code = _resolve_insee_code(postal_code)
            if code and code not in codes:
                codes.append(code)
        return codes
    return []


def _department_codes(location: dict) -> list[str]:
    """Les codes de département couverts par une localisation.

    Une région est traitée comme l'ensemble de ses départements. Laforet a bien
    un filtre région natif (`filter[regions][]=11`) qui donne exactement le même
    résultat — 741 annonces pour l'Île-de-France dans les deux cas — mais les
    départements sont préférés ici : ils rendent le périmètre explicite dans
    l'URL, et ne dépendent pas d'un découpage régional propre au site.

    Attention, c'est bien `filter[departments][]` au pluriel : au singulier,
    `filter[department]` ne filtre rien et renvoie le flux national (2545
    annonces, de l'Ain aux Pyrénées).
    """
    kind = location.get("kind")
    if kind == DEPARTMENT:
        return [location["code"]] if location.get("code") else []
    if kind == REGION:
        codes = list(location.get("departments") or [])
        if not codes and location.get("code"):
            # Région enregistrée sans ses départements (saisie manuelle, ou
            # format antérieur) : les retrouver plutôt que de tout abandonner.
            codes = region_departments(location["code"])
        return codes
    return []


# Départements d'outre-mer : le moteur de recherche de Laforêt n'y référence
# AUCUN bien (vérifié en live le 2026-08-23 : filter[departments][]=974 et
# filter[cities][]=97411 rendent 0 annonce, même avec des filtres prix). Pire,
# la page /ville/ d'un CP outre-mer sans filtre redirige vers la recherche
# nationale. Autant le dire avant le scrape plutôt que laisser un 0 annonce
# silencieux passer pour un succès.
DOM_ROM_DEPARTMENTS = ("971", "972", "973", "974", "976")


def _dom_rom_departments(locations: list[dict]) -> list[str]:
    """Les départements d'outre-mer couverts par ces périmètres.

    Déduits de tous les niveaux : code du département, départements d'une
    région, et — pour une commune ou une ville entière — préfixe des codes
    postaux et INSEE (« 97400 » -> « 974 »). Aucun code métropole ne commence
    par « 97 » (Corse comprise : 20xxx), donc le préfixe suffit.
    """
    found: list[str] = []
    for location in locations:
        candidates = [c for c in _department_codes(location) if c in DOM_ROM_DEPARTMENTS]
        for value in (
            location.get("postalCode"),
            *(location.get("postalCodes") or []),
            location.get("inseeCode"),
        ):
            prefix = str(value)[:3] if value else ""
            if prefix in DOM_ROM_DEPARTMENTS and prefix not in candidates:
                candidates.append(prefix)
        for code in candidates:
            if code not in found:
                found.append(code)
    return found


def _extract_genuine_section(html: str) -> str:
    """Ne garde que la partie de la page qui contient les vrais résultats, en
    retirant la section de remplissage ajoutée derrière (voir
    _NEARBY_SECTION_MARKER, et l'avertissement sur "Autres annonces").

    À appeler avant tout parsing de cartes ou de pagination.
    """
    idx = html.find(_NEARBY_SECTION_MARKER)
    return html[:idx] if idx != -1 else html


def _parse_price(text: str) -> float | None:
    m = _PRICE_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\s", "", m.group(1))
    try:
        return float(digits)
    except ValueError:
        return None


def _listing_path(href: str) -> str:
    """L'URL d'annonce débarrassée de son ancre et de sa query string.

    Laforet lie parfois directement une section de la page de l'annonce, par
    exemple `.../maison-11-pieces-52637604#section-video` quand elle a une
    vidéo. Comme _DETAIL_LINK_RE est ancré sur la fin (`(\\d+)$`), ces liens ne
    correspondaient à rien et l'annonce était purement ignorée — constaté en
    live sur Rennes (6 annonces récupérées pour 7 annoncées par le site).
    """
    return href.split("#", 1)[0].split("?", 1)[0].rstrip("/")


def _card_photos(article) -> list[str]:
    """Les URLs des photos d'une carte, absolues et dans l'ordre d'affichage.

    Les `src` sont relatifs et leur query string porte une SIGNATURE
    (`...jpg?w=400&h=250&...&s=6380268...`) : il faut la conserver
    intégralement, sinon le serveur d'images répond 403. C'est aussi pourquoi
    on ne réutilise pas _listing_path() ici, qui strippe la query.

    Les cartes portent plusieurs photos (a, b, c...), les suivantes masquées
    pour un défilement côté client — elles sont toutes gardées, comme le fait
    déjà SeLoger.
    """
    photos = []
    for img in article.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        marker = " ".join([
            src,
            img.get("alt") or "",
            " ".join(img.get("class") or []),
        ]).casefold()
        if "logo" in marker or "/agence" in src.casefold():
            continue
        url = f"{BASE_URL}{src}" if src.startswith("/") else src
        if url not in photos:
            photos.append(url)
    return photos


def _parse_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []
    for article in soup.find_all("article"):
        link = None
        for a in article.find_all("a", href=True):
            if "/agence-immobiliere/" in a["href"]:
                link = _listing_path(a["href"])
                break
        if not link:
            continue
        m = _DETAIL_LINK_RE.search(link)
        if not m:
            continue
        reference = m.group(1)

        text = article.get_text(" ", strip=True)
        price_value = _parse_price(text)
        city_zip = _CITY_ZIP_RE.search(text)
        surface_m = _SURFACE_RE.search(text)
        rooms_m = _ROOMS_RE.search(text)

        results.append({
            "reference": reference,
            "url": link,
            "price_value": price_value,
            "city": city_zip.group(1).strip() if city_zip else "",
            "zip_code": city_zip.group(2) if city_zip else "",
            "surface": surface_m.group(1) if surface_m else "",
            "rooms": rooms_m.group(1) if rooms_m else "",
            "agency": link.split("/agence-immobiliere/", 1)[1].split("/", 1)[0],
            "photos": _card_photos(article),
        })
    return results


def _passes_filters(listing: Listing, criteria: dict, locations: list[dict]) -> bool:
    """Applique nous-mêmes les filtres localisation + prix/surface/pièces.

    `locations` sont les périmètres canoniques que cette requête couvre, à
    quelque niveau que ce soit (un code postal, une ville entière, un
    département, une région) — c'est core.criteria.matches_locations qui
    tranche, pour que la règle soit la même partout.

    Une annonce dont on n'a pas pu lire le code postal, ou qui tombe hors des
    périmètres, n'est jamais supposée correspondre (on échoue fermé, pas
    ouvert : contrairement au prix ou à la surface plus bas, la localisation ne
    se laisse pas passer sous prétexte qu'une carte était difficile à lire —
    c'est exactement comme ça qu'une carte de remplissage sans code postal
    était autrefois remontée comme une fausse annonce).
    """
    if not matches_locations(listing.zip_code, locations):
        return False

    price_min = criteria.get("priceMin")
    price_max = criteria.get("priceMax")
    if listing.price_value is not None:
        if price_min and listing.price_value < price_min:
            return False
        if price_max and listing.price_value > price_max:
            return False

    surface_min = criteria.get("surfaceMin")
    surface_max = criteria.get("surfaceMax")
    if listing.surface:
        try:
            surface = float(listing.surface.replace(",", "."))
        except ValueError:
            surface = None
        if surface is not None:
            if surface_min and surface < surface_min:
                return False
            if surface_max and surface > surface_max:
                return False

    rooms_filter = criteria.get("rooms")
    if rooms_filter and listing.rooms:
        try:
            room_count = int(listing.rooms)
            allowed = {int(r) for r in rooms_filter}
        except (ValueError, TypeError):
            return True
        # "5" in the shared rooms vocabulary means "5+" (see search_edit.html).
        if not any(room_count == r or (r >= 5 and room_count >= 5) for r in allowed):
            return False

    return True


def _property_type_from_url(url: str) -> str:
    """Le type de bien lu dans l'URL de l'annonce elle-même.

    Une même requête peut mélanger les types (filter[types][] est répétable
    et prime sur le slug du chemin — vérifié en live : une page
    "achat-appartement-..." avec filter[types][]=apartment&...=house rend
    bien 17 appartements et 18 maisons). Le type ne peut donc pas être
    déduit de ce qui a été demandé, il doit être lu par annonce.
    """
    for canonical, slug in TYPE_SLUGS.items():
        if f"/{slug}-" in url:
            return TYPE_DISPLAY_NAMES[canonical]
    return ""


def _dict_to_listing(data: dict) -> Listing:
    price_value = data["price_value"]
    property_type = _property_type_from_url(data["url"])
    photos = data.get("photos") or []
    return Listing(
        listing_id=f"lf_{data['reference']}",
        url=data["url"],
        title=f"{property_type} {data['city']}".strip(),
        price=f"{int(price_value)} €" if price_value is not None else "",
        surface=data["surface"],
        rooms=data["rooms"],
        location=data["city"],
        city=data["city"],
        zip_code=data["zip_code"],
        agency=data["agency"],
        source="laforet",
        legacy_id=data["reference"],
        price_value=price_value,
        property_type=property_type,
        # Même forme que SeLoger : la première photo sert de vignette, la liste
        # complète est stockée en JSON (voir parsers/seloger.py).
        image_url=photos[0] if photos else "",
        photos=json.dumps([{"url": url, "alt": "", "key": ""} for url in photos]),
        # Issue #12 : les cartes d'annonces Laforêt ne portent aucune date de
        # publication. Les balises <time datetime> des pages listes sont des
        # dates d'ARTICLES DE BLOG (« Punaises de lit… »), pas des annonces —
        # ne jamais s'en servir. Sentinelle explicite plutôt que chaîne vide.
        creation_date=DATE_INCONNUE,
    )


@ParserRegistry.register
class LaforetParser(BaseParser):
    """Scrape Laforet.com via server-rendered HTML (no anti-bot on this site)."""

    SOURCE_ID = "laforet"
    SOURCE_NAME = "Laforêt"
    SOURCE_DESCRIPTION = "Laforet.com — scraping HTML serveur (ville + code postal)"

    # Laforet ne référence que de l'habitation : ni parking, ni terrain
    # (son URL n'a pas de slug pour eux et filter[types][] ne les connaît
    # pas). Déclaré ici pour que ce soit dit à l'utilisateur avant le
    # scrape, plutôt que levé en pleine exécution.
    SUPPORTED_PROPERTY_TYPES = (APARTMENT, HOUSE)

    URL_NOTE = (
        "Laforêt n'a pas de filtre de surface maximale, et son filtre de pièces "
        "est un minimum : ce lien peut donc montrer un peu plus large que la "
        "recherche. Le scraper applique les critères exacts de son côté. "
        "Laforêt ne scopre qu'à la commune entière : un code postal ne peut pas "
        "être isolé de ses voisins dans la même commune, le scraper re-filtre "
        "donc côté serveur — sur une très grosse commune, quelques annonces au-"
        "delà des 30 premières pages peuvent manquer. Sur la carte du site, "
        "déplacer ou zoomer peut faire perdre le périmètre (bug connu chez "
        "l'éditeur) : fiez-vous aux filtres listés dans l'URL, pas à la carte. "
        "Enfin, les départements d'outre-mer (971, 972, 973, 974 et 976) ne "
        "sont pas couverts par le moteur de recherche du site."
    )

    # has_valid_criteria / cannot_search_reason : pas de surcharge nécessaire,
    # ville + code postal (le contrat par défaut de BaseParser) suffisent —
    # le code INSEE dont filter[cities][] a besoin est résolu à partir du
    # code postal quand l'autocomplete ne l'a pas déjà fourni.

    def to_native(self, criteria: dict) -> dict:
        """Rien à traduire : Laforet construit ses URLs directement depuis le
        canonique (voir _search_paths). Les bornes prix/surface/pièces sont
        appliquées côté scraper par _passes_filters, qui lit lui aussi le
        canonique."""
        return criteria

    def _path_anchor(self, location: dict) -> dict | None:
        """Le tronçon de chemin identifiant le périmètre dans l'URL : un
        niveau (« ville », « departement », « region ») et le slug qui le suit
        après la transaction et le type de bien.

        Les communes passent par les pages /ville/ (slug + code postal) :
            /ville/location-appartement-paris-75018
        Un département ou une région passe par sa page canonique — vérifiées
        en live le 2026-08-23, elles existent pour tous les départements et
        portent tous les filtres :
            /departement/location-appartement-gironde
            /region/location-appartement-ile-de-france
        L'ancienne ancre « ville principale du département + premier CP » est
        abandonnée : sur 37 départements sur 101 cette page /ville/ n'existait
        pas et le site redirigeait (301) — pire, saint-denis-97400 rebasculait
        vers la recherche NATIONALE, et le repli sans filtres balayait alors
        tout le stock français pour n'en retenir rien.

        Renvoie None quand rien ne permet d'ancrer l'URL : commune sans code
        postal exploitable, ou département/région sans nom — le code seul
        (« /departement/location-appartement-33 ») ne correspond à aucune page
        réelle du site.
        """
        kind = location.get("kind", CITY)
        if kind == CITY:
            return {
                "level": "ville",
                "slug": f"{_slugify(location['city'])}-{location['postalCode']}",
            }
        if kind == WHOLE_CITY:
            postal_codes = location.get("postalCodes") or []
            if not postal_codes:
                return None
            return {
                "level": "ville",
                "slug": f"{_slugify(location['city'])}-{sorted(postal_codes)[0]}",
            }
        if kind in (DEPARTMENT, REGION):
            name = (location.get("name") or "").strip()
            if not name:
                logger.warning(
                    f"[Laforet] {_describe(location)} : pas de nom pour "
                    "construire l'URL canonique, requête impossible"
                )
                return None
            level = "departement" if kind == DEPARTMENT else "region"
            return {"level": level, "slug": _canonical_slug(name)}
        # Kind inconnu : pas d'ancre devinée.
        return None

    def _base_path(self, criteria: dict, location: dict) -> str | None:
        """Le chemin de la page de résultats pour cette localisation, ou None
        si on n'a pas de page pour l'ancrer.

        Le type dans le slug est celui du premier type demandé, mais il n'a
        pas d'effet réel : filter[types][] prime sur lui (vérifié en live).
        """
        anchor = self._path_anchor(location)
        if not anchor:
            return None
        transaction = TRANSACTION_SLUGS[_transaction(criteria)]
        type_slug = TYPE_SLUGS[_property_types(criteria)[0]]
        return (
            f"{BASE_URL}/{anchor['level']}/{transaction}-{type_slug}-{anchor['slug']}"
        )

    def _type_filters(self, criteria: dict) -> list[tuple[str, str]]:
        """Un filter[types][] par type de bien demandé — le paramètre est
        répétable et c'est lui qui gouverne réellement le résultat."""
        return [
            ("filter[types][]", TYPE_FILTER_VALUES[t])
            for t in _property_types(criteria)
        ]

    def _criteria_filters(self, criteria: dict) -> list[tuple[str, str]]:
        """Les filtres prix / surface / pièces à envoyer au site.

        À N'ENVOYER QUE conjointement à un filtre de périmètre
        (filter[cities][] ou filter[departments][]). Seuls, ils font basculer
        le site en recherche nationale et le cadrage du chemin est perdu :
        vérifié en live, une page /paris-75014 avec un filtre prix rend 39
        annonces dont 39 hors du 75014 (Bordeaux, Lyon, Chambéry...). C'est le
        piège qui avait fait retirer ces filtres à l'époque où
        filter[cities][] n'existait pas encore dans ce code.

        Avec un périmètre, en revanche, le cadrage tient parfaitement et le
        gain est net : sur la Gironde, la première page passe de 18 à 39
        annonces effectivement dans le budget demandé, ce qui compte d'autant
        plus qu'on ne lit que cette première page (les annonces les plus
        récentes).

        Sémantiques vérifiées côté site :
          filter[min]/filter[max]  bornes de prix
          filter[surface]          surface MINIMUM (il n'existe aucun filtre
                                   de surface maximum)
          filter[rooms]            nombre de pièces MINIMUM, pas une égalité
                                   (rooms=3 rend du 3, 4, 5 et 6 pièces)

        Le filtrage reste appliqué intégralement côté client par
        _passes_filters : ces filtres dégrossissent, ils ne sont pas exacts
        (une annonce à 424 000 € passe avec filter[max]=400000) et ne couvrent
        ni la surface maximale ni une sélection précise de nombres de pièces.
        """
        filters: list[tuple[str, str]] = []

        for param, key in (("filter[min]", "priceMin"), ("filter[max]", "priceMax")):
            value = criteria.get(key)
            if value:
                filters.append((param, str(value)))

        surface_min = criteria.get("surfaceMin")
        if surface_min:
            filters.append(("filter[surface]", str(surface_min)))

        rooms = criteria.get("rooms") or []
        # Le paramètre étant un minimum, seul le plus petit nombre de pièces
        # demandé peut être transmis sans risquer d'exclure une annonce voulue.
        # Un minimum de 1 n'écarte rien : autant ne pas l'envoyer.
        if rooms and min(rooms) > 1:
            filters.append(("filter[rooms]", str(min(rooms))))

        return filters

    def _location_filters(self, location: dict) -> list[tuple[str, str]]:
        """Les paramètres de filtre couvrant le périmètre d'une localisation.

        Chaque niveau passe par le filtre que Laforet lui destine :

            region      filter[departments][] pour chacun de ses départements
            department  filter[departments][]=33    (toute la Gironde)
            whole_city  filter[cities][]=75056      (tout Paris, un seul code)
            city        filter[cities][]=75115      (Paris 15e)

        Les filtres se cumulent en UNION : vérifié en live que cities=33063
        (149 annonces) et departments=75 (824) donnent 973 ensemble. Une même
        recherche peut donc couvrir plusieurs périmètres, de niveaux
        différents, dans une seule requête.
        """
        city_codes = _city_insee_codes(location)
        if city_codes:
            return [("filter[cities][]", code) for code in city_codes]

        return [("filter[departments][]", code) for code in _department_codes(location)]

    def _split_locations(self, criteria: dict) -> tuple[list[dict], list[tuple[str, str]], list[dict]]:
        """Répartit les localisations entre celles qu'on sait filtrer et les
        autres.

        Renvoie (localisations filtrables, leurs filtres cumulés, localisations
        sans filtre). Ces dernières — une commune dont le code INSEE n'a pas pu
        être résolu — prennent leur propre requête sur la page ville nue plutôt
        que d'être silencieusement abandonnées.
        """
        filterable: list[dict] = []
        filters: list[tuple[str, str]] = []
        plain: list[dict] = []

        for location in get_locations(criteria):
            location_filters = self._location_filters(location)
            if location_filters:
                filterable.append(location)
                filters.extend(location_filters)
            else:
                logger.warning(
                    f"[Laforet] Périmètre non filtrable ({_describe(location)}), "
                    "requête séparée sur la page ville"
                )
                plain.append(location)
        return filterable, filters, plain

    def build_search_url(self, criteria: dict) -> str | None:
        """First location's URL — see build_search_urls() for all of them."""
        urls = self.build_search_urls(criteria)
        return urls[0] if urls else None

    def build_search_urls(self, criteria: dict) -> list[str]:
        """The URL(s) actually used to scrape — mirrors scrape()'s own
        strategy exactly, so "Voir l'URL" shows the truth: every location
        that resolves to an INSEE code is combined into one merged URL
        (filter[cities][]), not shown as separate links per city/postal
        code. Only a location that can't be resolved gets its own plain
        URL, matching the real per-location fallback.
        """
        if not get_locations(criteria):
            return []
        if not _property_types(criteria):
            # Seuls des types que Laforet ne référence pas (parking, terrain) :
            # aucune URL plutôt qu'un lien qui montrerait autre chose que ce qui
            # a été demandé.
            return []

        filterable, filters, plain = self._split_locations(criteria)

        urls = []
        if filterable:
            base_path = self._base_path(criteria, filterable[0])
            if base_path:
                query = self._type_filters(criteria) + filters + self._criteria_filters(criteria)
                urls.append(f"{base_path}?{urlencode(query)}")
        for location in plain:
            base_path = self._base_path(criteria, location)
            if base_path:
                urls.append(base_path)
        return urls

    def scrape(self, criteria: dict) -> list[Listing]:
        locations = get_locations(criteria)
        if not locations:
            raise ValueError("Laforet nécessite au moins une localisation (ville + code postal) dans les critères")
        if not _property_types(criteria):
            raise ValueError(
                "Laforet ne référence aucun des types de bien demandés "
                f"(uniquement {sorted(TYPE_SLUGS)})"
            )

        session = requests.Session()
        session.headers.update({
            "User-Agent": DESKTOP_UA,
            "Accept": "text/html, application/xhtml+xml",
        })

        # Tous les périmètres filtrables partent dans une seule requête +
        # pagination : filter[cities][] et filter[departments][] se combinent
        # en union (vérifié en live), donc une même recherche peut couvrir des
        # communes, des départements et des régions d'un coup. Un périmètre non
        # filtrable (commune dont le code INSEE n'a pas pu être résolu) prend sa
        # propre requête au lieu d'être silencieusement abandonné.
        filterable, filters, plain = self._split_locations(criteria)

        # Outre-mer : le moteur Laforêt n'y référence rien (voir
        # DOM_ROM_DEPARTMENTS) — mieux vaut l'annoncer que laisser un 0 annonce
        # silencieux passer pour un succès.
        outre_mer = _dom_rom_departments(filterable + plain)
        if outre_mer:
            logger.warning(
                f"[Laforet] Départements d'outre-mer couverts ({', '.join(outre_mer)}) : "
                "le moteur de recherche de Laforêt n'y référence aucun bien, aucun "
                "résultat ne pourra en venir (limitation du site, pas un échec du "
                "scraper)."
            )

        seen: set[str] = set()
        listings: list[Listing] = []
        errors: list[str] = []
        attempts = 0
        failures = 0

        if filterable:
            attempts += 1
            try:
                listings.extend(
                    self._scrape_merged(session, criteria, filterable, filters, seen)
                )
            except Exception as e:
                failures += 1
                logger.warning(f"[Laforet] Requête fusionnée ({len(filterable)} périmètres) échouée: {e}")
                errors.append(f"requête fusionnée: {e}")
                plain = plain + filterable

        for location in plain:
            attempts += 1
            try:
                listings.extend(
                    self._scrape_location(session, criteria, location, seen)
                )
            except Exception as e:
                failures += 1
                logger.warning(f"[Laforet] {_describe(location)}: {e}")
                errors.append(f"{_describe(location)}: {e}")

        if attempts and failures == attempts:
            raise ValueError("; ".join(errors))

        logger.info(f"[Laforet] Scraping terminé : {len(listings)} annonces uniques")
        return listings

    def _scrape_merged(self, session, criteria: dict, locations: list[dict],
                       filters: list[tuple[str, str]], seen: set) -> list[Listing]:
        """Une requête (+ sa pagination) couvrant tous les périmètres
        filtrables d'un coup, via filter[cities][] et filter[departments][]
        cumulés — le site les combine en union."""
        base_path = self._base_path(criteria, locations[0])
        if not base_path:
            raise ValueError("aucune page pour ancrer l'URL de recherche")

        base_query = self._type_filters(criteria) + filters + self._criteria_filters(criteria)

        def fetch(page: int) -> str:
            query = list(base_query)
            if page > 1:
                query.append(("page", page))
            resp = session.get(base_path, params=query, timeout=15)
            if resp.status_code == 404:
                raise ValueError(f"URL de base invalide ({base_path})")
            resp.raise_for_status()
            return resp.text

        # `filters` ne contient QUE les filtres de périmètre (filter[cities][]/
        # filter[departments][]) : leur présence seule déclenche l'alerte
        # « 200 mais rien de parsé » — un repli sans périmètre est supposé
        # pouvoir rester vide (seuls, les critères prix basculeraient en
        # recherche nationale, voir le docstring du module).
        return self._collect_pages(fetch, criteria, locations, seen, "requête fusionnée",
                                   scope_filtered=bool(filters))

    def _collect_pages(self, fetch, criteria: dict, locations: list[dict],
                       seen: set, label: str, scope_filtered: bool = False) -> list[Listing]:
        """Parcourt les pages de résultats jusqu'à épuisement, en collectant les
        annonces qui passent les filtres.

        La condition d'arrêt est « cette page n'apporte plus aucune annonce
        inédite », et non le nombre de pages annoncé par le site : ce nombre se
        lisait dans un bloc JSON-LD ItemList qui DISPARAÎT dès qu'un filtre est
        envoyé. La boucle s'arrêtait donc toujours après la première page —
        constaté sur une recherche Île-de-France à 850-870 € et 25-30 m² :
        2 annonces retenues au lieu de 4, les deux autres (Vitry-sur-Seine et
        Suresnes) attendant en page 2 sur les 97 résultats annoncés par le site.

        Compter les annonces inédites plutôt que les pages est aussi ce qui
        absorbe le chevauchement entre pages consécutives (page 2 rend 40
        cartes dont seulement 20 nouvelles).

        MAX_PAGES borne le parcours : une recherche large sans filtre serré
        pourrait sinon enchaîner les requêtes très longtemps.

        `scope_filtered` signale que la requête porte des filtres de périmètre :
        si ALORS la page rendue n'a AUCUNE carte parsée (aucune depuis le début
        de la requête), c'est suspect — page « 0 résultat » légitime d'un petit
        périmètre, ou HTML inattendu après une redirection / un changement du
        site : impossible de trancher sans voir la page. On alerte bruyamment
        plutôt que de présenter un succès vide comme une vérité ; à l'inverse,
        une page vide APRÈS en avoir rendu est une fin légitime de pagination
        et reste silencieuse.
        """
        listings: list[Listing] = []
        pages_lues = 0
        # Les références rencontrées dans CETTE requête, y compris celles que
        # les filtres écartent : c'est ce qui détecte l'épuisement des pages.
        # Distinct de `seen`, qui ne retient que les annonces effectivement
        # gardées et est partagé entre les requêtes d'un même scrape — une carte
        # écartée pour un périmètre doit pouvoir être retenue pour un autre.
        vues_ici: set[str] = set()

        for page in range(1, MAX_PAGES + 1):
            cards = _parse_cards(_extract_genuine_section(fetch(page)))
            pages_lues = page

            nouvelles = 0
            for card in cards:
                reference = card["reference"]
                if reference in vues_ici:
                    continue
                vues_ici.add(reference)
                nouvelles += 1

                if reference in seen:
                    continue
                listing = _dict_to_listing(card)
                if _passes_filters(listing, criteria, locations):
                    seen.add(reference)
                    listings.append(listing)

            if not nouvelles:
                # Suspect UNIQUEMENT si rien n'a été parsé depuis le début de
                # la requête : une page vide après du contenu, c'est une fin
                # légitime de pagination — le balisage, lui, est bien compris.
                if not cards and not vues_ici and scope_filtered:
                    logger.warning(
                        f"[Laforet] {label} : page {page} en HTTP 200 mais aucune "
                        "carte parsée — soit ce périmètre n'a vraiment aucun bien, "
                        "soit la page n'est pas celle attendue (redirection, "
                        "changement du site). À vérifier avant de conclure à un "
                        "périmètre vide."
                    )
                break
        else:
            logger.warning(
                f"[Laforet] {label} : limite de {MAX_PAGES} pages atteinte "
                f"({len(listings)} annonces retenues, résultat possiblement partiel)"
            )

        logger.debug(f"[Laforet] {label} : {pages_lues} page(s) lue(s), {len(listings)} annonces retenues")
        return listings

    def _scrape_location(self, session, criteria: dict, location: dict, seen: set) -> list[Listing]:
        """Fallback path for a single location whose postal code couldn't
        be resolved to an INSEE code (or when the merged request failed)."""
        base_search_url = self._base_path(criteria, location)
        if not base_search_url:
            raise ValueError(f"aucune page pour ancrer l'URL ({_describe(location)})")

        def fetch(page: int) -> str:
            sep = "&" if "?" in base_search_url else "?"
            page_url = base_search_url if page == 1 else f"{base_search_url}{sep}page={page}"
            resp = session.get(page_url, timeout=15)
            if resp.status_code == 404:
                raise ValueError(f"ville/code postal invalide ({page_url})")
            resp.raise_for_status()
            return resp.text

        return self._collect_pages(fetch, criteria, [location], seen, _describe(location))
