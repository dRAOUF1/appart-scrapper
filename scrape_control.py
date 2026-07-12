"""Centralized, thread-safe scrape-job submission.

Shared by the background scheduler (main.py) and the manual-trigger routes
(routes/api.py, routes/web.py, routes/admin.py) so they don't race on the
app._scrape_futures dict.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()


def submit_scrape(app, search_id: int, user_id: int) -> tuple[bool, str]:
    """Submit a scrape job for search_id if one isn't already running.

    Returns (submitted, message). Thread-safe: the check-then-submit
    sequence on app._scrape_futures is guarded by a single process-wide lock.
    """
    from services.scrape_service import ScrapeService

    with _lock:
        fut = app._scrape_futures.get(search_id)
        if fut is not None:
            if not fut.done():
                return False, "Scraping déjà en cours pour cette recherche"
            del app._scrape_futures[search_id]

        new_fut = app._scrape_executor.submit(
            ScrapeService(app.storage, app.notifier).execute, search_id, user_id
        )
        app._scrape_futures[search_id] = new_fut
    return True, "Scraping démarré en arrière-plan"
