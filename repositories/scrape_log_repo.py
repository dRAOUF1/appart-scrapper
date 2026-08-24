"""Scrape log repository — file-based CRUD for scrape execution logs."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import scrape_logs.storage as log_storage
from repositories.base import BaseRepository
from scrape_logs.exporter import export_search_logs, import_search_logs
from scrape_logs.storage import (
    allocate_log_id,
    append_entry,
    find_entry_any,
    read_entries,
    read_raw_log,
    write_raw_log,
)

# Longueur maximale d'un message d'erreur dans le classement « top erreurs » :
# au-delà, le message est tronqué AVANT regroupement (deux stacktraces d'une
# même panne partagent alors leur préfixe et comptent ensemble).
_TAILLE_MESSAGE_ERREUR = 120
_TOP_ERREURS = 5


class ScrapeLogRepository(BaseRepository):
    """Scrape log CRUD operations (file storage)."""

    def create_scrape_log(
        self, search_id: int, status: str, listings_found: int = 0, new_listings: int = 0,
        error_message: str = "", details: dict | None = None, started_at=None,
    ) -> int:
        now = started_at or datetime.utcnow()
        completed_at = datetime.utcnow()
        duration = (completed_at - now).total_seconds() if started_at else 0
        log_id = allocate_log_id()
        entry = {
            "id": log_id,
            "search_id": search_id,
            "started_at": now,
            "completed_at": completed_at,
            "status": status,
            "listings_found": listings_found,
            "new_listings": new_listings,
            "error_message": error_message,
            "details": details or {},
            "duration_sec": duration,
            "raw_logs_file": f"raw/{log_id}.log",
        }
        append_entry(search_id, entry)
        return log_id

    def get_scrape_logs(self, search_id: int, limit: int = 50, offset: int = 0, status_filter: str = "") -> list[dict]:
        logs = read_entries(search_id)
        if status_filter:
            logs = [log for log in logs if log.get("status") == status_filter]
        logs.sort(key=lambda log: log.get("started_at") or datetime.min, reverse=True)
        return logs[offset: offset + limit]

    def count_scrape_logs(self, search_id: int, status_filter: str = "") -> int:
        logs = read_entries(search_id)
        if status_filter:
            logs = [log for log in logs if log.get("status") == status_filter]
        return len(logs)

    def get_scrape_stats(self, search_id: int) -> dict:
        logs = read_entries(search_id)
        total = len(logs)
        success_count = sum(1 for log in logs if log.get("status") == "success")
        error_count = sum(1 for log in logs if log.get("status") == "error")
        # "empty" = ran fine, legitimately found nothing (e.g. no listing
        # matches the requested location/filters right now) — distinct from
        # "error" (something actually broke) so it isn't shown/counted as a
        # failure in the UI/stats.
        empty_count = sum(1 for log in logs if log.get("status") == "empty")
        avg_listings = sum(log.get("listings_found") or 0 for log in logs) / total if total else 0
        avg_new = sum(log.get("new_listings") or 0 for log in logs) / total if total else 0
        avg_duration = sum(log.get("duration_sec") or 0 for log in logs) / total if total else 0
        last_scrape = None
        if logs:
            last_scrape = max(logs, key=lambda log: log.get("started_at") or datetime.min)
            last_scrape = {
                "status": last_scrape.get("status"),
                "started_at": last_scrape.get("started_at"),
                "error_message": last_scrape.get("error_message"),
            }
        return {
            "total": total,
            "success_count": success_count,
            "error_count": error_count,
            "empty_count": empty_count,
            "avg_listings": avg_listings,
            "avg_new": avg_new,
            "avg_duration": avg_duration,
            "last_scrape": last_scrape,
        }

    def update_scrape_log_raw(self, log_id: int, raw_logs: str) -> bool:
        found = find_entry_any(log_id)
        if not found:
            return False
        search_id, _ = found
        return write_raw_log(search_id, log_id, raw_logs)

    def get_scrape_log_raw(self, log_id: int, user_id: int | None = None) -> dict | None:
        found = find_entry_any(log_id)
        if not found:
            return None
        search_id, entry = found
        if user_id is not None:
            conn = self._get_conn_for_request()
            try:
                with self._dict_cursor(conn) as cur:
                    cur.execute(
                        "SELECT id FROM searches WHERE id = %s AND user_id = %s",
                        (search_id, user_id),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
            finally:
                self._release_conn(conn)
        entry = dict(entry)
        entry["raw_logs"] = read_raw_log(search_id, log_id)
        return entry

    def get_latest_scrape_log_id(self, search_id: int) -> int | None:
        logs = read_entries(search_id)
        if not logs:
            return None
        latest = max(logs, key=lambda log: log.get("started_at") or datetime.min)
        return latest.get("id")

    # ------------------------------------------------------------------
    # Vue GLOBALE des scrapes (issue #19) — lecture seule, toutes recherches.
    #
    # Le stockage est un JSONL par recherche : il n'existe ni SQL ni index ici,
    # donc l'agrégation vit DANS le repository (une passe par fichier de
    # métadonnées, bornée par la rétention de 5 jours de cleanup_old_logs) —
    # jamais dans les routes, qui ne reçoivent que leur page de résultats.
    # ------------------------------------------------------------------

    @staticmethod
    def _iterer_toutes_les_entrees() -> Iterator[dict]:
        """Itère sur les entrées de TOUTES les recherches, sans tout charger.

        Même contrat que find_entry_any : seuls les répertoires `search_<id>`
        sont considérés, les autres noms (exports, counter.json…) sont ignorés.
        """
        racine = log_storage.SCRAPE_LOGS_DIR
        if not os.path.isdir(racine):
            return
        for name in os.listdir(racine):
            if not name.startswith("search_"):
                continue
            try:
                search_id = int(name.split("_")[-1])
            except ValueError:
                continue
            yield from read_entries(search_id)

    @staticmethod
    def _borne_date(valeur, *, fin_de_journee: bool = False) -> datetime | None:
        """Convertit un filtre date du formulaire (« AAAA-MM-JJ ») en borne.

        Une valeur illisible est ignorée silencieusement (comportement des
        filtres date_from/date_to existants du journal d'audit), pas une erreur.
        `fin_de_journee` étend la borne à 23:59:59 pour une borne supérieure
        inclusive.
        """
        if not valeur:
            return None
        try:
            borne = datetime.fromisoformat(str(valeur))
        except ValueError:
            return None
        if fin_de_journee:
            borne = borne.replace(hour=23, minute=59, second=59, microsecond=999999)
        return borne

    @classmethod
    def _filtrer_entrees(
        cls,
        entrees: Iterator[dict],
        *,
        status_filter: str = "",
        search_ids: list[int] | None = None,
        date_from=None,
        date_to=None,
    ) -> list[dict]:
        """Applique les filtres de la vue globale et trie du plus récent au plus ancien.

        `search_ids` restreint à des recherches données (filtre « recherche » du
        formulaire, ou recherches d'une source résolues en amont : les entrées
        de log ne portent PAS la source, elle appartient à la recherche).
        """
        borne_min = cls._borne_date(date_from)
        borne_max = cls._borne_date(date_to, fin_de_journee=True)
        resultat = []
        for log in entrees:
            if status_filter and log.get("status") != status_filter:
                continue
            if search_ids is not None and log.get("search_id") not in search_ids:
                continue
            debut = log.get("started_at")
            if borne_min and (not isinstance(debut, datetime) or debut < borne_min):
                continue
            if borne_max and (not isinstance(debut, datetime) or debut > borne_max):
                continue
            resultat.append(log)
        resultat.sort(key=lambda log: log.get("started_at") or datetime.min, reverse=True)
        return resultat

    def get_all_scrape_logs(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        status_filter: str = "",
        search_ids: list[int] | None = None,
        date_from=None,
        date_to=None,
    ) -> list[dict]:
        """Page de scrape_logs toutes recherches confondues (#19), tri desc.

        Complément lecture seule de get_scrape_logs (qui reste par recherche) :
        mêmes entrées, mêmes statuts, pagination limit/offset identique.
        """
        entrees = self._filtrer_entrees(
            self._iterer_toutes_les_entrees(),
            status_filter=status_filter,
            search_ids=search_ids,
            date_from=date_from,
            date_to=date_to,
        )
        return entrees[offset: offset + limit]

    def count_all_scrape_logs(
        self,
        *,
        status_filter: str = "",
        search_ids: list[int] | None = None,
        date_from=None,
        date_to=None,
    ) -> int:
        """Compteur des mêmes entrées que get_all_scrape_logs, pour la pagination."""
        return len(
            self._filtrer_entrees(
                self._iterer_toutes_les_entrees(),
                status_filter=status_filter,
                search_ids=search_ids,
                date_from=date_from,
                date_to=date_to,
            )
        )

    def get_global_scrape_stats(self) -> dict:
        """Cartes de synthèse santé des scrapes (#19), calculées en UNE passe.

        - taux_succes_24h / taux_succes_7j : part des scrapes SANS ERREUR sur
          la fenêtre, en %. Un scrape « empty » n'est PAS un échec (il a tourné
          et n'a légitimement rien trouvé — cf. get_scrape_stats) ; il compte
          donc au numérateur. `None` quand la fenêtre ne contient aucun scrape :
          « pas de donnée » n'est pas « 0 % ».
        - duree_moyenne_7j : moyenne des duration_sec sur 7 jours (tous statuts).
        - top_erreurs : messages d'échec regroupés après troncature, avec leur
          nombre d'occurrences et la dernière survenue — triés par fréquence
          puis récence, plafonnés aux 5 premiers.

        Les timestamps stockés sont naïfs UTC (héritage create_scrape_log) : la
        référence « maintenant » est construite tz-aware puis rendue naïve UTC
        pour rester comparable, sans introduire un nouvel appel utcnow().
        """
        maintenant = datetime.now(UTC).replace(tzinfo=None)
        il_y_a_24h = maintenant - timedelta(hours=24)
        il_y_a_7j = maintenant - timedelta(days=7)
        nb_24h = nb_succes_24h = nb_7j = nb_succes_7j = 0
        duree_totale_7j = 0.0
        durees_7j = 0
        erreurs: dict[str, dict] = {}
        for log in self._iterer_toutes_les_entrees():
            debut = log.get("started_at")
            if not isinstance(debut, datetime):
                continue
            statut = log.get("status")
            dans_24h = debut >= il_y_a_24h
            if dans_24h or debut >= il_y_a_7j:
                nb_7j += 1
                if statut in ("success", "empty"):
                    nb_succes_7j += 1
                duree = log.get("duration_sec")
                if isinstance(duree, (int, float)):
                    duree_totale_7j += duree
                    durees_7j += 1
            if dans_24h:
                nb_24h += 1
                if statut in ("success", "empty"):
                    nb_succes_24h += 1
            if statut == "error":
                message = str(log.get("error_message") or "Erreur inconnue")[:_TAILLE_MESSAGE_ERREUR]
                groupe = erreurs.setdefault(message, {"occurrences": 0, "derniere": debut})
                groupe["occurrences"] += 1
                groupe["derniere"] = max(groupe["derniere"], debut)
        top_erreurs = sorted(
            (
                {"message": message, **groupe}
                for message, groupe in erreurs.items()
            ),
            # Fréquence décroissante, puis récence décroissante : à effectif
            # égal, l'erreur la plus récente passe devant.
            key=lambda e: (-e["occurrences"], -e["derniere"].timestamp()),
        )[:_TOP_ERREURS]
        return {
            "taux_succes_24h": round(nb_succes_24h * 100 / nb_24h, 1) if nb_24h else None,
            "nb_scrapes_24h": nb_24h,
            "taux_succes_7j": round(nb_succes_7j * 100 / nb_7j, 1) if nb_7j else None,
            "nb_scrapes_7j": nb_7j,
            "duree_moyenne_7j": round(duree_totale_7j / durees_7j, 1) if durees_7j else None,
            "top_erreurs": top_erreurs,
        }

    def export_scrape_logs(self, search_id: int) -> str:
        return export_search_logs(search_id)

    def import_scrape_logs(
        self, search_id: int, zip_path: str, allow_override: bool = False, performed_by: str = "",
    ) -> dict:
        return import_search_logs(search_id, zip_path, allow_override, performed_by)
