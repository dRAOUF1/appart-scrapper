"""BienIci scraper — API JSON publique realEstateAds.json.

Contrairement à SeLoger (JSON embarqué à extraire d'une page HTML) et à
Laforet (HTML pur), bienici expose directement une API JSON dédiée à la
recherche, accessible sans authentification ni session (vérifié en direct le
26/07/2026 — aucune protection Cloudflare/DataDome rencontrée) :

    GET https://www.bienici.com/realEstateAds.json?filters=<JSON url-encodé>

`filters` porte tous les critères (voir BienIciParser.to_native pour la
traduction depuis le canonique) : `filterType` ("buy"/"rent"),
`propertyType` (liste), `minPrice`/`maxPrice`, `minArea`/`maxArea`,
`minRooms`/`maxRooms`, `zoneIdsByTypes: {"zoneIds": [...]}`, et `size`/`from`
pour la pagination. La réponse est `{"total": int, "from": int, "perPage":
int, "realEstateAds": [...]}`.

Plusieurs scrapers tiers (le package `bieniciscraper` sur PyPI, entre autres)
documentent une limite dure d'environ 2500 annonces par recherche (~100
pages de 24-25) au-delà de laquelle l'API n'avance plus sur la même
combinaison de filtres — acceptée comme limitation connue, MAX_PAGES ici
matérialise ce plafond plutôt que de boucler indéfiniment dessus.
"""

from __future__ import annotations

import json
import time

import requests
from loguru import logger

ADS_URL = "https://www.bienici.com/realEstateAds.json"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

PAGE_SIZE = 24
MAX_PAGES = 100


def _headers() -> dict:
    return {
        "User-Agent": DESKTOP_UA,
        "Accept": "*/*",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Referer": "https://www.bienici.com/",
        "X-Requested-With": "XMLHttpRequest",
    }


def _fetch_page(filters: dict, max_retries: int = 3) -> dict:
    last_error: Exception | None = None
    for attempt in range(max_retries):
        if attempt > 0:
            wait = 2 ** attempt
            logger.warning(f"[BienIci] Tentative {attempt + 1}/{max_retries} (attente {wait}s)...")
            time.sleep(wait)
        try:
            resp = requests.get(
                ADS_URL,
                params={"filters": json.dumps(filters)},
                headers=_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("schéma JSON inattendu : objet attendu")
            if "realEstateAds" not in data or "total" not in data:
                raise ValueError("schéma JSON inattendu : realEstateAds/total absent")
            if not isinstance(data["realEstateAds"], list) or not isinstance(data["total"], int):
                raise ValueError("schéma JSON inattendu : types realEstateAds/total invalides")
            return data
        except requests.exceptions.RequestException as e:
            last_error = e
            logger.warning(f"[BienIci] Erreur réseau : {e}")
        except (requests.exceptions.JSONDecodeError, ValueError) as e:
            last_error = e
            logger.warning(f"[BienIci] Réponse invalide : {e}")

    raise ValueError(f"Impossible d'interroger l'API bienici après {max_retries} tentatives : {last_error}")


def scrape(native: dict) -> list[dict]:
    """Exécute le scraping : toutes les annonces pour ces critères natifs.

    `native` est déjà au format bienici (filterType, propertyType,
    zoneIdsByTypes, ...) — la traduction depuis le vocabulaire canonique est
    faite en amont par BienIciParser.to_native(). Une erreur réelle (réseau,
    format inattendu) remonte à l'appelant plutôt que d'être aplatie en liste
    vide, pour que ScrapeService puisse distinguer un échec d'une recherche
    légitimement sans résultat — même contrat que scraper.seloger.scrape().
    """
    listings: list[dict] = []
    offset = 0

    for _page in range(MAX_PAGES):
        filters = {**native, "size": PAGE_SIZE, "from": offset}
        data = _fetch_page(filters)
        page_listings = data["realEstateAds"]
        listings.extend(page_listings)

        total = data["total"]
        if not page_listings and offset < total:
            raise ValueError(
                f"API bienici : page vide inattendue à l'offset {offset} "
                f"alors que {total} annonces sont annoncées"
            )
        offset += len(page_listings)
        if not page_listings or offset >= total:
            break
    else:
        logger.warning(
            f"[BienIci] Limite de {MAX_PAGES} pages atteinte, arrêt anticipé "
            f"({len(listings)} annonces collectées) — limite structurelle connue de l'API "
            "sur une combinaison de filtres trop large."
        )

    logger.info(f"[BienIci] {len(listings)} annonces récupérées")
    return listings
