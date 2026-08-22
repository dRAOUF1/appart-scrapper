"""Tests unitaires de `scraper/seloger.py`.

C'est le composant le plus fragile du dépôt : il lit les annonces dans un blob
JSON embarqué dans le HTML (`window["__UFRN_FETCHER__"]`), après un
double-décodage UTF-8 non trivial, et se bat contre DataDome via une rotation de
proxies publics. Avant cette suite, `get_detailed_listings` — 143 lignes, le
cœur du scraper — n'avait AUCUN test.

Les fixtures HTML de ce fichier sont volontairement réalistes (voir
`CLASSIFIED_ITEM` et `ufrn_page`) : c'est ce qui leur donne leur valeur. Un
changement du pipeline de décodage, du format du blob ou des chemins
d'extraction champ par champ doit faire rougir un test, pas passer inaperçu
jusqu'à ce que la production remonte 0 annonce.

Ce qui n'est PAS testé ici, volontairement :
  * `_PROXY_SOURCES` — c'est une liste d'URLs, pas du comportement.
  * le contenu exact des en-têtes de la session directe (User-Agent iPhone) :
    vérifié une fois, pas pour chaque champ.
"""

from __future__ import annotations

import json
import random
import re
from concurrent.futures import TimeoutError as FuturesTimeoutError
from unittest.mock import MagicMock, patch

import lzstring
import pytest
import requests

from scraper import seloger
from scraper.seloger import (
    SEARCH_URL,
    _fetch_proxy_source,
    _get_free_proxies,
    _split_csv_values,
    _try_with_proxies,
    build_search_url,
    get_detailed_listings,
    parse_search_url,
    scrape,
)

HOME_URL = "https://www.seloger.com/"

# ---------------------------------------------------------------------------
# Fixtures de données : le blob `__UFRN_FETCHER__` tel qu'il arrive vraiment
#
# La page de résultats contient, dans une balise <script> :
#
#     window["__UFRN_FETCHER__"] = JSON.parse("{\"data\":{...}}")
#
# soit une chaîne JSON *échappée pour JavaScript* (les guillemets internes sont
# précédés d'un antislash, les accents restent en UTF-8 littéral). L'enveloppe
# porte `data["classified-serp-init-data"]`, qui contient — au choix, les deux
# formats ayant existé — un dict direct ou une chaîne compressée en LZ-string
# base64. Sous `pageProps`, deux structures parallèles :
#
#     classifieds      : la LISTE ORDONNÉE des ids (l'ordre d'affichage)
#     classifiedsData  : le dict id -> annonce complète
#
# Les accents sont indispensables dans ces fixtures : le décodage de production
# fait `encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")`,
# une chaîne d'opérations qui ne se casse QUE sur du non-ASCII.
# ---------------------------------------------------------------------------

CLASSIFIED_ITEM = {
    "url": "https://www.seloger.com/annonces/locations/appartement/paris-13eme/gobelins/213456789.htm",
    "hardFacts": {
        "title": "Appartement 3 pièces 65 m²",
        "price": {"formatted": "1 250 €/mois", "additionalInformation": "Charges comprises"},
        "keyfacts": ["3 pièces", "65 m²", "2 chambres"],
    },
    "location": {"address": {"city": "Paris", "district": "Paris 13ème", "zipCode": "75013"}},
    "metadata": {
        "legacyId": "213456789",
        "creationDate": "2026-07-20T08:30:00Z",
        "updateDate": "2026-07-24T11:05:00Z",
    },
    "provider": {"isPrivateOwner": False, "phoneNumbers": ["+33145678901"]},
    "cardProvider": {"title": "Agence Beauséjour"},
    "mainDescription": {
        "headline": "Charmant 3 pièces rénové",
        "description": "Très lumineux, exposé sud, proche métro Place d'Italie.",
    },
    "tags": {"isNew": True, "isExclusive": False, "has3DVisit": True},
    "rawData": {
        "surface": {"main": 65.0, "unit": "m²"},
        "price": 1250.0,
        "nbroom": 3,
        "propertyTypeLabel": "Appartement",
    },
    "gallery": {
        "images": [
            {"url": "https://v.seloger.com/s/crop/590x330/1.jpg", "alt": "Séjour", "key": "img-1"},
            {"url": "https://v.seloger.com/s/crop/590x330/2.jpg", "alt": "Cuisine", "key": "img-2"},
        ]
    },
    "energyClass": "C",
    "gesClass": "B",
}

LISTING_ID = "213456789"


def serp_payload(items: dict | None = None, ids: list[str] | None = None) -> dict:
    """`pageProps` tel que la page l'embarque, à partir d'un dict id -> annonce."""
    items = {LISTING_ID: CLASSIFIED_ITEM} if items is None else items
    return {
        "pageProps": {
            "classifieds": list(items) if ids is None else ids,
            "classifiedsData": items,
            # Champs réellement présents et volontairement ignorés par
            # get_detailed_listings : ils ne doivent pas la déranger.
            "totalCount": len(items),
            "seoData": {"title": "Locations d'appartements à Paris 13ème"},
        }
    }


def js_escape(text: str) -> str:
    """Échappe `text` comme le fait le `JSON.parse("...")` de la page.

    `json.dumps(..., ensure_ascii=False)` produit exactement la forme attendue :
    guillemets et antislashes échappés, accents laissés en UTF-8 littéral —
    c'est cette combinaison précise que le double-décodage de production
    dénoue.
    """
    return json.dumps(text, ensure_ascii=False)[1:-1]


def js_escape_ascii(text: str) -> str:
    """Variante entièrement ASCII : les accents deviennent des `\\uXXXX`.

    C'est ce que produirait un `JSON.stringify` par défaut côté serveur SeLoger
    — la forme la plus banale du monde, et celle que le double-décodage de
    production ne sait PAS lire (voir
    test_a_unicode_escape_in_the_blob_makes_the_decoding_explode).
    """
    return json.dumps(text, ensure_ascii=True)[1:-1]


def ufrn_page(init_data, *, extra_html: str = "") -> str:
    """Une page de résultats complète autour de `init_data`.

    `init_data` est placé sous `data["classified-serp-init-data"]` : passer un
    dict simule le format actuel, passer une chaîne base64 le format LZ-string
    historique.
    """
    envelope = json.dumps({"data": {"classified-serp-init-data": init_data}}, ensure_ascii=False)
    return (
        "<!DOCTYPE html><html lang=\"fr\"><head><title>Locations</title></head><body>"
        "<div id=\"__next\">…</div>"
        f'<script>window["__UFRN_FETCHER__"] = JSON.parse("{js_escape(envelope)}")</script>'
        f"{extra_html}</body></html>"
    )


def lz_blob(payload: dict) -> str:
    """Le même payload, compressé comme l'ancien format le faisait."""
    return lzstring.LZString().compressToBase64(json.dumps(payload, ensure_ascii=False))


HAPPY_PAGE = ufrn_page(serp_payload())
BLOCKED_PAGE = "<html><body>Vous avez été bloqué. DataDome</body></html>"


@pytest.fixture
def serve(requests_mock):
    """Sert la home puis la page de recherche. Renvoie le matcher de recherche."""

    def install(*, page: str = HAPPY_PAGE, status: int = 200, home_status: int = 200):
        requests_mock.get(HOME_URL, text="<html>accueil</html>", status_code=home_status)
        return requests_mock.get(SEARCH_URL, text=page, status_code=status)

    return install


@pytest.fixture
def deterministic_backoff(monkeypatch):
    """`random.uniform(1, 3)` figé à 2.0 : le backoff devient assertable."""
    monkeypatch.setattr(random, "uniform", lambda a, b: 2.0)


# ---------------------------------------------------------------------------
# build_search_url
# ---------------------------------------------------------------------------

class TestBuildSearchUrl:
    """Chaque critère est conditionnel et porte un nom SeLoger différent du
    nom canonique : une faute de frappe ici produit une recherche silencieusement
    plus large, pas une erreur."""

    @pytest.mark.parametrize(
        ("criteria", "expected_query"),
        [
            ({}, ""),
            ({"distributionTypes": ["Rent"]}, "distributionTypes=Rent"),
            ({"estateTypes": ["Apartment", "House"]}, "estateTypes=Apartment&estateTypes=House"),
            ({"priceMin": 600}, "priceMin=600"),
            ({"priceMax": 1500}, "priceMax=1500"),
            ({"spaceMin": 20}, "spaceMin=20"),
            ({"spaceMax": 120}, "spaceMax=120"),
            ({"rooms": ["2", "3"]}, "rooms=2&rooms=3"),
            ({"bedrooms": ["1"]}, "bedrooms=1"),
            ({"locationsInBuildingExcluded": ["Basement"]}, "locationsInBuildingExcluded=Basement"),
        ],
        ids=[
            "empty", "distributionTypes", "estateTypes", "priceMin", "priceMax",
            "spaceMin", "spaceMax", "rooms", "bedrooms", "locationsInBuildingExcluded",
        ],
    )
    def test_each_criterion_maps_to_its_own_query_parameter(self, criteria, expected_query):
        assert build_search_url(criteria) == f"{SEARCH_URL}?{expected_query}"

    def test_place_ids_are_sent_under_the_locations_parameter(self):
        """Le nom du paramètre côté SeLoger est `locations`, pas `placeIds` :
        c'est la seule clé dont le nom change à la traduction."""
        url = build_search_url({"placeIds": ["AD08FR31096", "AD08FR36603"]})

        assert url == f"{SEARCH_URL}?locations=AD08FR31096%2CAD08FR36603"
        assert "placeIds" not in url

    def test_place_ids_are_comma_joined_in_a_single_occurrence(self):
        """Vérifié en direct contre seloger.com : avec des occurrences
        répétées (`locations=A&locations=B`), le site ignore tout sauf la
        première et renvoie 30/30 résultats d'une seule ville. Avec un unique
        paramètre virgule (`locations=A,B`), les résultats couvrent bien
        toutes les villes demandées. `doseq=True` sur une liste produirait la
        forme répétée cassée — d'où le join manuel."""
        url = build_search_url({"placeIds": ["AD08FR31096", "AD08FR36603", "AD08FR36621"]})

        assert url.count("locations=") == 1
        assert url == f"{SEARCH_URL}?locations=AD08FR31096%2CAD08FR36603%2CAD08FR36621"

    def test_order_is_added_only_when_provided(self):
        assert "order=" not in build_search_url({"priceMax": 900})
        assert build_search_url({"priceMax": 900}, order="DateDesc").endswith("order=DateDesc")

    def test_parameter_order_is_stable(self):
        """L'URL est loggée et comparée à l'œil en diagnostic : un ordre
        instable rendrait deux scrapes identiques illisibles."""
        criteria = {
            "distributionTypes": ["Rent"], "estateTypes": ["Apartment"],
            "placeIds": ["AD08FR31096"], "priceMin": 600, "priceMax": 1500,
            "spaceMin": 20, "spaceMax": 120, "rooms": ["3"], "bedrooms": ["2"],
        }

        url = build_search_url(criteria, order="DateDesc")

        assert url == (
            f"{SEARCH_URL}?distributionTypes=Rent&estateTypes=Apartment"
            "&locations=AD08FR31096&priceMin=600&priceMax=1500&spaceMin=20"
            "&spaceMax=120&rooms=3&bedrooms=2&order=DateDesc"
        )

    @pytest.mark.parametrize("key", ["priceMin", "spaceMin", "priceMax", "spaceMax", "rooms", "bedrooms"])
    def test_zero_valued_criteria_are_dropped(self, key):
        """# BUG : `if criteria.get("priceMin")` teste la *vérité*, pas la
        présence (scraper/seloger.py:186-197).

        Conséquence : `priceMin=0` et `spaceMin=0` — des bornes basses
        parfaitement légitimes, et ce que produit un formulaire dont le champ
        « prix minimum » est rempli à 0 — disparaissent de l'URL. Pour
        `priceMin`/`spaceMin` la recherche reste correcte par accident (0 est la
        borne implicite), mais l'aller-retour
        `build_search_url` -> `parse_search_url` perd l'information : un
        critère enregistré à 0 ne se relit pas. Pour `priceMax=0`/`spaceMax=0`
        (absurde mais saisissable) le filtre est purement et simplement ignoré.

        Comportement actuel figé ici, non corrigé.
        """
        assert build_search_url({key: 0}) == f"{SEARCH_URL}?"

    @pytest.mark.parametrize("falsy", [[], None, ""], ids=["empty_list", "none", "empty_string"])
    def test_falsy_list_criteria_are_dropped_which_is_desirable(self, falsy):
        """Pour les critères de type liste, le test de vérité est le bon
        comportement : une liste vide ne doit pas produire `estateTypes=`."""
        assert build_search_url({"estateTypes": falsy, "placeIds": falsy}) == f"{SEARCH_URL}?"


# ---------------------------------------------------------------------------
# parse_search_url
# ---------------------------------------------------------------------------

class TestParseSearchUrl:
    """Une URL collée par l'utilisateur est la façon la plus courante de créer
    une recherche : tout ce qui n'est pas relu correctement est perdu sans bruit."""

    def test_comma_joined_locations_are_split_into_separate_place_ids(self):
        """Régression réelle : une vraie URL SeLoger joint plusieurs placeIds
        par des virgules dans UNE occurrence de `locations=`, là où notre propre
        `build_search_url` répète le paramètre. Sans le split, une recherche
        multi-villes collée devenait UN seul identifiant opaque bidon —
        constaté sur « AD08FR31096,AD08FR36603,AD08FR36621,AD08FR36616 », gardé
        comme une chaîne unique au lieu de quatre ids.
        """
        url = f"{SEARCH_URL}?locations=AD08FR31096,AD08FR36603,AD08FR36621,AD08FR36616"

        assert parse_search_url(url)["placeIds"] == [
            "AD08FR31096", "AD08FR36603", "AD08FR36621", "AD08FR36616",
        ]

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("locations=AD08FR31096", ["AD08FR31096"]),
            ("locations=AD08FR31096&locations=AD08FR36603", ["AD08FR31096", "AD08FR36603"]),
            ("locations=AD08FR31096,AD08FR36603&locations=AD08FR36621",
             ["AD08FR31096", "AD08FR36603", "AD08FR36621"]),
        ],
        ids=["single", "repeated_param", "mixed_shapes"],
    )
    def test_both_real_world_url_shapes_parse(self, query, expected):
        assert parse_search_url(f"{SEARCH_URL}?{query}")["placeIds"] == expected

    @pytest.mark.parametrize(
        "key",
        ["placeIds", "distributionTypes", "estateTypes", "priceMin", "priceMax",
         "spaceMin", "spaceMax", "rooms", "bedrooms", "locationsInBuildingExcluded"],
    )
    def test_absent_parameters_produce_no_key_at_all(self, key):
        """Une clé absente et une clé à `None` ne se comportent pas pareil en
        aval (`criteria.get(...)` dans build_search_url) : l'absence doit rester
        une absence."""
        criteria = parse_search_url(SEARCH_URL)

        assert key not in criteria
        assert criteria == {"order": "DateDesc"}

    @pytest.mark.parametrize(
        ("key", "raw", "expected"),
        [
            ("priceMin", "600", 600),
            ("priceMax", "1500", 1500),
            ("spaceMin", "20", 20),
            ("spaceMax", "120", 120),
        ],
    )
    def test_numeric_parameters_become_integers(self, key, raw, expected):
        criteria = parse_search_url(f"{SEARCH_URL}?{key}={raw}")

        assert criteria[key] == expected
        assert isinstance(criteria[key], int), "une chaîne casserait la comparaison en base"

    @pytest.mark.parametrize(
        ("key", "raw"),
        [
            ("priceMin", "abc"), ("priceMax", "n%2Fa"), ("spaceMin", "20m2"),
            ("spaceMax", "1+500"), ("priceMin", "1500.0"),
        ],
        ids=["letters", "url_encoded_slash", "unit_suffix", "space_separator", "float"],
    )
    def test_unparseable_numbers_are_dropped_rather_than_kept_as_text(self, key, raw):
        """Le `try/except (ValueError, IndexError)` avale la valeur : la clé
        n'apparaît pas, plutôt que de propager une chaîne qui exploserait plus
        loin. Un filtre est silencieusement perdu — c'est le compromis assumé."""
        criteria = parse_search_url(f"{SEARCH_URL}?{key}={raw}")

        assert key not in criteria

    @pytest.mark.parametrize("key", ["priceMin", "priceMax", "spaceMin", "spaceMax", "locations", "rooms"])
    def test_a_blank_value_makes_the_parameter_vanish_before_any_conversion(self, key):
        """`parse_qs` ignore les valeurs vides (`keep_blank_values=False`) : le
        `try/except` n'est même pas atteint. Conséquence utile : une URL
        SeLoger avec `&priceMax=` (champ de formulaire laissé vide) ne produit
        pas un critère fantôme."""
        assert parse_search_url(f"{SEARCH_URL}?{key}=") == {"order": "DateDesc"}

    @pytest.mark.parametrize(
        ("query", "expected_order"),
        [("", "DateDesc"), ("order=PriceAsc", "DateDesc"), ("order=DateDesc", "DateDesc")],
        ids=["absent", "overridden", "already_datedesc"],
    )
    def test_order_is_always_forced_to_datedesc(self, query, expected_order):
        """Le tracker ne veut QUE les plus récentes : l'ordre de l'URL collée
        est écrasé, y compris s'il demandait un tri par prix. C'est ce qui rend
        les 30 annonces de la première page équivalentes aux 30 dernières
        publiées (l'API BFF qui paginait n'existe plus)."""
        assert parse_search_url(f"{SEARCH_URL}?{query}")["order"] == expected_order

    @pytest.mark.parametrize(
        ("key", "url_key"),
        [
            ("distributionTypes", "distributionTypes"),
            ("estateTypes", "estateTypes"),
            ("rooms", "rooms"),
            ("bedrooms", "bedrooms"),
            ("locationsInBuildingExcluded", "locationsInBuildingExcluded"),
        ],
    )
    def test_every_list_parameter_is_csv_split(self, key, url_key):
        criteria = parse_search_url(f"{SEARCH_URL}?{url_key}=A,B&{url_key}=C")

        assert criteria[key] == ["A", "B", "C"]

    def test_unknown_parameters_are_ignored(self):
        """SeLoger ajoute des paramètres de tracking (`utm_*`, `bd`) : ils ne
        doivent pas se retrouver dans les critères enregistrés."""
        criteria = parse_search_url(f"{SEARCH_URL}?locations=AD08FR31096&utm_source=newsletter&bd=1")

        assert criteria == {"placeIds": ["AD08FR31096"], "order": "DateDesc"}

    def test_a_full_real_url_round_trips_through_build(self):
        """L'aller-retour est ce qui garantit qu'une recherche relue produit la
        même requête. `order` est le seul champ ajouté d'office. `locations`
        est reconstruit sous forme virgule (le seul format que SeLoger honore
        pour toutes les villes, voir test_place_ids_are_comma_joined...),
        même si l'URL d'origine utilisait des occurrences répétées."""
        original = f"{SEARCH_URL}?locations=AD08FR31096&locations=AD08FR36603&priceMax=1500&rooms=2&rooms=3"

        criteria = parse_search_url(original)
        rebuilt = build_search_url(criteria, order=criteria["order"])

        assert parse_search_url(rebuilt) == criteria
        assert rebuilt == (
            f"{SEARCH_URL}?locations=AD08FR31096%2CAD08FR36603"
            "&priceMax=1500&rooms=2&rooms=3&order=DateDesc"
        )


class TestSplitCsvValues:
    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            ([], []),
            (["A"], ["A"]),
            (["A,B"], ["A", "B"]),
            (["A,B", "C"], ["A", "B", "C"]),
            ([""], [""]),
            (["A,"], ["A", ""]),
        ],
        ids=["empty", "single", "csv", "mixed", "empty_value", "trailing_comma"],
    )
    def test_flattens_comma_separated_occurrences(self, values, expected):
        """Une virgule finale produit une valeur vide : aucun filtrage n'est
        fait, la fonction est purement structurelle."""
        assert _split_csv_values(values) == expected


# ---------------------------------------------------------------------------
# Récupération des proxies
# ---------------------------------------------------------------------------

class TestFetchProxySource:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("1.2.3.4:8080\n5.6.7.8:3128", ["1.2.3.4:8080", "5.6.7.8:3128"]),
            ("  1.2.3.4:8080  \n\n5.6.7.8:3128\n", ["1.2.3.4:8080", "5.6.7.8:3128"]),
            # Pas de ":" -> ce n'est pas un couple hôte:port.
            ("1.2.3.4\nnot a proxy", []),
            # >= 30 caractères : en pratique une ligne d'entête ou de commentaire.
            (f"{'1' * 26}:8080\n1.2.3.4:8080", ["1.2.3.4:8080"]),
            ("", []),
            ("# Liste de proxies gratuits, mise à jour\n1.2.3.4:8080", ["1.2.3.4:8080"]),
        ],
        ids=["plain", "whitespace", "no_colon", "too_long", "empty", "header_comment"],
    )
    def test_keeps_only_short_lines_containing_a_colon(self, requests_mock, body, expected):
        """Le filtre est purement heuristique : « contient `:` » et « moins de
        30 caractères ». Il laisse passer n'importe quoi qui y ressemble (un
        `http://x` par exemple) — c'est acceptable, `_try_with_proxies` élimine
        ensuite ce qui ne fonctionne pas."""
        requests_mock.get("https://proxies.example/list.txt", text=body)

        assert _fetch_proxy_source("https://proxies.example/list.txt") == expected

    @pytest.mark.parametrize("status", [403, 404, 500, 301])
    def test_non_200_responses_yield_nothing(self, requests_mock, status):
        requests_mock.get("https://proxies.example/list.txt", text="1.2.3.4:8080", status_code=status)

        assert _fetch_proxy_source("https://proxies.example/list.txt") == []

    def test_any_exception_is_swallowed(self, requests_mock):
        """`except Exception: pass` : une source morte ne doit pas faire tomber
        le scrape. 42 sources publiques, la plupart cassées à tout moment."""
        requests_mock.get(
            "https://proxies.example/list.txt", exc=requests.exceptions.ConnectTimeout,
        )

        assert _fetch_proxy_source("https://proxies.example/list.txt") == []


class TestGetFreeProxies:
    @pytest.fixture
    def sources(self, monkeypatch):
        """Remplace `_fetch_proxy_source` par un compteur d'appels."""
        calls: list[str] = []

        def fake_fetch(src):
            calls.append(src)
            return [f"10.0.0.{len(calls)}:8080"]

        monkeypatch.setattr(seloger, "_fetch_proxy_source", fake_fetch)
        return calls

    def test_queries_every_source_once_and_deduplicates(self, sources, monkeypatch):
        monkeypatch.setattr(seloger, "_PROXY_SOURCES", ["s1", "s2", "s2"])
        monkeypatch.setattr(random, "shuffle", lambda seq: None)

        proxies = _get_free_proxies()

        assert sorted(sources) == ["s1", "s2", "s2"], "chaque source est soumise, doublons compris"
        # `proxies` est un set en interne : la déduplication porte sur les
        # valeurs, pas sur les sources.
        assert len(proxies) == 3

    def test_a_second_call_within_the_ttl_reuses_the_cache(self, sources, monkeypatch):
        """TTL de 180 s partagé par tout le process : sans lui, chaque scrape
        rejouerait 42 requêtes HTTP."""
        monkeypatch.setattr(seloger, "_PROXY_SOURCES", ["s1"])

        first = _get_free_proxies()
        second = _get_free_proxies()

        assert sources == ["s1"], "la deuxième invocation ne doit refaire aucune requête"
        assert second is first, "le cache est renvoyé tel quel, pas recopié"

    @pytest.mark.parametrize(
        ("age", "refetches"),
        [(0, False), (179.9, False), (180.0, True), (3600, True)],
        ids=["fresh", "just_under_ttl", "exactly_at_ttl", "long_expired"],
    )
    def test_the_cache_expires_after_exactly_180_seconds(
        self, sources, monkeypatch, age, refetches
    ):
        monkeypatch.setattr(seloger, "_PROXY_SOURCES", ["s1"])
        monkeypatch.setattr(seloger, "_PROXY_CACHE", ["9.9.9.9:80"])
        now = 1_800_000_000.0
        monkeypatch.setattr(seloger.time, "time", lambda: now)
        monkeypatch.setattr(seloger, "_PROXY_CACHE_TIME", now - age)

        result = _get_free_proxies()

        assert bool(sources) is refetches
        assert (result == ["9.9.9.9:80"]) is not refetches

    def test_an_empty_cache_is_never_considered_valid(self, sources, monkeypatch):
        """`if _PROXY_CACHE and ...` : un cache vide (aucune source vivante au
        tour précédent) est re-tenté immédiatement, sans attendre le TTL."""
        monkeypatch.setattr(seloger, "_PROXY_SOURCES", ["s1"])
        monkeypatch.setattr(seloger, "_PROXY_CACHE", [])
        monkeypatch.setattr(seloger, "_PROXY_CACHE_TIME", seloger.time.time())

        _get_free_proxies()

        assert sources == ["s1"]

    def test_the_deadline_keeps_whatever_was_already_collected(self, sources, monkeypatch):
        """`as_completed(timeout=...)` lève `TimeoutError` : le code continue
        avec les proxies déjà récupérés au lieu de tout perdre. C'est ce qui
        borne le scrape (42 sources à 5 s en série faisaient ~200 s)."""
        monkeypatch.setattr(seloger, "_PROXY_SOURCES", ["s1", "s2", "s3"])
        monkeypatch.setattr(random, "shuffle", lambda seq: None)

        def partial_then_timeout(futures, timeout=None):
            done = list(futures)[:1]
            yield from done
            raise FuturesTimeoutError

        monkeypatch.setattr(seloger, "as_completed", partial_then_timeout)

        proxies = _get_free_proxies()

        assert len(proxies) == 1, "on garde le seul résultat consommé avant le délai"
        assert proxies[0].startswith("10.0.0.")

    def test_the_result_is_shuffled_then_truncated_to_count(self, sources, monkeypatch):
        """L'ordre est aléatoire *avant* la troncature : sinon on retesterait
        toujours les mêmes proxies morts, dans le même ordre."""
        monkeypatch.setattr(seloger, "_PROXY_SOURCES", ["s1", "s2", "s3", "s4"])
        order: list[list[str]] = []

        def recording_shuffle(seq):
            order.append(list(seq))
            seq.sort()  # déterministe, et distinct de l'ordre du set

        monkeypatch.setattr(random, "shuffle", recording_shuffle)

        proxies = _get_free_proxies(count=2)

        assert len(order) == 1, "shuffle est appelé une seule fois, sur la liste complète"
        assert len(order[0]) == 4, "shuffle voit les 4 proxies, la troncature vient après"
        assert proxies == sorted(order[0])[:2]
        assert seloger._PROXY_CACHE == proxies, "c'est la liste tronquée qui est mise en cache"


class TestTryWithProxies:
    @pytest.fixture
    def one_proxy(self, monkeypatch):
        monkeypatch.setattr(seloger, "_get_free_proxies", lambda: ["1.2.3.4:8080"])

    @pytest.mark.parametrize(
        ("status", "body", "expect_response"),
        [
            (200, HAPPY_PAGE, True),
            (200, BLOCKED_PAGE, False),
            (403, HAPPY_PAGE, False),
            (500, HAPPY_PAGE, False),
        ],
        ids=["ok", "200_without_marker", "403_with_marker", "500_with_marker"],
    )
    def test_success_requires_both_a_200_and_the_data_marker(
        self, one_proxy, requests_mock, status, body, expect_response
    ):
        """Un 200 ne suffit pas : DataDome répond 200 avec une page de challenge.
        Le seul signal fiable est la présence du blob `__UFRN_FETCHER__`."""
        requests_mock.get(HOME_URL, text="accueil")
        requests_mock.get(SEARCH_URL, text=body, status_code=status)

        resp = _try_with_proxies(f"{SEARCH_URL}?locations=AD08FR31096")

        if expect_response:
            assert resp is not None and "__UFRN_FETCHER__" in resp.text
        else:
            assert resp is None

    def test_the_home_page_is_warmed_up_before_the_search_url(self, one_proxy, requests_mock):
        """SeLoger pose ses cookies sur la home : attaquer directement l'URL de
        recherche derrière un proxy neuf se fait bloquer."""
        requests_mock.get(HOME_URL, text="accueil")
        requests_mock.get(SEARCH_URL, text=HAPPY_PAGE)

        _try_with_proxies(f"{SEARCH_URL}?locations=AD08FR31096")

        paths = [r.path for r in requests_mock.request_history]
        assert paths == ["/", "/classified-search"]

    def test_each_proxy_gets_its_own_session_routed_over_http_for_both_schemes(
        self, one_proxy, requests_mock, monkeypatch
    ):
        """Une `Session` par thread de test : elles portent des cookies et un
        proxy différents, les partager mélangerait les identités. Et les deux
        schémas passent par `http://` — un proxy HTTP tunnelise le HTTPS via
        CONNECT, il ne faut surtout pas préfixer `https://`."""
        requests_mock.get(HOME_URL, text="accueil")
        requests_mock.get(SEARCH_URL, text=HAPPY_PAGE)
        sessions: list[requests.Session] = []
        real_session_cls = requests.Session

        def tracking_session():
            session = real_session_cls()
            sessions.append(session)
            return session

        monkeypatch.setattr(seloger.requests, "Session", tracking_session)

        _try_with_proxies(f"{SEARCH_URL}?locations=AD08FR31096")

        assert len(sessions) == 1
        assert sessions[0].proxies == {
            "http": "http://1.2.3.4:8080",
            "https": "http://1.2.3.4:8080",
        }
        assert sessions[0].headers["User-Agent"] == seloger.MOBILE_UA
        assert sessions[0].headers["Accept-Language"] == "fr-FR,fr;q=0.9"

    def test_a_dead_proxy_yields_none_rather_than_raising(self, one_proxy, requests_mock):
        """`except Exception: pass` dans `test_proxy` : un proxy injoignable est
        un non-événement, pas une panne du scrape."""
        requests_mock.get(HOME_URL, exc=requests.exceptions.ProxyError)

        assert _try_with_proxies(f"{SEARCH_URL}?locations=AD08FR31096") is None

    def test_no_proxy_available_returns_none_without_any_request(self, monkeypatch, requests_mock):
        monkeypatch.setattr(seloger, "_get_free_proxies", lambda: [])

        assert _try_with_proxies(f"{SEARCH_URL}?x=1") is None
        assert requests_mock.request_history == []

    def test_only_max_proxies_are_tested(self, monkeypatch, requests_mock):
        """La borne existe pour que le worker unique de scrape ne reste pas
        bloqué des heures sur 5 000 proxies morts."""
        monkeypatch.setattr(seloger, "_get_free_proxies", lambda: [f"10.0.0.{i}:80" for i in range(20)])
        requests_mock.get(HOME_URL, exc=requests.exceptions.ProxyError)

        _try_with_proxies(f"{SEARCH_URL}?x=1", max_proxies=3)

        assert len(requests_mock.request_history) == 3

    def test_gives_up_after_the_deadline_without_hanging(self):
        """Même si chaque test de proxy bloquait indéfiniment, le délai borne
        l'attente totale : la file de scrape à worker unique ne peut pas être
        gelée pour des heures. Les futures restantes sont explicitement
        annulées."""
        with patch.object(seloger, "_get_free_proxies", return_value=["1.2.3.4:8080"] * 5), \
             patch.object(seloger, "ThreadPoolExecutor") as mock_executor_cls, \
             patch.object(seloger, "as_completed", side_effect=FuturesTimeoutError):
            executor = mock_executor_cls.return_value.__enter__.return_value
            pending = MagicMock()
            executor.submit.return_value = pending

            resp = _try_with_proxies(f"{SEARCH_URL}?x=1", max_proxies=5, deadline_seconds=0.01)

        assert resp is None
        pending.cancel.assert_called()


# ---------------------------------------------------------------------------
# get_detailed_listings — le cœur du scraper
# ---------------------------------------------------------------------------

class TestGetDetailedListingsHappyPath:
    def test_extracts_every_field_from_a_realistic_page(self, serve):
        serve()

        listings = get_detailed_listings({"placeIds": ["AD08FR31096"]})

        assert listings == [{
            "id": LISTING_ID,
            "legacyId": "213456789",
            "title": "Appartement 3 pièces 65 m²",
            "headline": "Charmant 3 pièces rénové",
            "description": "Très lumineux, exposé sud, proche métro Place d'Italie.",
            "price": "1 250 €/mois",
            "priceValue": 1250.0,
            "priceDetails": "Charges comprises",
            "surface": 65.0,
            "rooms": 3,
            "propertyType": "Appartement",
            "city": "Paris",
            "district": "Paris 13ème",
            "zipCode": "75013",
            "url": CLASSIFIED_ITEM["url"],
            "photos": [
                {"url": "https://v.seloger.com/s/crop/590x330/1.jpg", "alt": "Séjour", "key": "img-1"},
                {"url": "https://v.seloger.com/s/crop/590x330/2.jpg", "alt": "Cuisine", "key": "img-2"},
            ],
            "agency": "Agence Beauséjour",
            "isPrivate": False,
            "phone": ["+33145678901"],
            "epc": "C",
            "ges": "B",
            "isNew": True,
            "isExclusive": False,
            "has3DVisit": True,
            "creationDate": "2026-07-20T08:30:00Z",
            "updateDate": "2026-07-24T11:05:00Z",
            "keyfacts": ["3 pièces", "65 m²", "2 chambres"],
        }]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("title", "Appartement 3 pièces 65 m²"),
            ("headline", "Charmant 3 pièces rénové"),
            ("description", "Très lumineux, exposé sud, proche métro Place d'Italie."),
            ("price", "1 250 €/mois"),
            ("district", "Paris 13ème"),
            ("agency", "Agence Beauséjour"),
        ],
    )
    def test_accented_text_survives_the_double_utf8_decoding(self, serve, field, value):
        """Le pipeline de production est
        `encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")`.

        Chacune de ces quatre étapes est indispensable et aucune n'est
        interchangeable : retirer l'aller-retour latin-1 produit du mojibake
        (« piÃ¨ces »), et le supprimer entièrement fait échouer `json.loads` sur
        les guillemets échappés. Ce test est le garde-fou : il compare des
        chaînes contenant é, è, û, ² et € — s'il passe, le décodage est intact.
        """
        serve()

        listing = get_detailed_listings({"placeIds": ["AD08FR31096"]})[0]

        assert listing[field] == value

    def test_the_lz_string_compressed_format_is_still_supported(self, serve):
        """SeLoger a migré vers du JSON direct, mais le fallback LZ-string reste
        en place. Le blob de ce test est produit par la vraie librairie."""
        serve(page=ufrn_page(lz_blob(serp_payload())))

        listings = get_detailed_listings({"placeIds": ["AD08FR31096"]})

        assert [item["id"] for item in listings] == [LISTING_ID]
        assert listings[0]["title"] == "Appartement 3 pièces 65 m²"

    def test_the_display_order_comes_from_the_classifieds_list(self, serve):
        """`classifiedsData` est un dict (ordre non signifiant côté API) :
        l'ordre d'affichage — donc la fraîcheur, l'ordre est DateDesc — est
        porté par la liste `classifieds`."""
        items = {
            "111": {**CLASSIFIED_ITEM, "hardFacts": {"title": "Premier"}},
            "222": {**CLASSIFIED_ITEM, "hardFacts": {"title": "Second"}},
        }
        serve(page=ufrn_page(serp_payload(items, ids=["222", "111"])))

        listings = get_detailed_listings({"placeIds": ["AD08FR31096"]})

        assert [item["id"] for item in listings] == ["222", "111"]
        assert [item["title"] for item in listings] == ["Second", "Premier"]

    def test_the_home_is_warmed_up_then_the_built_url_is_fetched(self, serve, requests_mock):
        """La session directe fait la même chorégraphie que les proxies : home
        d'abord (cookies), URL de recherche ensuite."""
        serve()
        criteria = {"placeIds": ["AD08FR31096"], "priceMax": 1500}

        get_detailed_listings(criteria, order="DateDesc")

        assert [r.path for r in requests_mock.request_history] == ["/", "/classified-search"]
        assert requests_mock.request_history[-1].url == build_search_url(criteria, "DateDesc")
        assert requests_mock.request_history[0].headers["User-Agent"] == seloger.MOBILE_UA

    def test_no_retry_and_no_sleep_on_the_first_success(self, serve, slept):
        serve()

        get_detailed_listings({"placeIds": ["AD08FR31096"]})

        assert slept == [], "le premier essai réussi ne doit rien attendre"

    def test_an_empty_result_set_is_an_empty_list_not_an_error(self, serve):
        """Zéro annonce est un résultat légitime (critères trop stricts) :
        `ScrapeService` compte là-dessus pour logger « empty » et non « error »."""
        serve(page=ufrn_page(serp_payload({})))

        assert get_detailed_listings({"placeIds": ["AD08FR31096"]}) == []


class TestGetDetailedListingsFieldExtraction:
    @pytest.mark.parametrize(
        ("surface_data", "expected"),
        [
            ({"main": 65.0, "unit": "m²"}, 65.0),
            ({"main": None}, None),
            ({}, None),
            (65.0, 65.0),
            ("65", "65"),
            (None, None),
        ],
        ids=["dict", "dict_null_main", "empty_dict", "scalar_float", "scalar_string", "null"],
    )
    def test_surface_is_read_from_a_dict_or_used_as_a_scalar(self, serve, surface_data, expected):
        """`rawData.surface` a les deux formes selon le type de bien. Le
        `isinstance(dict)` est le seul endroit du parsing qui gère deux schémas."""
        item = {**CLASSIFIED_ITEM, "rawData": {**CLASSIFIED_ITEM["rawData"], "surface": surface_data}}
        serve(page=ufrn_page(serp_payload({LISTING_ID: item})))

        assert get_detailed_listings({"placeIds": ["X"]})[0]["surface"] == expected

    def test_an_id_missing_from_classifieds_data_is_skipped(self, serve):
        """Cas réel : un id présent dans `classifieds` mais absent (ou vide) de
        `classifiedsData` — annonce retirée entre le rendu serveur et
        l'hydratation. On saute, on ne plante pas."""
        items = {LISTING_ID: CLASSIFIED_ITEM, "absent": {}}
        serve(page=ufrn_page(serp_payload(items, ids=["absent", LISTING_ID, "jamais-vu"])))

        listings = get_detailed_listings({"placeIds": ["X"]})

        assert [item["id"] for item in listings] == [LISTING_ID]

    def test_a_completely_bare_item_yields_neutral_defaults(self, serve):
        """Chaque champ est lu avec un `.get()` et un défaut : une annonce dont
        SeLoger n'a rendu qu'une coquille produit un dict complet, pas un
        KeyError qui deviendrait un retry silencieux."""
        serve(page=ufrn_page(serp_payload({"bare": {"url": "https://x/1.htm"}})))

        listing = get_detailed_listings({"placeIds": ["X"]})[0]

        assert listing["id"] == "bare"
        assert listing["title"] == "" and listing["headline"] == "" and listing["description"] == ""
        assert listing["price"] is None and listing["priceValue"] is None
        assert listing["surface"] is None and listing["rooms"] is None
        assert listing["photos"] == [] and listing["phone"] == []
        assert listing["isPrivate"] is False and listing["isNew"] is False
        assert listing["keyfacts"] == []

    def test_a_photo_without_a_url_key_breaks_the_whole_scrape(self, serve):
        """# BUG : `img["url"]` est le SEUL accès non protégé de l'extraction
        (scraper/seloger.py:388).

        Tous les autres champs passent par `.get()`. Ici un `KeyError` remonte
        au `except Exception` de la boucle de retry : les trois tentatives
        s'enchaînent puis on lève « ton IP est bloquée par DataDome », un
        diagnostic entièrement faux pour une annonce dont la galerie est
        incomplète. Le reste de la page — potentiellement 29 annonces
        valides — est perdu.

        Comportement actuel figé ici, non corrigé.
        """
        item = {**CLASSIFIED_ITEM, "gallery": {"images": [{"alt": "Sans url"}]}}
        serve(page=ufrn_page(serp_payload({LISTING_ID: item})))

        with pytest.raises(ValueError, match="DataDome"):
            get_detailed_listings({"placeIds": ["X"]}, max_retries=1)


class TestGetDetailedListingsRetryAndBackoff:
    @pytest.fixture
    def no_proxy_works(self, monkeypatch):
        """Aucun proxy ne fonctionne : chaque tentative part en `continue`."""
        monkeypatch.setattr(seloger, "_try_with_proxies", lambda url: None)

    def test_the_backoff_grows_exponentially_between_attempts(
        self, serve, slept, deterministic_backoff, no_proxy_works
    ):
        """`(2 ** attempt) + uniform(1, 3)` : aucun test n'affirmait cette
        progression, alors que c'est elle qui décide si le scraper laisse
        DataDome se calmer ou le harcèle. Avec `uniform` figé à 2.0 :
        tentative 2 -> 4 s, tentative 3 -> 6 s, tentative 4 -> 10 s.
        """
        serve(page=BLOCKED_PAGE)

        with pytest.raises(ValueError, match="DataDome"):
            get_detailed_listings({"placeIds": ["X"]}, max_retries=4)

        assert slept == [4.0, 6.0, 10.0]

    def test_the_first_attempt_never_waits(self, serve, slept, deterministic_backoff, no_proxy_works):
        serve(page=BLOCKED_PAGE)

        with pytest.raises(ValueError, match="DataDome"):
            get_detailed_listings({"placeIds": ["X"]}, max_retries=1)

        assert slept == []

    @pytest.mark.parametrize("max_retries", [1, 2, 3, 5])
    def test_the_wait_stays_within_the_documented_bounds(
        self, serve, slept, no_proxy_works, max_retries
    ):
        """Sans figer `uniform`, chaque attente doit rester dans
        `[2**n + 1, 2**n + 3]` : c'est le jitter qui évite que plusieurs
        recherches ne repartent en rafale synchronisée."""
        serve(page=BLOCKED_PAGE)

        with pytest.raises(ValueError, match="DataDome"):
            get_detailed_listings({"placeIds": ["X"]}, max_retries=max_retries)

        assert len(slept) == max_retries - 1
        for attempt, wait in enumerate(slept, start=1):
            assert 2**attempt + 1.0 <= wait <= 2**attempt + 3.0
        assert slept == sorted(slept), "le backoff ne doit jamais décroître"

    def test_a_success_on_a_later_attempt_returns_normally(self, requests_mock, slept):
        """Le premier essai est bloqué, le second passe : c'est le scénario
        pour lequel le retry existe."""
        requests_mock.get(HOME_URL, text="accueil")
        requests_mock.get(
            SEARCH_URL,
            [{"text": BLOCKED_PAGE, "status_code": 403}, {"text": HAPPY_PAGE}],
        )

        with patch.object(seloger, "_try_with_proxies", return_value=None):
            listings = get_detailed_listings({"placeIds": ["X"]}, max_retries=2)

        assert [item["id"] for item in listings] == [LISTING_ID]
        assert len(slept) == 1, "une seule attente : celle qui précède la 2e tentative"

    @pytest.mark.parametrize(
        ("status", "page", "case"),
        [
            (403, BLOCKED_PAGE, "403 explicite"),
            (200, BLOCKED_PAGE, "200 sans marqueur (challenge DataDome)"),
        ],
        ids=["403", "200_without_marker"],
    )
    def test_both_blocking_signals_trigger_the_proxy_rotation(
        self, serve, status, page, case
    ):
        serve(page=page, status=status)
        # `side_effect=calls.append` renvoie None : aucun proxy ne fonctionne.
        calls: list[str] = []

        with patch.object(seloger, "_try_with_proxies", side_effect=calls.append):
            with pytest.raises(ValueError, match="DataDome"):
                get_detailed_listings({"placeIds": ["AD08FR31096"]}, max_retries=1)

        assert calls == [build_search_url({"placeIds": ["AD08FR31096"]})], case

    def test_a_working_proxy_rescues_a_blocked_direct_attempt(self, serve):
        serve(page=BLOCKED_PAGE, status=403)
        proxied = MagicMock(status_code=200, text=HAPPY_PAGE)
        proxied.raise_for_status.return_value = None

        with patch.object(seloger, "_try_with_proxies", return_value=proxied):
            listings = get_detailed_listings({"placeIds": ["X"]}, max_retries=1)

        assert [item["id"] for item in listings] == [LISTING_ID]

    def test_a_proxy_that_still_returns_403_is_abandoned(self, serve):
        """Deuxième garde : `_try_with_proxies` filtre déjà les non-200, mais le
        code revérifie le 403 avant de parser. Chemin défensif, couvert ici."""
        serve(page=BLOCKED_PAGE, status=403)
        proxied = MagicMock(status_code=403, text=HAPPY_PAGE)

        with patch.object(seloger, "_try_with_proxies", return_value=proxied):
            with pytest.raises(ValueError, match="DataDome"):
                get_detailed_listings({"placeIds": ["X"]}, max_retries=1)

    def test_a_proxy_response_without_the_marker_is_abandoned(self, serve):
        serve(page=BLOCKED_PAGE, status=403)
        proxied = MagicMock(status_code=200, text=BLOCKED_PAGE)
        proxied.raise_for_status.return_value = None

        with patch.object(seloger, "_try_with_proxies", return_value=proxied):
            with pytest.raises(ValueError, match="DataDome"):
                get_detailed_listings({"placeIds": ["X"]}, max_retries=1)

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.TooManyRedirects,
        ],
    )
    def test_network_errors_are_retried_then_reported_as_datadome(self, requests_mock, exc):
        """Toute `RequestException` part en `continue`. Après épuisement, le
        message parle de DataDome — y compris pour une simple coupure réseau.
        Diagnostic trompeur, comportement actuel."""
        requests_mock.get(HOME_URL, exc=exc)

        with pytest.raises(ValueError, match="DataDome"):
            get_detailed_listings({"placeIds": ["X"]}, max_retries=2)

    def test_a_non_403_http_error_is_raised_by_raise_for_status_and_retried(self, serve):
        """Un 500 *avec* marqueur ne déclenche pas la rotation de proxies : il
        tombe sur `raise_for_status()`, qui lève une `HTTPError`
        (donc `RequestException`) -> `continue`."""
        serve(page=HAPPY_PAGE, status=500)

        with pytest.raises(ValueError, match="DataDome"):
            get_detailed_listings({"placeIds": ["X"]}, max_retries=1)

    def test_the_exhaustion_message_names_datadome_and_the_wait(self, serve, no_proxy_works):
        serve(page=BLOCKED_PAGE)

        with pytest.raises(ValueError, match=r"Toutes les tentatives ont échoué.*DataDome.*15-30"):
            get_detailed_listings({"placeIds": ["X"]}, max_retries=1)

    @pytest.mark.parametrize("max_retries", [0, -1], ids=["zero", "negative"])
    def test_a_non_positive_retry_budget_fails_without_any_request(
        self, serve, requests_mock, max_retries
    ):
        """`range(0)` : la boucle ne tourne pas et on lève directement. Utile à
        connaître, un appelant pourrait passer une valeur de configuration."""
        serve()

        with pytest.raises(ValueError, match="DataDome"):
            get_detailed_listings({"placeIds": ["X"]}, max_retries=max_retries)

        assert requests_mock.request_history == []


class TestGetDetailedListingsParsingFailuresBecomeRetries:
    """Le point noir du module : `except Exception: continue`.

    Tout échec de *parsing* (blob absent, JSON malformé, structure inattendue)
    est traité comme un blocage réseau : on retente, puis on lève « ton IP est
    bloquée par DataDome ». L'information réelle — la page a changé de format —
    est perdue, et le diagnostic envoie l'exploitant attendre 30 minutes pour
    rien. Chaque test de cette classe documente une cause distincte qui produit
    le MÊME message.
    """

    @pytest.mark.parametrize(
        ("page", "cause"),
        [
            (
                '<html><body>window["__UFRN_FETCHER__"] = hydrate({"data":{}})</body></html>',
                "marqueur présent mais plus de JSON.parse -> regex sans correspondance",
            ),
            (
                f'<script>window["__UFRN_FETCHER__"] = JSON.parse("{js_escape("{pas du json}")}")</script>',
                "JSON malformé dans le blob",
            ),
            (
                ufrn_page({"pageProps": {}}),  # data["classified-serp-init-data"] présent, mais...
                "enveloppe valide, structure interne vide",
            ),
            (
                f'<script>window["__UFRN_FETCHER__"] = JSON.parse("{js_escape(json.dumps({"data": {}}))}")</script>',
                "clé classified-serp-init-data absente -> KeyError",
            ),
            (
                ufrn_page(""),
                "chaîne vide -> branche LZ-string, décompression impossible",
            ),
            (
                ufrn_page("pas-du-lz-string-base64"),
                "chaîne non-LZ -> KeyError dans la librairie lzstring",
            ),
        ],
        ids=["no_json_parse", "malformed_json", "empty_page_props", "missing_key",
             "empty_lz_blob", "invalid_lz_blob"],
    )
    def test_every_parsing_failure_is_reported_as_a_datadome_block(self, serve, page, cause):
        serve(page=page)

        if "structure interne vide" in cause:
            # Seule exception : une enveloppe valide sans annonce est un
            # SUCCÈS vide, pas une erreur. Contraste volontaire.
            assert get_detailed_listings({"placeIds": ["X"]}, max_retries=1) == []
            return

        with pytest.raises(ValueError, match="DataDome") as excinfo:
            get_detailed_listings({"placeIds": ["X"]}, max_retries=1)

        assert cause  # documenté dans les ids
        assert "format" not in str(excinfo.value).lower(), (
            "le message ne mentionne jamais la vraie cause (un changement de format) : "
            "c'est exactement la perte d'information dénoncée ici"
        )

    def test_a_unicode_escape_in_the_blob_makes_the_decoding_explode(self, serve):
        """# BUG : le double-décodage ne survit pas aux échappements `\\uXXXX`
        (scraper/seloger.py:340).

        `decode("unicode_escape")` transforme `\\u00e9` en U+00E9, que
        `encode("latin-1")` réduit à l'octet 0xE9 — invalide en UTF-8 seul, donc
        `UnicodeDecodeError`. Aujourd'hui SeLoger sert de l'UTF-8 littéral et
        tout va bien ; le jour où son moteur de rendu passe à
        `JSON.stringify` avec échappement ASCII (une simple mise à jour de
        librairie), TOUT le scraper tombe en « IP bloquée par DataDome » sans
        qu'aucun log ne mentionne l'encodage.

        Comportement actuel figé ici, non corrigé.
        """
        envelope = json.dumps({"data": {"classified-serp-init-data": serp_payload()}}, ensure_ascii=False)
        page = f'<script>window["__UFRN_FETCHER__"] = JSON.parse("{js_escape_ascii(envelope)}")</script>'
        assert "\\u00e9" in page, "la fixture doit porter des échappements \\uXXXX simples"
        serve(page=page)

        with pytest.raises(ValueError, match="DataDome"):
            get_detailed_listings({"placeIds": ["X"]}, max_retries=1)

    def test_the_same_page_in_literal_utf8_parses_perfectly(self, serve):
        """Le pendant du test précédent : seule la *forme* de l'échappement
        change, et elle décide du succès ou de l'échec total du scrape."""
        serve(page=ufrn_page(serp_payload()))

        assert len(get_detailed_listings({"placeIds": ["X"]}, max_retries=1)) == 1

    def test_a_quote_followed_by_a_parenthesis_truncates_the_blob(self, serve):
        """# BUG : la regex `JSON\\.parse\\("(.+?)"\\)` est non-gourmande
        (scraper/seloger.py:334).

        Elle s'arrête au PREMIER `")` rencontré. Une annonce dont le texte
        contient cette séquence — « (voir photo") », un guillemet mal fermé dans
        une description rédigée par un agent — coupe le blob en plein milieu :
        le JSON tronqué est illisible et la page entière est perdue, à nouveau
        sous l'étiquette DataDome.

        Comportement actuel figé ici, non corrigé.
        """
        item = {**CLASSIFIED_ITEM, "mainDescription": {"headline": 'Lumineux ") plein sud', "description": ""}}
        page = ufrn_page(serp_payload({LISTING_ID: item}))
        assert re.search(r'JSON\.parse\("(.+?)"\)', page).group(1) != page.split('JSON.parse("')[1][:-2]

        serve(page=page)

        with pytest.raises(ValueError, match="DataDome"):
            get_detailed_listings({"placeIds": ["X"]}, max_retries=1)


# ---------------------------------------------------------------------------
# scrape
# ---------------------------------------------------------------------------

class TestScrape:
    def test_forces_the_date_descending_order(self, serve, monkeypatch):
        """Sans `DateDesc`, la page renvoie les 30 « meilleures » annonces au
        sens de SeLoger, pas les 30 dernières publiées : un tracker ne verrait
        jamais les nouveautés."""
        captured: list[tuple[dict, str]] = []

        def fake_detailed(criteria, order=None, max_retries=3):
            captured.append((criteria, order))
            return []

        monkeypatch.setattr(seloger, "get_detailed_listings", fake_detailed)

        scrape({"placeIds": ["AD08FR31096"], "order": "PriceAsc"})

        assert captured == [({"placeIds": ["AD08FR31096"], "order": "PriceAsc"}, "DateDesc")]

    def test_returns_the_listings_untouched(self, serve):
        serve()

        assert scrape({"placeIds": ["AD08FR31096"]}) == get_detailed_listings(
            {"placeIds": ["AD08FR31096"]}, order="DateDesc"
        )

    def test_a_real_failure_propagates_instead_of_becoming_an_empty_list(self, serve, monkeypatch):
        """`ScrapeService` distingue « aucune annonce ne correspond » (statut
        `empty`) de « le scrape a échoué » (statut `error`) : aplatir l'erreur
        en `[]` rendrait cette distinction impossible."""
        monkeypatch.setattr(seloger, "_try_with_proxies", lambda url: None)
        serve(page=BLOCKED_PAGE)

        with pytest.raises(ValueError, match="DataDome"):
            scrape({"placeIds": ["AD08FR31096"]})
