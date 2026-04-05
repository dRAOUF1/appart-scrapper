"""SeLoger scraper — API BFF + classified-search avec LZ-string.

Utilise un User-Agent iPhone mobile Safari + rotation de proxies gratuits
pour bypass DataDome sur les environnements cloud (Render, AWS, etc.).
"""

from __future__ import annotations

import json
import random
import re
import time
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import lzstring
from loguru import logger

BFF_ONLY_KEYS = {
    "placeIds", "priceMin", "priceMax", "spaceMin", "spaceMax",
    "rooms", "bedrooms", "distributionTypes", "estateTypes",
    "locationsInBuildingExcluded",
}

BFF_API = "https://www.seloger.com/serp-bff/search"
SEARCH_URL = "https://www.seloger.com/classified-search"

MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"

_PROXY_CACHE: list[str] = []
_PROXY_CACHE_TIME: float = 0


def _get_free_proxies(count: int = 50) -> list[str]:
    """Fetch free HTTP proxies from public APIs."""
    global _PROXY_CACHE, _PROXY_CACHE_TIME
    now = time.time()
    if _PROXY_CACHE and now - _PROXY_CACHE_TIME < 300:
        return _PROXY_CACHE

    proxies: list[str] = []
    sources = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
        "https://api.openproxylist.xyz/http.txt",
    ]
    for src in sources:
        try:
            r = requests.get(src, timeout=10)
            if r.status_code == 200:
                for line in r.text.strip().split("\n"):
                    line = line.strip()
                    if ":" in line and len(line) < 30:
                        proxies.append(line)
        except Exception:
            pass

    random.shuffle(proxies)
    _PROXY_CACHE = proxies[:count]
    _PROXY_CACHE_TIME = now
    return _PROXY_CACHE


def _try_with_proxies(url: str, max_proxies: int = 20) -> requests.Response | None:
    """Try fetching URL through free proxies until one works."""
    proxies = _get_free_proxies()
    logger.debug(f"  Testing {min(max_proxies, len(proxies))} free proxies...")

    for i, proxy in enumerate(proxies[:max_proxies]):
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
                logger.info(f"  Proxy {proxy} worked (#{i+1})")
                return resp
        except Exception:
            continue

    return None


def _build_search_url(criteria: dict, order: str | None = None) -> str:
    params = {}
    if criteria.get("distributionTypes"):
        params["distributionTypes"] = criteria["distributionTypes"]
    if criteria.get("estateTypes"):
        params["estateTypes"] = criteria["estateTypes"]
    if criteria.get("placeIds"):
        params["locations"] = criteria["placeIds"]
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


def clean_criteria_for_bff(criteria: dict) -> dict:
    """Nettoie les critères pour ne garder que les clés acceptées par l'API BFF."""
    return {k: v for k, v in criteria.items() if k in BFF_ONLY_KEYS}


def parse_search_url(url: str) -> dict:
    """Extrait les critères de recherche d'une URL SeLoger."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    criteria = {}

    if "locations" in params:
        place_ids = [v for v in params["locations"]]
        criteria["placeIds"] = place_ids
        criteria["location"] = {"placeIds": place_ids}

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

    if "order" in params:
        criteria["order"] = params["order"][0]

    if "locationsInBuildingExcluded" in params:
        criteria["locationsInBuildingExcluded"] = _split_csv_values(params["locationsInBuildingExcluded"])

    return criteria


def _split_csv_values(values: list[str]) -> list[str]:
    """Splitte les valeurs comma-separated en liste plate."""
    result = []
    for v in values:
        result.extend(v.split(","))
    return result


def get_all_ids(criteria: dict, page_size: int = 30, max_pages: int = 50) -> tuple[list, int]:
    """Récupère TOUS les IDs via l'API BFF."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Content-Type": "application/json",
    })
    session.get("https://www.seloger.com/classified-search", timeout=15)

    all_ids: list = []
    all_classifieds: list = []
    page = 1
    total = None

    while page <= max_pages:
        payload = {
            "criteria": clean_criteria_for_bff(criteria),
            "paging": {"page": page, "size": page_size},
        }

        resp = session.post(BFF_API, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        page_classifieds = data.get("classifieds", [])
        page_ids = [c["id"] for c in page_classifieds]
        total = data.get("totalCount", total)
        all_ids.extend(page_ids)
        all_classifieds.extend(page_classifieds)

        logger.debug(f"  Page {page}: {len(page_ids)} IDs (total connu: {total})")

        if len(page_ids) < page_size:
            break

        page += 1
        time.sleep(0.3)

    return all_ids, total or len(all_ids), all_classifieds


def get_detailed_listings(criteria: dict, order: str | None = None, max_retries: int = 3) -> list[dict]:
    """Récupère les données détaillées depuis le HTML compressé.

    Stratégie :
    1. Essai direct avec User-Agent iPhone mobile Safari
    2. Si 403, rotation de proxies gratuits
    """
    url = _build_search_url(criteria, order)
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
                logger.warning(f"    IP bloquée ou pas de données, tentative avec proxies gratuits...")
                resp = _try_with_proxies(url, max_proxies=30)
                if resp is None:
                    logger.warning(f"    Aucun proxy gratuit n'a fonctionné")
                    continue

            if resp.status_code == 403:
                logger.warning(f"    Bloqué (403) même avec proxy")
                continue

            resp.raise_for_status()

            if "__UFRN_FETCHER__" not in resp.text:
                logger.warning(f"    Pas de données trouvées dans le HTML")
                continue

            match = re.search(
                r'window\["__UFRN_FETCHER__"\]\s*=\s*JSON\.parse\("(.+?)"\)',
                resp.text, re.DOTALL,
            )
            if not match:
                logger.warning(f"    Format HTML inattendu")
                continue

            raw = match.group(1)
            decoded = raw.encode("utf-8").decode("unicode_escape")
            outer = json.loads(decoded)
            encoded = outer["data"]["classified-serp-init-data"]

            lzs = lzstring.LZString()
            decompressed = lzs.decompressFromBase64(encoded)
            if not decompressed:
                logger.warning(f"    Échec décodage LZ-string")
                continue

            data = json.loads(decompressed)
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
                media = item.get("media", {})

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
                    "photos": [{"url": img["url"], "alt": img.get("alt", ""), "key": img.get("key", "")} for img in gallery.get("images", [])],
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

    raise ValueError("Toutes les tentatives ont échoué. Ton IP est bloquée par DataDome. Attends 15-30 minutes et réessaie.")


def scrape(criteria: dict, use_bff: bool = True) -> tuple[list[dict], list, int]:
    """Exécute le scraping complet : données détaillées + IDs."""
    logger.info(f"[SeLoger] Début du scraping avec critères: {criteria} (BFF={'oui' if use_bff else 'non'})")

    bff_classifieds = []
    all_ids = []
    total = 0

    if use_bff:
        try:
            all_ids, total, bff_classifieds = get_all_ids(criteria)
            logger.info(f"[SeLoger] {len(all_ids)} IDs récupérés sur {total} annonces via BFF")
        except Exception as e:
            logger.error(f"[SeLoger] Échec BFF: {e}")

    detailed = []
    try:
        detailed = get_detailed_listings(criteria, order="DateDesc")
        logger.info(f"[SeLoger] {len(detailed)} annonces détaillées via mobile UA")
    except Exception as e:
        logger.warning(f"[SeLoger] Échec données détaillées: {e}")

    if detailed:
        return detailed, [l["id"] for l in detailed], len(detailed)

    if bff_classifieds:
        logger.info(f"[SeLoger] Fallback: conversion des {len(bff_classifieds)} résultats BFF en listings")
        detailed = _convert_bff_to_listings(bff_classifieds)
        return detailed, all_ids, total

    return [], all_ids, total


def _convert_bff_to_listings(classifieds: list[dict]) -> list[dict]:
    """Convertit les résultats bruts de l'API BFF en format listing standard."""
    listings = []
    for c in classifieds:
        try:
            card = c.get("card", {})
            price = card.get("price", {})
            location = c.get("location", {})
            address = location.get("address", {})
            photos = c.get("photos", [])

            listings.append({
                "id": c.get("id", ""),
                "legacyId": c.get("legacyId", ""),
                "title": card.get("title", ""),
                "headline": card.get("title", ""),
                "description": "",
                "price": price.get("text", ""),
                "priceValue": price.get("value"),
                "priceDetails": price.get("priceDetails", ""),
                "surface": card.get("surface", ""),
                "rooms": card.get("rooms", ""),
                "propertyType": card.get("propertyType", ""),
                "city": address.get("city", ""),
                "district": address.get("district", ""),
                "zipCode": address.get("zipCode", ""),
                "url": c.get("urls", {}).get("classified", ""),
                "photos": [{"url": p.get("url", ""), "alt": p.get("caption", ""), "key": ""} for p in photos],
                "agency": card.get("agency", {}).get("name", ""),
                "isPrivate": False,
                "phone": [],
                "epc": card.get("energyRate", ""),
                "ges": card.get("gesRate", ""),
                "isNew": card.get("isNew", False),
                "isExclusive": False,
                "has3DVisit": bool(card.get("has3DTour")),
                "creationDate": c.get("publicationDate", ""),
                "updateDate": c.get("lastUpdateDate", ""),
                "keyfacts": card.get("keyFacts", []),
            })
        except Exception as e:
            logger.debug(f"  Erreur conversion BFF: {e}")
            continue

    return listings
