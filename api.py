"""
Apartment Scraper API

REST API that receives HTML content and parses it to detect new listings.
Each endpoint corresponds to a specific real estate website.
"""

import json
import os
import logging
from typing import Optional
from contextlib import asynccontextmanager

import yaml
import requests
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from scrapers.seloger import SeLogerScraper
from scrapers.bienici import BienIciScraper
from scrapers.laforet import LaforetScraper
from scrapers.century21 import Century21Scraper
from scrapers.safar import SafarScraper
from scrapers.valierecortez import ValiereCortezScraper

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("scraper.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

CONFIG_PATH = "config.yaml"
DATA_PATH = "annonces.json"


def load_config() -> dict:
    """Load configuration from YAML file."""
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def load_data() -> dict:
    """Load existing listings from JSON file."""
    if not os.path.exists(DATA_PATH):
        return {}
    with open(DATA_PATH, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_data(data: dict):
    """Save listings to JSON file."""
    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def send_notification(topic: str, message: str, url: str = "", title: str = "Nouveau Logement"):
    """Send notification via ntfy.sh."""
    if not topic:
        return
    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode('utf-8'),
            headers={"Title": title},
            timeout=10
        )
        if resp.status_code == 200:
            logger.info(f"Notification envoyée: {url}")
        else:
            logger.error(f"Échec notification: {resp.status_code}")
    except Exception as e:
        logger.error(f"Échec notification: {e}")


def get_scraper(scraper_name: str, config: dict):
    """Get scraper instance by name."""
    search_criteria = config.get('search_criteria', {})
    scraper_config = config.get('scrapers', {}).get(scraper_name, {}).copy()
    scraper_config['filters'] = {**search_criteria, **scraper_config.get('filters', {})}
    
    scrapers = {
        'seloger': SeLogerScraper,
        'bienici': BienIciScraper,
        'laforet': LaforetScraper,
        'century21': Century21Scraper,
        'safar': SafarScraper,
        'valierecortez': ValiereCortezScraper,
    }
    
    scraper_class = scrapers.get(scraper_name)
    if not scraper_class:
        return None
    
    return scraper_class(scraper_config)


def process_html(scraper_name: str, html_content: str) -> dict:
    """
    Process HTML content through the appropriate scraper.
    
    Returns:
        dict with keys: new_listings, total_parsed, scraper
    """
    config = load_config()
    scraper = get_scraper(scraper_name, config)
    
    if not scraper:
        raise ValueError(f"Unknown scraper: {scraper_name}")
    
    # Parse HTML
    listings = scraper.parse_listings(html_content)
    logger.info(f"[{scraper_name}] Parsed {len(listings)} listings from HTML")
    
    # Load existing data
    all_data = load_data()
    if scraper_name not in all_data:
        all_data[scraper_name] = {}
    
    site_data = all_data[scraper_name]
    new_listings = []
    
    # Check for new listings
    for listing in listings:
        lid = scraper.get_listing_id(listing)
        if not lid:
            continue
        
        if lid not in site_data:
            new_listings.append(listing)
            site_data[lid] = listing
            
            # Send notification
            msg = scraper.format_notification(listing)
            topic = config.get('notifications', {}).get('ntfy_topic')
            url = listing.get('url', '')
            send_notification(topic, msg, url)
    
    # Save updated data
    save_data(all_data)
    
    if new_listings:
        logger.info(f"[{scraper_name}] ✅ {len(new_listings)} nouvelles annonces")
    else:
        logger.info(f"[{scraper_name}] 📭 Aucune nouvelle annonce")
    
    return {
        "scraper": scraper_name,
        "new_listings": new_listings,
        "new_count": len(new_listings),
        "total_parsed": len(listings),
    }


# FastAPI app
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 API démarrée")
    yield
    logger.info("🛑 API arrêtée")


app = FastAPI(
    title="Apartment Scraper API",
    description="API pour parser les pages HTML des sites immobiliers et détecter les nouvelles annonces",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/listings/{scraper_name}")
async def get_listings(scraper_name: str):
    """Get all stored listings for a specific scraper."""
    data = load_data()
    if scraper_name not in data:
        return {"scraper": scraper_name, "listings": [], "count": 0}
    
    listings = list(data[scraper_name].values())
    return {
        "scraper": scraper_name,
        "listings": listings,
        "count": len(listings),
    }


@app.get("/listings")
async def get_all_listings():
    """Get all stored listings from all scrapers."""
    data = load_data()
    result = {}
    total = 0
    for scraper_name, listings in data.items():
        result[scraper_name] = list(listings.values())
        total += len(listings)
    
    return {
        "listings": result,
        "total_count": total,
    }


@app.post("/parse/{scraper_name}")
async def parse_html(scraper_name: str, request: Request):
    """
    Parse HTML content and detect new listings.
    
    Send the raw HTML as the request body.
    Content-Type should be text/html or application/octet-stream.
    """
    valid_scrapers = ['seloger', 'bienici', 'laforet', 'century21', 'safar', 'valierecortez']
    
    if scraper_name not in valid_scrapers:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scraper. Valid options: {valid_scrapers}"
        )
    
    # Read raw body
    body = await request.body()
    html_content = body.decode('utf-8')
    
    if not html_content or len(html_content) < 100:
        raise HTTPException(
            status_code=400,
            detail="HTML content is empty or too short"
        )
    
    try:
        result = process_html(scraper_name, html_content)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error processing {scraper_name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Convenience endpoint for BienIci (which uses JSON, not HTML)
class BienIciPayload(BaseModel):
    """BienIci uses JSON API responses, not HTML."""
    data: dict


@app.post("/parse/bienici/json")
async def parse_bienici_json(payload: BienIciPayload):
    """
    Parse BienIci JSON API response.
    
    BienIci returns JSON from their API, not HTML.
    This endpoint accepts the JSON response directly.
    """
    config = load_config()
    scraper = get_scraper('bienici', config)
    
    if not scraper:
        raise HTTPException(status_code=500, detail="Failed to initialize BienIci scraper")
    
    # Parse JSON (BienIci's parse_listings accepts dict)
    listings = scraper.parse_listings(payload.data)
    
    # Load existing data
    all_data = load_data()
    if 'bienici' not in all_data:
        all_data['bienici'] = {}
    
    site_data = all_data['bienici']
    new_listings = []
    
    for listing in listings:
        lid = scraper.get_listing_id(listing)
        if not lid:
            continue
        
        if lid not in site_data:
            new_listings.append(listing)
            site_data[lid] = listing
            
            msg = scraper.format_notification(listing)
            topic = config.get('notifications', {}).get('ntfy_topic')
            url = listing.get('url', '')
            send_notification(topic, msg, url)
    
    save_data(all_data)
    
    return {
        "scraper": "bienici",
        "new_listings": new_listings,
        "new_count": len(new_listings),
        "total_parsed": len(listings),
    }


if __name__ == "__main__":
    import uvicorn
    config = load_config()
    api_config = config.get('api', {})
    host = api_config.get('host', '0.0.0.0')
    port = api_config.get('port', 8000)
    uvicorn.run(app, host=host, port=port)
