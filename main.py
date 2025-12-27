import time
import yaml
import json
import os
import logging
import requests
import random
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from scrapers.seloger import SeLogerScraper

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
USER_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_profile_uc")

def load_config():
    if not os.path.exists(CONFIG_PATH):
        logger.error(f"Configuration file {CONFIG_PATH} not found.")
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

def send_notification(topic, message, title="Nouveau Logement Détecté"):
    if not topic:
        return
    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}", 
            data=message.encode('utf-8'),
            headers={"Title": title}
        )
        if resp.status_code == 200:
            logger.info("Notification sent successfully.")
        else:
            logger.error(f"Failed to send notification: {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")


def send_alert_notification(topic, alert_type, details=""):
    """Send an alert notification for errors like being blocked."""
    if not topic:
        return
    try:
        message = f"ALERTE SCRAPER\n\nType: {alert_type}\n"
        if details:
            message += f"Details: {details}\n"
        message += "\nVerifiez les logs et debug_seloger.html/png"
        
        resp = requests.post(
            f"https://ntfy.sh/{topic}", 
            data=message.encode('utf-8'),
            headers={
                "Title": "Scraper Bloque",
                "Priority": "high",
                "Tags": "warning",
                "Content-Type": "text/plain; charset=utf-8"
            }
        )
        if resp.status_code == 200:
            logger.info("Alert notification sent.")
        else:
            logger.error(f"Failed to send alert: {resp.status_code}")
    except Exception as e:
        logger.error(f"Failed to send alert notification: {e}")


def create_stealth_driver(config: dict):
    """Create an undetected Chrome driver with anti-bot measures."""
    headless = config.get('anti_detection', {}).get('headless', True)
    if config.get('debug', False):
        logger.info("Debug mode enabled: Running headful browser.")
        headless = False
    
    # Chrome options
    options = uc.ChromeOptions()
    
    # Persistent profile for cookies
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    options.add_argument(f"--user-data-dir={USER_DATA_DIR}")
    
    # Anti-detection arguments
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-popup-blocking")
    options.add_argument(f"--window-size={random.randint(1200, 1920)},{random.randint(800, 1080)}")
    
    # Language and locale
    options.add_argument("--lang=fr-FR")
    options.add_argument("--accept-language=fr-FR,fr;q=0.9,en;q=0.8")
    
    # Random user agent (if provided in config)
    user_agents = config.get('anti_detection', {}).get('user_agents', [])
    if user_agents:
        ua = random.choice(user_agents)
        logger.info(f"Using User-Agent: {ua}")
    
    # Create undetected driver
    driver = uc.Chrome(
        options=options,
        headless=headless,
        use_subprocess=True,
        version_main=140,  # Match installed Chrome version
    )
    
    logger.info(f"Browser launched with persistent profile: {USER_DATA_DIR}")
    return driver


def simulate_human_behavior(driver, min_delay=1, max_delay=3):
    """Simulate human-like browsing behavior."""
    try:
        # Random scroll
        scroll_distance = random.randint(200, 600)
        driver.execute_script(f"window.scrollBy(0, {scroll_distance})")
        time.sleep(random.uniform(0.5, 1.5))
        
        # More scrolling
        scroll_distance = random.randint(300, 700)
        driver.execute_script(f"window.scrollBy(0, {scroll_distance})")
        time.sleep(random.uniform(min_delay, max_delay))
        
    except Exception as e:
        logger.debug(f"Human simulation error: {e}")


def handle_cookie_consent(driver):
    """Try to handle cookie consent popups."""
    try:
        consent_selectors = [
            (By.ID, "didomi-notice-agree-button"),
            (By.CSS_SELECTOR, "button[aria-label*='accept']"),
            (By.CSS_SELECTOR, "button[aria-label*='Accepter']"),
            (By.XPATH, "//button[contains(text(), 'Accepter')]"),
            (By.XPATH, "//button[contains(text(), 'Tout accepter')]"),
            (By.CSS_SELECTOR, "[data-testid*='accept']"),
        ]
        
        for by, selector in consent_selectors:
            try:
                element = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((by, selector))
                )
                element.click()
                logger.info(f"Clicked cookie consent: {selector}")
                time.sleep(random.uniform(0.5, 1.5))
                return True
            except:
                continue
                
    except Exception as e:
        logger.debug(f"Cookie consent handling: {e}")
    
    return False


def run_scrapers():
    """Main scraper function using undetected-chromedriver."""
    logger.info("Starting scraper run...")
    config = load_config()
    if not config:
        return

    previous_listings = load_data()
    driver = None
    
    try:
        # Create undetected driver
        driver = create_stealth_driver(config)
        
        # Scrapers
        scrapers = []
        if config.get('scrapers', {}).get('seloger', {}).get('enabled', False):
            scrapers.append(SeLogerScraper(config['scrapers']['seloger']))
            
        for scraper in scrapers:
            site_name = scraper.get_name()
            logger.info(f"Running scraper: {site_name}")
            
            try:
                # Random delay before starting
                delay = random.uniform(
                    config.get('anti_detection', {}).get('min_delay', 5),
                    config.get('anti_detection', {}).get('max_delay', 15)
                )
                logger.debug(f"Sleeping for {delay:.2f} seconds...")
                time.sleep(delay)
                
                # Scrape using undetected-chromedriver
                current_listings_list, is_blocked = scraper.scrape_with_uc(driver, config)
                
                # Send alert if blocked
                if is_blocked:
                    topic = config.get('notifications', {}).get('ntfy_topic')
                    send_alert_notification(topic, "Bot Detection", f"Le scraper {site_name} a été bloqué par DataDome/Cloudflare")
                    logger.warning(f"Alert notification sent for {site_name} being blocked")
                    continue
                
                # Check for new listings
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
                        
                        # Notification
                        msg = scraper.format_notification(listing)
                        topic = config.get('notifications', {}).get('ntfy_topic')
                        send_notification(topic, msg)
                        logger.info(f"New listing detected: {lid}")
                
                if new_count > 0:
                    logger.info(f"Found {new_count} new listings for {site_name}")
                else:
                    logger.info(f"No new listings for {site_name}")
                    
            except Exception as e:
                logger.error(f"Error scraping {site_name}: {e}", exc_info=True)
    
    finally:
        if driver:
            driver.quit()
            logger.info("Browser closed.")
    
    save_data(previous_listings)
    logger.info("Scraper run completed.")


if __name__ == "__main__":
    run_scrapers()
