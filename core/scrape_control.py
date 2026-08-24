"""Centralized, thread-safe scrape-job submission and queue inspection.

Shared by the background scheduler (main.py) and the manual-trigger routes
(routes/api.py, routes/web.py, routes/admin.py) so they don't race on the
app._scrape_futures dict.
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()

# Issue #17 : clé app_settings portant la pause GLOBALE du scheduler ('true'/
# 'false'). Source unique de la constante, consommée par main.py (décision de
# tick planifié) et routes/admin.py (affichage/toggle) — aucune dépendance
# circulaire : ce module n'importe ni l'un ni l'autre au niveau module.
CLE_PAUSE_SCHEDULER = "scheduler_paused"

# Clé du verrou consultatif Postgres garantissant qu'un seul process fait
# tourner le scheduler (voir main._try_acquire_scheduler_lock). Exposée ici
# pour que la vue file d'attente de l'admin (#17) lise son détenteur sans
# dupliquer la valeur — main.py conserve `_SCHEDULER_LOCK_KEY` comme alias.
CLE_VERROU_SCHEDULER = 727271

# Issue #17 : instant (time.monotonic) de soumission par recherche, maintenu à
# côté de app._scrape_futures pour afficher « soumis il y a N s » dans la vue
# file d'attente sans changer le contrat du dict lui-même (un Future).
_soumissions: dict[int, float] = {}


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
        _soumissions[search_id] = time.monotonic()
    return True, "Scraping démarré en arrière-plan"


def file_dattente(app) -> list[dict]:
    """Snapshot thread-safe de la file des scrapes (issue #17).

    Retourne une liste ordonnée de `{search_id, rang, age_s, termine}` :
      - rang 1 = scrape présumé EN COURS, rangs suivants = EN ATTENTE ;
      - age_s = secondes écoulées depuis la soumission (0 si inconnue).

    Avec ThreadPoolExecutor(max_workers=1), l'ordre d'exécution est FIFO sur
    les soumissions : l'ordre d'insertion de _scrape_futures reflète donc la
    file réelle, aux ré-soumissions près (une recherche terminée puis relancée
    repasse en queue — ce qui est bien le comportement observé).

    Ne consulte AUCUN état externe (pas de base, pas de pause) : cette fonction
    reste utilisable quel que soit l'état de l'app et ne lève jamais — c'est ce
    qui garantit qu'un fragment admin pollé toutes les quelques secondes ne
    rend jamais 500.
    """
    with _lock:
        # Purge des timestamps dont le future a disparu du dict partagé
        # (nettoyé après exécution) : évite une croissance sans fin.
        vivants = set(app._scrape_futures)
        for search_id in [sid for sid in _soumissions if sid not in vivants]:
            del _soumissions[search_id]

        entrees: list[dict] = []
        maintenant = time.monotonic()
        for search_id, fut in app._scrape_futures.items():
            termine = bool(fut is not None and fut.done())
            soumis_a = _soumissions.get(search_id)
            entrees.append(
                {
                    "search_id": search_id,
                    "rang": len(entrees) + 1,
                    "age_s": int(maintenant - soumis_a) if soumis_a is not None else 0,
                    "termine": termine,
                }
            )
        return entrees
