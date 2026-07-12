"""Scrape service — orchestrates the scraping pipeline."""

from __future__ import annotations

import time

from loguru import logger


class ScrapeService:
    """Execute scraping for a search: fetch, save, notify, log.

    Depends only on storage/notifier (not on Flask) so it can run on a
    background thread and be tested without spinning up an app.
    """

    def __init__(self, storage, notifier):
        self.storage = storage
        self.notifier = notifier

    def execute(self, search_id: int, user_id: int) -> int:
        """Execute a scrape for a given search. Returns new listing count."""
        import datetime
        from scrape_logs.manager import SearchLogManager

        storage = self.storage
        notifier = self.notifier

        started_at = datetime.datetime.utcnow()
        log_mgr = SearchLogManager(search_id)
        log_mgr.start()

        try:
            search = storage.searches.get_search(search_id)
            if not search or search["user_id"] != user_id:
                return 0

            result = self._do_scrape(search, search_id, storage, notifier, started_at, log_mgr)
            # Marqué seulement si le scrape est allé à son terme (succès ou
            # échec géré) : si une exception inattendue interrompt le
            # scrape, on veut le retenter au prochain cycle plutôt que
            # d'attendre l'intervalle complet.
            storage.searches.update_last_scraped(search_id)

            log_mgr.cleanup_old_logs()
            return result

        except Exception as e:
            logger.exception(f"[search:{search_id}] Exception dans execute: {e}")
            try:
                storage.scrape_logs.create_scrape_log(
                    search_id, "error",
                    error_message=f"Exception: {e}",
                    started_at=started_at,
                )
            except Exception as log_err:
                logger.error(f"[search:{search_id}] Failed to log exception: {log_err}")
            log_mgr.cleanup_old_logs()
            raise

    def _do_scrape(self, search, search_id, storage, notifier, started_at, log_mgr):
        if not search.get("criteria", {}).get("placeIds"):
            logger.warning(f"[search:{search_id}] Critères vides, skip")
            storage.scrape_logs.create_scrape_log(
                search_id, "error",
                error_message="Critères vides ou placeIds manquant",
                started_at=started_at,
            )
            return 0

        use_bff = storage.settings.get_setting("use_bff_api", "true") == "true"

        try:
            from parsers import get_parser
            parser = get_parser(search["source"])
        except ValueError as e:
            logger.error(f"[search:{search_id}] Parser inconnu: {e}")
            storage.scrape_logs.create_scrape_log(
                search_id, "error",
                error_message=f"Parser inconnu: {e}",
                started_at=started_at,
            )
            return 0

        try:
            listings = parser.scrape(search["criteria"], use_bff=use_bff)
        except Exception as e:
            err_msg = str(e)
            logger.error(f"[search:{search_id}] Erreur scraping: {err_msg}")
            storage.scrape_logs.create_scrape_log(
                search_id, "error",
                error_message=err_msg,
                started_at=started_at,
            )
            return 0

        if not listings:
            logger.info(f"[search:{search_id}] Aucune annonce trouvée")
            storage.scrape_logs.create_scrape_log(
                search_id, "error",
                error_message="Aucune annonce trouvée",
                listings_found=0, new_listings=0,
                started_at=started_at,
            )
            return 0

        new_listings, already = storage.listings.save_and_link(listings, search_id)
        topic = search["ntfy_topic"]
        blacklist_mode = search.get("blacklist_mode", "exclude")
        blacklisted_agencies = search.get("blacklisted_agencies", [])

        needs_filter = blacklist_mode == "exclude" and blacklisted_agencies
        skip_notify_agencies = set(blacklisted_agencies) if blacklist_mode == "no_notify" else set()

        # Notifie les annonces liées mais pas encore notifiées avec succès :
        # celles de ce scrape ET celles restées en attente d'un scrape
        # précédent qui a planté ou dont l'envoi ntfy avait échoué. Marquées
        # notifiées seulement après confirmation d'envoi (pas de perte
        # silencieuse en cas de crash ou d'échec ntfy).
        pending = storage.listings.get_unnotified_listings_for_search(search_id)
        handled_ids = []
        for listing in pending:
            agency = listing.agency
            if needs_filter and agency in blacklisted_agencies:
                handled_ids.append(listing.listing_id)
                continue
            if agency in skip_notify_agencies:
                handled_ids.append(listing.listing_id)
                continue
            if notifier.notify_new_listing(topic, listing):
                handled_ids.append(listing.listing_id)
            else:
                logger.warning(
                    f"[search:{search_id}] Notification échouée pour {listing.listing_id}, "
                    "retentera au prochain scrape"
                )
            time.sleep(0.3)

        storage.listings.mark_listings_notified(search_id, handled_ids)

        # Désactivé pour éviter le spam de notifications quand il y a beaucoup de nouvelles annonces
        # if new_listings:
        #     notifier.notify_summary(topic, len(new_listings), len(listings))

        log_id = storage.scrape_logs.create_scrape_log(
            search_id, "success",
            listings_found=len(listings),
            new_listings=len(new_listings),
            details={"already_known": len(already)},
            started_at=started_at,
        )

        raw_logs = log_mgr.stop()
        storage.scrape_logs.update_scrape_log_raw(log_id, raw_logs)

        logger.info(
            f"[search:{search_id}] Scraped {len(listings)}, "
            f"{len(new_listings)} new, {len(already)} already known"
        )
        return len(new_listings)
