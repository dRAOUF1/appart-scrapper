"""SeLoger scraper — API BFF + classified-search avec LZ-string.

Contourne DataDome avec curl_cffi (impersonation Safari TLS fingerprint).
Reproduit exactement la logique de test_scrap/seloger_api.py.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qs, urlparse

import requests
from curl_cffi import requests as curl_requests
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


def clean_criteria_for_bff(criteria: dict) -> dict:
    """Nettoie les critères pour ne garder que les clés acceptées par l'API BFF."""
    return {k: v for k, v in criteria.items() if k in BFF_ONLY_KEYS}


def parse_search_url(url: str) -> dict:
    """Extrait les critères de recherche d'une URL SeLoger.

    Exemple d'URL :
    https://www.seloger.com/classified-search?distributionTypes=Rent&estateTypes=Apartment
        &locations=AD08FR31096&priceMin=600&priceMax=850&spaceMin=19

    Retourne un dict criteria compatible avec scrape().
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    criteria = {}

    if "locations" in params:
        place_ids = [v for v in params["locations"]]
        criteria["placeIds"] = place_ids
        criteria["location"] = {"placeIds": place_ids}

    if "distributionTypes" in params:
        criteria["distributionTypes"] = params["distributionTypes"]

    if "estateTypes" in params:
        criteria["estateTypes"] = params["estateTypes"]

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
        criteria["rooms"] = params["rooms"]

    if "bedrooms" in params:
        criteria["bedrooms"] = params["bedrooms"]

    if "order" in params:
        criteria["order"] = params["order"][0]

    if "locationsInBuildingExcluded" in params:
        criteria["locationsInBuildingExcluded"] = params["locationsInBuildingExcluded"]

    return criteria


def get_all_ids(criteria: dict, page_size: int = 30, max_pages: int = 50) -> tuple[list, int]:
    """Récupère TOUS les IDs via l'API BFF (fonctionne toujours, jamais bloqué)."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": MOBILE_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Content-Type": "application/json",
    })
    session.get("https://www.seloger.com/classified-search", timeout=15)

    all_ids: list = []
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

        page_ids = [c["id"] for c in data.get("classifieds", [])]
        total = data.get("totalCount", total)
        all_ids.extend(page_ids)

        logger.debug(f"  Page {page}: {len(page_ids)} IDs (total connu: {total})")

        if len(page_ids) < page_size:
            break

        page += 1
        time.sleep(0.3)

    return all_ids, total or len(all_ids)


def get_detailed_listings(criteria: dict, order: str | None = None, max_retries: int = 3) -> list[dict]:
    """Récupère les données détaillées depuis le HTML compressé.

    Utilise curl_cffi avec impersonation Safari17 pour contourner DataDome.
    """
    session = curl_requests.Session(impersonate="safari17_0")
    session.headers.update({
        "User-Agent": MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9",
    })

    params = {
        "distributionTypes": criteria.get("distributionTypes", []),
        "estateTypes": criteria.get("estateTypes", []),
        "locations": criteria.get("placeIds", []),
        "priceMin": criteria.get("priceMin", ""),
        "priceMax": criteria.get("priceMax", ""),
        "spaceMin": criteria.get("spaceMin", ""),
    }
    if order:
        params["order"] = order
    if criteria.get("locationsInBuildingExcluded"):
        params["locationsInBuildingExcluded"] = criteria["locationsInBuildingExcluded"]

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                wait = attempt * 10
                logger.info(f"  Tentative {attempt + 1}/{max_retries} (attente {wait}s)...")
                time.sleep(wait)
            else:
                logger.info(f"  Tentative 1/{max_retries}...")

            resp = session.get(SEARCH_URL, params=params, timeout=15)

            if resp.status_code == 403:
                logger.warning(f"    Bloqué (403) — IP temporairement limitée par DataDome")
                continue

            resp.raise_for_status()

            if "__UFRN_FETCHER__" not in resp.text:
                logger.warning(f"    Pas de données trouvées")
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
                    "photos": media.get("photos", []),
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
    """Exécute le scraping complet : données détaillées + IDs.

    Args:
        criteria: Critères de recherche SeLoger
        use_bff: Si True, utilise l'API BFF pour récupérer tous les IDs.
                 Si False, se limite aux résultats de classified-search.

    Returns:
        (detailed_listings, all_ids, total_count)
    """
    logger.info(f"[SeLoger] Début du scraping avec critères: {criteria} (BFF={'oui' if use_bff else 'non'})")

    detailed = []
    try:
        detailed = get_detailed_listings(criteria, order="DateDesc")
        logger.info(f"[SeLoger] {len(detailed)} annonces détaillées")
    except Exception as e:
        logger.error(f"[SeLoger] Échec données détaillées: {e}")

    all_ids = []
    total = len(detailed)

    if use_bff:
        try:
            all_ids, total = get_all_ids(criteria)
            logger.info(f"[SeLoger] {len(all_ids)} IDs récupérés sur {total} annonces")
        except Exception as e:
            logger.error(f"[SeLoger] Échec BFF: {e}")
            if detailed:
                all_ids = [l["id"] for l in detailed]
                total = len(detailed)
                logger.info(f"[SeLoger] Fallback: {len(all_ids)} IDs depuis les détails")
    else:
        all_ids = [l["id"] for l in detailed]
        total = len(detailed)
        logger.info(f"[SeLoger] BFF désactivé : {len(all_ids)} IDs depuis les détails uniquement")

    return detailed, all_ids, total
