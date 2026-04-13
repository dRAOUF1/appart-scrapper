"""Scrape service — orchestrates the scraping pipeline."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from flask import Flask


class ScrapeService:
    """Execute scraping for a search: fetch, save, notify, log."""

    def __init__(self, app: Flask):
        self.app = app

    def execute(self, search_id: int, user_id: int) -> int:
        """Execute a scrape for a given search. Returns new listing count."""
        import datetime
        from log_manager import SearchLogManager

        started_at = datetime.datetime.utcnow()
        log_mgr = SearchLogManager(search_id)
        log_mgr.start()

        try:
            with self.app.app_context():
                storage = self.app.storage
                notifier = self.app.notifier

                search = storage.get_search(search_id)
                if not search or search["user_id"] != user_id:
                    return 0

                if not search.get("criteria", {}).get("placeIds"):
                    logger.warning(f"[search:{search_id}] Critères vides, skip")
                    storage.create_scrape_log(
                        search_id, "error",
                        error_message="Critères vides ou placeIds manquant",
                        started_at=started_at,
                    )
                    log_mgr.cleanup_old_logs()
                    return 0

                use_bff = storage.get_setting("use_bff_api", "true") == "true"

                try:
                    from parsers import get_parser
                    parser = get_parser(search["source"])
                except ValueError as e:
                    logger.error(f"[search:{search_id}] Parser inconnu: {e}")
                    storage.create_scrape_log(
                        search_id, "error",
                        error_message=f"Parser inconnu: {e}",
                        started_at=started_at,
                    )
                    log_mgr.cleanup_old_logs()
                    return 0

                try:
                    listings = parser.scrape(search["criteria"], use_bff=use_bff)
                except Exception as e:
                    err_msg = str(e)
                    logger.error(f"[search:{search_id}] Erreur scraping: {err_msg}")
                    storage.update_last_scraped(search_id)
                    storage.create_scrape_log(
                        search_id, "error",
                        error_message=err_msg,
                        started_at=started_at,
                    )
                    log_mgr.cleanup_old_logs()
                    return 0

                if not listings:
                    logger.info(f"[search:{search_id}] Aucune annonce trouvée")
                    storage.update_last_scraped(search_id)
                    storage.create_scrape_log(
                        search_id, "error",
                        error_message="Aucune annonce trouvée",
                        listings_found=0, new_listings=0,
                        started_at=started_at,
                    )
                    log_mgr.cleanup_old_logs()
                    return 0

                new_listings, already = storage.save_and_link(listings, search_id)
                storage.update_last_scraped(search_id)
                topic = search["ntfy_topic"]

                for listing in new_listings:
                    notifier.notify_new_listing(topic, listing)
                    time.sleep(0.3)

                # Désactivé pour éviter le spam de notifications quand il y a beaucoup de nouvelles annonces
                # if new_listings:
                #     notifier.notify_summary(topic, len(new_listings), len(listings))

                log_id = storage.create_scrape_log(
                    search_id, "success",
                    listings_found=len(listings),
                    new_listings=len(new_listings),
                    details={"already_known": len(already)},
                    started_at=started_at,
                )

                raw_logs = log_mgr.stop()
                storage.update_scrape_log_raw(log_id, raw_logs)
                log_mgr.cleanup_old_logs()

                logger.info(
                    f"[search:{search_id}] Scraped {len(listings)}, "
                    f"{len(new_listings)} new, {len(already)} already known"
                )
                return len(new_listings)

        except Exception as e:
            logger.exception(f"[search:{search_id}] Exception dans execute: {e}")
            try:
                storage.update_last_scraped(search_id)
                storage.create_scrape_log(
                    search_id, "error",
                    error_message=f"Exception: {e}",
                    started_at=started_at,
                )
            except Exception as log_err:
                logger.error(f"[search:{search_id}] Failed to log exception: {log_err}")
            log_mgr.cleanup_old_logs()
            raise
