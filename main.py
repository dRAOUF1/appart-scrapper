import time
import yaml
import json
import os
import logging
import requests
import random
from curl_cffi import requests as curl_requests
from scrapers.seloger import SeLogerScraper
from scrapers.bienici import BienIciScraper
from scrapers.laforet import LaforetScraper
from scrapers.century21 import Century21Scraper
from scrapers.safar import SafarScraper

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


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def load_data():
    if not os.path.exists(DATA_PATH):
        return {}
    with open(DATA_PATH, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_data(data):
    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def send_notification(topic, message, url="", title="Nouveau Logement"):
    if not topic:
        return
    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}", 
            data=message.encode('utf-8'),
            headers={"Title": title}
        )
        if resp.status_code == 200:
            logger.info(f"Notification envoyée: {url}")
        else:
            logger.error(f"Échec notification: {resp.status_code}")
    except Exception as e:
        logger.error(f"Échec notification: {e}")


def send_alert(topic, alert_type, details=""):
    """Send an alert notification for errors/blocking."""
    if not topic:
        return
    try:
        message = f"ALERTE SCRAPER\n\nType: {alert_type}"
        if details:
            message += f"\nDetails: {details}"
        
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode('utf-8'),
            headers={
                "Title": alert_type,
                "Priority": "high",
                "Tags": "warning"
            },
            timeout=10
        )
        logger.info(f"Alerte envoyee: {alert_type}")
    except Exception as e:
        logger.error(f"Echec envoi alerte: {e}")


def create_session(config: dict) -> curl_requests.Session:
    """Create a curl_cffi session with Safari impersonation."""
    impersonate = config.get('anti_detection', {}).get('impersonate', 'safari17_0')
    session = curl_requests.Session(impersonate=impersonate)
    logger.info(f"Session créée (impersonate={impersonate})")
    return session


def run_scrapers():
    """Main scraper function using curl_cffi."""
    logger.info("=== Démarrage du scraping ===")
    config = load_config()
    if not config:
        return

    previous_listings = load_data()
    session = None
    
    try:
        session = create_session(config)
        
        # Merge search_criteria into scraper configs
        search_criteria = config.get('search_criteria', {})
        
        scrapers = []
        if config.get('scrapers', {}).get('seloger', {}).get('enabled', False):
            seloger_config = config['scrapers']['seloger'].copy()
            seloger_config['filters'] = {**search_criteria, **seloger_config.get('filters', {})}
            scrapers.append(SeLogerScraper(seloger_config))
        if config.get('scrapers', {}).get('bienici', {}).get('enabled', False):
            bienici_config = config['scrapers']['bienici'].copy()
            bienici_config['filters'] = {**search_criteria, **bienici_config.get('filters', {})}
            scrapers.append(BienIciScraper(bienici_config))
        if config.get('scrapers', {}).get('laforet', {}).get('enabled', False):
            laforet_config = config['scrapers']['laforet'].copy()
            laforet_config['filters'] = {**search_criteria, **laforet_config.get('filters', {})}
            scrapers.append(LaforetScraper(laforet_config))
        if config.get('scrapers', {}).get('century21', {}).get('enabled', False):
            century21_config = config['scrapers']['century21'].copy()
            century21_config['filters'] = {**search_criteria, **century21_config.get('filters', {})}
            scrapers.append(Century21Scraper(century21_config))
        if config.get('scrapers', {}).get('safar', {}).get('enabled', False):
            safar_config = config['scrapers']['safar'].copy()
            safar_config['filters'] = {**search_criteria, **safar_config.get('filters', {})}
            scrapers.append(SafarScraper(safar_config))
            
        for scraper in scrapers:
            site_name = scraper.get_name()
            logger.info(f"Scraping {site_name}...")
            
            try:
                delay = random.uniform(
                    config.get('anti_detection', {}).get('min_delay', 5),
                    config.get('anti_detection', {}).get('max_delay', 15)
                )
                time.sleep(delay)
                
                current_listings_list, is_blocked = scraper.scrape_with_curl(session, config)
                
                if is_blocked:
                    logger.error(f"❌ {site_name} BLOQUÉ par anti-bot!")
                    topic = config.get('notifications', {}).get('ntfy_topic')
                    send_alert(topic, "Bot Détecté", f"{site_name} a été bloqué par le site")
                    continue
                
                if site_name not in previous_listings:
                    previous_listings[site_name] = {}
                
                site_data = previous_listings[site_name]
                new_count = 0
                
                for listing in current_listings_list:
                    lid = scraper.get_listing_id(listing)
                    if not lid:
                        continue
                        
                    if lid not in site_data:
                        new_count += 1
                        site_data[lid] = listing
                        
                        msg = scraper.format_notification(listing)
                        topic = config.get('notifications', {}).get('ntfy_topic')
                        url = listing.get('url', '')
                        send_notification(topic, msg, url)
                
                if new_count > 0:
                    logger.info(f"✅ {site_name}: {new_count} nouvelles annonces")
                else:
                    logger.info(f"📭 {site_name}: aucune nouvelle annonce")
                    
            except Exception as e:
                logger.error(f"Erreur {site_name}: {e}", exc_info=True)
    
    finally:
        if session:
            session.close()
    
    save_data(previous_listings)
    logger.info("=== Scraping terminé ===")


if __name__ == "__main__":
    logger.info("🚀 Scraper démarré")
    
    while True:
        try:
            run_scrapers()
            
            config = load_config()
            interval = config.get('check_interval', 10)
            logger.info(f"⏳ Prochain check dans {interval} min...")
            time.sleep(interval * 60)
            
        except KeyboardInterrupt:
            logger.info("🛑 Scraper arrêté")
            config = load_config()
            topic = config.get('notifications', {}).get('ntfy_topic')
            send_alert(topic, "Scraper Arrêté", "Arrêt manuel (Ctrl+C)")
            break
        except Exception as e:
            logger.error(f"Erreur critique: {e}", exc_info=True)
            config = load_config()
            topic = config.get('notifications', {}).get('ntfy_topic')
            send_alert(topic, "Erreur Critique", str(e))
            logger.info("Redémarrage dans 1 min...")
            time.sleep(60)
