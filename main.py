#!/usr/bin/env python3
"""
SeLoger Scraper — Point d'entree principal.

Surveille les resultats de recherche SeLoger.com et envoie des notifications
push via ntfy quand de nouvelles annonces apparaissent.

Usage:
    python main.py                  # Boucle continue
    python main.py --once           # Un seul scan
    python main.py --test-notif     # Tester les notifications
    python main.py --config other.yaml  # Config alternative
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
import os
import threading
from flask import Flask

import schedule
from loguru import logger

from config import load_config, AppConfig
from notifier import Notifier
from scraper import SeLogerScraper
from storage import Storage


app = Flask(__name__)

# Optionnel : On masque les logs "werkzeug" de Flask pour 
# ne pas spammer ta console à chaque ping de Render
import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

@app.route('/')
def health_check():
    return "Scraper is running OK with Flask!", 200

def start_flask_server():
    # Render definit automatiquement la variable d'environnement PORT
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Serveur Flask demarre sur le port {port} (pour Render)")
    
    # host="0.0.0.0" est obligatoire pour que le serveur soit accessible de l'exterieur
    # use_reloader=False est crucial dans un thread pour eviter que Flask ne lance un 2eme processus
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# -- Logging setup --

def setup_logging(log_level: str) -> None:
    """Configure loguru logging."""
    logger.remove()  # Remove default handler
    logger.add(
        sys.stderr,
        level=log_level,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{module}</cyan>:<cyan>{function}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )
    # Also log to file
    logger.add(
        "seloger_scraper.log",
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {module}:{function} | {message}",
    )


# -- Core scan function --

def run_scan(config: AppConfig, scraper: SeLogerScraper, storage: Storage, notifier: Notifier) -> None:
    """Execute a single scan cycle across all searches."""
    total_new = 0
    total_scanned = 0

    for search in config.searches:
        search_url = search.url
        topic = search.topic

        logger.info(f"{'=' * 60}")
        logger.info(f"Scan [{topic}]: {search_url[:80]}...")

        try:
            # Scrape the search results
            listings = scraper.scrape(search_url)
            total_scanned += len(listings)

            if not listings:
                logger.warning(f"[{topic}] Aucune annonce trouvee")
                continue

            # Filter and save new listings
            new_listings = storage.save_batch(listings, search_url)
            total_new += len(new_listings)

            logger.info(
                f"[{topic}] Resultat: {len(listings)} annonces, "
                f"{len(new_listings)} nouvelle{'s' if len(new_listings) > 1 else ''}"
            )

            # Send individual notifications for each new listing
            for listing in new_listings:
                logger.info(
                    f"  NEW {listing.title} -- {listing.price} -- "
                    f"{listing.location} -- {listing.agency} -- {listing.url}"
                )
                notifier.notify_new_listing(topic, listing)
                time.sleep(0.5)  # Avoid rate limiting

            # Send summary for this search
            if new_listings:
                notifier.notify_summary(topic, len(new_listings), len(listings), search_url)

        except Exception as e:
            logger.error(f"Erreur lors du scan [{topic}]: {e}")
            continue

    # Stats
    stats = storage.get_stats()
    logger.info(f"{'=' * 60}")
    logger.info(
        f"Scan termine: {total_scanned} scannees, {total_new} nouvelles | "
        f"Total en base: {stats['total']} | Nouvelles aujourd'hui: {stats['new_today']}"
    )


# -- Signal handling --

_running = True


def _signal_handler(signum, frame):
    global _running
    logger.info(f"\nSignal recu ({signum}), arret en cours...")
    _running = False


# -- Main --

def main():
    parser = argparse.ArgumentParser(
        description="SeLoger.com -- Surveillance d'annonces immobilieres",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples:
  python main.py                     Lancer la surveillance continue
  python main.py --once              Faire un seul scan
  python main.py --test-notif        Tester les notifications
  python main.py --config my.yaml    Utiliser un fichier config alternatif
        """,
    )
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Chemin vers le fichier de configuration (defaut: config.yaml)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Executer un seul scan puis quitter",
    )
    parser.add_argument(
        "--test-notif",
        action="store_true",
        help="Envoyer une notification de test puis quitter",
    )
    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)
    setup_logging(config.log_level)

    logger.info("=" * 60)
    logger.info("SeLoger Scraper -- Demarrage")
    logger.info(f"   Recherches       : {len(config.searches)}")
    for s in config.searches:
        logger.info(f"     - [{s.topic}] {s.url[:60]}...")
    logger.info(f"   Intervalle       : {config.interval_minutes} min")
    logger.info(f"   ntfy server      : {config.ntfy.server}")
    logger.info(f"   Headless         : {config.browser.headless}")
    logger.info("=" * 60)

    # Initialize components
    notifier = Notifier(
        server=config.ntfy.server,
        priority=config.ntfy.priority,
    )
    storage = Storage(db_path=config.storage.db_path)

    # Test notification mode
    if args.test_notif:
        logger.info("Envoi de notifications de test...")
        for s in config.searches:
            success = notifier.send_test(s.topic)
            if success:
                logger.info(f"  OK Notification envoyee sur [{s.topic}]")
            else:
                logger.error(f"  FAIL Echec pour [{s.topic}]")
        return

    # Create scraper
    scraper = SeLogerScraper(
        headless=config.browser.headless,
        page_load_timeout=config.browser.page_load_timeout,
        action_delay=config.browser.action_delay,
    )

    # Register signal handlers
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        if args.once:
            # Single scan mode
            logger.info("Mode scan unique (--once)")
            run_scan(config, scraper, storage, notifier)
        else:
            # Continuous monitoring mode
            logger.info(
                f"Mode continu -- scan toutes les {config.interval_minutes} minutes. "
                "Ctrl+C pour arreter."
            )

            # Lancement flask
            server_thread = threading.Thread(target=start_flask_server, daemon=True)
            server_thread.start()
            ##############
            
            # Run immediately on start
            run_scan(config, scraper, storage, notifier)

            # Schedule recurring scans
            schedule.every(config.interval_minutes).minutes.do(
                run_scan, config, scraper, storage, notifier
            )

            while _running:
                schedule.run_pending()
                time.sleep(1)

    except KeyboardInterrupt:
        logger.info("\nInterruption clavier, arret...")
    finally:
        scraper.close()
        stats = storage.get_stats()
        logger.info(f"Total annonces en base : {stats['total']}")
        logger.info("SeLoger Scraper -- Arrete")


if __name__ == "__main__":
    main()
