"""SeLoger scraper — page classified-search + données embarquées.

Les annonces sont lues dans le blob JSON `window["__UFRN_FETCHER__"]` de la
page de résultats (historiquement compressé en LZ-string, aujourd'hui du
JSON direct — les deux sont gérés, voir get_detailed_listings).

Utilise un User-Agent iPhone mobile Safari + rotation de proxies gratuits
pour bypass DataDome sur les environnements cloud (Render, AWS, etc.).

L'API BFF (`/serp-bff/search`) a été retirée : elle servait à récupérer
l'intégralité des IDs page par page, mais elle rejette désormais nos
requêtes (400 — son schéma attend un objet `criteria.location` et non plus
une liste `placeIds`). Elle n'est pas remplacée : la page de résultats est
triée par date décroissante, donc les 30 annonces qu'elle renvoie sont les
30 plus récentes, ce qui est exactement ce qu'un tracker doit voir.
"""

from __future__ import annotations

import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from urllib.parse import parse_qs, urlencode, urlparse

import lzstring
import requests
from loguru import logger

SEARCH_URL = "https://www.seloger.com/classified-search"

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
    " (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

_PROXY_CACHE: list[str] = []
_PROXY_CACHE_TIME: float = 0

_PROXY_SOURCES = [
    # Direct APIs
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    "https://api.openproxylist.xyz/http.txt",
    "https://proxy-list.download/api/v1/get?type=http",
    "https://proxyscan.io/download?type=http",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/databay-labs/free-proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/gfpcom/free-proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies_anonymous/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/zloi-user/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/hookzof/proxyscrape/master/proxy-list/http.txt",
    "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://raw.githubusercontent.com/YFZMATE/Proxy-List/main/http.txt",
    "https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/HTTP.txt",
    "https://raw.githubusercontent.com/ALIILAPRO/Proxy/main/http.txt",
    "https://raw.githubusercontent.com/mertguvencli/http-proxy-list/main/proxy-list/data.txt",
    "https://raw.githubusercontent.com/saisuiu/UAS/main/http.txt",
    "https://raw.githubusercontent.com/opsxcq/proxy-list/master/list.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies.txt",
    "https://raw.githubusercontent.com/hendrikbgr/Free-Proxy-Repo/master/proxy_list.txt",
    "https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt",
    "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/http/http.txt",
    "https://raw.githubusercontent.com/UptimerBot/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/ProxyScraper/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/Proxy-List/Proxy-List/main/http.txt",
    "https://raw.githubusercontent.com/Free-Proxy-List/Proxy-List/main/http.txt",
    "https://raw.githubusercontent.com/Proxy4parsing/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/ProxyScrape/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/Proxy-List-Download/Proxy-List/main/http.txt",
    "https://raw.githubusercontent.com/Proxy-List-Provider/Proxy-List/main/http.txt",
    "https://raw.githubusercontent.com/Proxy-List-Provider/Proxy-List/main/https.txt",
    "https://raw.githubusercontent.com/Proxy-List-Provider/Proxy-List/main/socks4.txt",
    "https://raw.githubusercontent.com/Proxy-List-Provider/Proxy-List/main/socks5.txt",
    "https://raw.githubusercontent.com/Proxy-List-Provider/Proxy-List/main/anonymous.txt",
    "https://raw.githubusercontent.com/Proxy-List-Provider/Proxy-List/main/elite.txt",
]


def _fetch_proxy_source(src: str) -> list[str]:
    found = []
    try:
        r = requests.get(src, timeout=5)
        if r.status_code == 200:
            for line in r.text.strip().split("\n"):
                line = line.strip()
                if ":" in line and len(line) < 30:
                    found.append(line)
    except Exception:
        pass
    return found


def _get_free_proxies(count: int = 5000, deadline_seconds: float = 20) -> list[str]:
    """Fetch free HTTP proxies from all public sources in parallel.

    Bounded by deadline_seconds so a handful of slow/dead sources can't
    stall the whole scrape (previously up to ~200s fetching 42 sources
    sequentially at a 5s timeout each).
    """
    global _PROXY_CACHE, _PROXY_CACHE_TIME
    now = time.time()
    if _PROXY_CACHE and now - _PROXY_CACHE_TIME < 180:
        return _PROXY_CACHE

    proxies: set[str] = set()
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(_fetch_proxy_source, src): src for src in _PROXY_SOURCES}
        try:
            for future in as_completed(futures, timeout=deadline_seconds):
                proxies.update(future.result())
        except TimeoutError:
            logger.warning(
                f"  Délai de {deadline_seconds}s dépassé pour la récupération des proxies,"
                f" on continue avec {len(proxies)} trouvés"
            )
            for f in futures:
                f.cancel()

    proxy_list = list(proxies)
    random.shuffle(proxy_list)
    _PROXY_CACHE = proxy_list[:count]
    _PROXY_CACHE_TIME = now
    return _PROXY_CACHE


def _try_with_proxies(url: str, max_proxies: int = 50, deadline_seconds: float = 60) -> requests.Response | None:
    """Try fetching URL through free proxies in parallel (10 workers).

    Each proxy test runs in its own thread with its own Session.
    First proxy that succeeds cancels all remaining tests. Bounded by
    max_proxies and deadline_seconds so a run of unresponsive/black-holed
    proxies can't stall the single-worker scrape queue for the whole system.
    """
    proxies = _get_free_proxies()
    logger.info(
        f"  Testing up to {min(max_proxies, len(proxies))} proxies in parallel"
        f" (from {len(proxies)} available)..."
    )

    def test_proxy(proxy):
        try:
            session = requests.Session()
            session.proxies = {
                "http": f"http://{proxy}",
                "https": f"http://{proxy}",
            }
            session.headers.update({
                "User-Agent": MOBILE_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9",
            })
            session.get("https://www.seloger.com/", timeout=8)
            resp = session.get(url, timeout=12)

            if resp.status_code == 200 and "__UFRN_FETCHER__" in resp.text:
                return proxy, resp
        except Exception:
            pass
        return proxy, None

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(test_proxy, p): p for p in proxies[:max_proxies]}
        try:
            for future in as_completed(futures, timeout=deadline_seconds):
                proxy, resp = future.result()
                if resp is not None:
                    # Cancel remaining tests
                    for f in futures:
                        f.cancel()
                    logger.info(f"  Proxy {proxy} worked")
                    return resp
        except TimeoutError:
            logger.warning(f"  Délai de {deadline_seconds}s dépassé, abandon de la rotation de proxies")
            for f in futures:
                f.cancel()

    return None


def build_search_url(criteria: dict, order: str | None = None) -> str:
    params = {}
    if criteria.get("distributionTypes"):
        params["distributionTypes"] = criteria["distributionTypes"]
    if criteria.get("estateTypes"):
        params["estateTypes"] = criteria["estateTypes"]
    if criteria.get("placeIds"):
        # SeLoger attend les placeIds joints par des virgules dans une seule
        # occurrence de `locations=` ; avec doseq=True et une liste, urlencode
        # produit des paramètres répétés (locations=A&locations=B) que
        # SeLoger ne traite pas comme prévu et où seul le premier est retenu.
        params["locations"] = ",".join(criteria["placeIds"])
    if criteria.get("priceMin"):
        params["priceMin"] = criteria["priceMin"]
    if criteria.get("priceMax"):
        params["priceMax"] = criteria["priceMax"]
    if criteria.get("spaceMin"):
        params["spaceMin"] = criteria["spaceMin"]
    if criteria.get("spaceMax"):
        params["spaceMax"] = criteria["spaceMax"]
    if criteria.get("rooms"):
        params["rooms"] = criteria["rooms"]
    if criteria.get("bedrooms"):
        params["bedrooms"] = criteria["bedrooms"]
    if order:
        params["order"] = order
    if criteria.get("locationsInBuildingExcluded"):
        params["locationsInBuildingExcluded"] = criteria["locationsInBuildingExcluded"]

    query = urlencode(params, doseq=True)
    return f"{SEARCH_URL}?{query}"


def parse_search_url(url: str) -> dict:
    """Extrait les critères de recherche d'une URL SeLoger."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    criteria = {}

    if "locations" in params:
        # Une vraie URL SeLoger peut joindre plusieurs placeIds par des
        # virgules dans une seule occurrence de `locations=`, là où notre
        # build_search_url() répète le paramètre. Sans ce split, une URL
        # multi-villes collée par l'utilisateur devient UN seul placeId
        # opaque bidon (constaté sur une recherche réelle :
        # "AD08FR31096,AD08FR36603,AD08FR36621,AD08FR36616" gardé comme une
        # unique chaîne au lieu de quatre identifiants).
        criteria["placeIds"] = _split_csv_values(params["locations"])

    if "distributionTypes" in params:
        criteria["distributionTypes"] = _split_csv_values(params["distributionTypes"])

    if "estateTypes" in params:
        criteria["estateTypes"] = _split_csv_values(params["estateTypes"])

    if "priceMin" in params:
        try:
            criteria["priceMin"] = int(params["priceMin"][0])
        except (ValueError, IndexError):
            pass

    if "priceMax" in params:
        try:
            criteria["priceMax"] = int(params["priceMax"][0])
        except (ValueError, IndexError):
            pass

    if "spaceMin" in params:
        try:
            criteria["spaceMin"] = int(params["spaceMin"][0])
        except (ValueError, IndexError):
            pass

    if "spaceMax" in params:
        try:
            criteria["spaceMax"] = int(params["spaceMax"][0])
        except (ValueError, IndexError):
            pass

    if "rooms" in params:
        criteria["rooms"] = _split_csv_values(params["rooms"])

    if "bedrooms" in params:
        criteria["bedrooms"] = _split_csv_values(params["bedrooms"])

    criteria["order"] = "DateDesc"

    if "locationsInBuildingExcluded" in params:
        criteria["locationsInBuildingExcluded"] = _split_csv_values(params["locationsInBuildingExcluded"])

    return criteria


def _split_csv_values(values: list[str]) -> list[str]:
    """Splitte les valeurs comma-separated en liste plate."""
    result = []
    for v in values:
        result.extend(v.split(","))
    return result


def get_detailed_listings(criteria: dict, order: str | None = None, max_retries: int = 3) -> list[dict]:
    """Récupère les données détaillées depuis le HTML compressé.

    Stratégie :
    1. Essai direct avec User-Agent iPhone mobile Safari
    2. Si 403, rotation de proxies gratuits
    """
    url = build_search_url(criteria, order)
    logger.debug(f"  URL de recherche: {url[:120]}...")

    for attempt in range(max_retries):
        resp = None

        try:
            if attempt > 0:
                wait = (2 ** attempt) + random.uniform(1.0, 3.0)
                logger.info(f"  Tentative {attempt + 1}/{max_retries} (attente {wait:.1f}s)...")
                time.sleep(wait)
            else:
                logger.info(f"  Tentative 1/{max_retries}...")

            # Essai direct
            session = requests.Session()
            session.headers.update({
                "User-Agent": MOBILE_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9",
            })
            session.get("https://www.seloger.com/", timeout=15)
            resp = session.get(url, timeout=20)

            if resp.status_code == 403 or "__UFRN_FETCHER__" not in resp.text:
                # IP bloquée — essayer avec proxies
                logger.warning("    IP bloquée ou pas de données, tentative avec proxies gratuits...")
                resp = _try_with_proxies(url)
                if resp is None:
                    logger.warning("    Aucun proxy gratuit n'a fonctionné")
                    continue

            if resp.status_code == 403:
                logger.warning("    Bloqué (403) même avec proxy")
                continue

            resp.raise_for_status()

            if "__UFRN_FETCHER__" not in resp.text:
                logger.warning("    Pas de données trouvées dans le HTML")
                continue

            match = re.search(
                r'window\["__UFRN_FETCHER__"\]\s*=\s*JSON\.parse\("(.+?)"\)',
                resp.text, re.DOTALL,
            )
            if not match:
                logger.warning("    Format HTML inattendu")
                continue

            raw = match.group(1)
            # Fix double-encoded UTF-8 (unicode_escape produces mojibake,
            # so encode back to latin-1 and decode as proper UTF-8)
            decoded = raw.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")
            outer = json.loads(decoded)
            raw_data = outer["data"]["classified-serp-init-data"]

            # SeLoger a changé le format : maintenant un dict JSON direct,
            # mais on garde le fallback LZ-string au cas où.
            if isinstance(raw_data, str):
                lzs = lzstring.LZString()
                decompressed = lzs.decompressFromBase64(raw_data)
                if not decompressed:
                    logger.warning("    Échec décodage LZ-string")
                    continue
                data = json.loads(decompressed)
            else:
                data = raw_data

            page_props = data.get("pageProps", {})
            classified_ids = page_props.get("classifieds", [])
            classifieds_data = page_props.get("classifiedsData", {})

            listings = []
            for listing_id in classified_ids:
                item = classifieds_data.get(listing_id, {})
                if not item:
                    continue

                hf = item.get("hardFacts", {})
                price_info = hf.get("price", {})
                location = item.get("location", {}).get("address", {})
                metadata = item.get("metadata", {})
                provider = item.get("provider", {})
                card_provider = item.get("cardProvider", {})
                main_desc = item.get("mainDescription", {})
                tags = item.get("tags", {})
                raw_data = item.get("rawData", {})
                gallery = item.get("gallery", {})

                surface_data = raw_data.get("surface", {})
                surface_value = surface_data.get("main") if isinstance(surface_data, dict) else surface_data

                listings.append({
                    "id": listing_id,
                    "legacyId": metadata.get("legacyId"),
                    "title": hf.get("title", ""),
                    "headline": main_desc.get("headline", ""),
                    "description": main_desc.get("description", ""),
                    "price": price_info.get("formatted"),
                    "priceValue": raw_data.get("price"),
                    "priceDetails": price_info.get("additionalInformation"),
                    "surface": surface_value,
                    "rooms": raw_data.get("nbroom"),
                    "propertyType": raw_data.get("propertyTypeLabel"),
                    "city": location.get("city"),
                    "district": location.get("district"),
                    "zipCode": location.get("zipCode"),
                    "url": item.get("url", ""),
                    "photos": [
                        {"url": img["url"], "alt": img.get("alt", ""), "key": img.get("key", "")}
                        for img in gallery.get("images", [])
                    ],
                    "agency": card_provider.get("title"),
                    "isPrivate": provider.get("isPrivateOwner", False),
                    "phone": provider.get("phoneNumbers", []),
                    "epc": item.get("energyClass", ""),
                    "ges": item.get("gesClass", ""),
                    "isNew": tags.get("isNew", False),
                    "isExclusive": tags.get("isExclusive", False),
                    "has3DVisit": tags.get("has3DVisit", False),
                    "creationDate": metadata.get("creationDate"),
                    "updateDate": metadata.get("updateDate"),
                    "keyfacts": hf.get("keyfacts", []),
                })

            logger.info(f"[SeLoger] {len(listings)} annonces détaillées récupérées")
            return listings

        except requests.exceptions.RequestException as e:
            logger.error(f"    Erreur réseau: {e}")
            continue
        except Exception as e:
            logger.error(f"    Erreur: {e}")
            continue

    raise ValueError(
        "Toutes les tentatives ont échoué. Ton IP est bloquée par DataDome."
        " Attends 15-30 minutes et réessaie."
    )


def scrape(criteria: dict) -> list[dict]:
    """Exécute le scraping : les annonces de la page de résultats.

    Une erreur réelle (réseau, blocage anti-bot, format inattendu) remonte à
    l'appelant plutôt que d'être aplatie en liste vide, pour que
    ScrapeService puisse distinguer un échec d'une recherche légitimement
    sans résultat.

    `criteria` est déjà au format natif SeLoger ici (placeIds,
    distributionTypes, ...) — la traduction depuis le vocabulaire canonique
    est faite en amont par SeLogerParser.to_native().
    """
    logger.info(f"[SeLoger] Début du scraping avec critères: {criteria}")

    listings = get_detailed_listings(criteria, order="DateDesc")
    logger.info(f"[SeLoger] {len(listings)} annonces récupérées")
    return listings
