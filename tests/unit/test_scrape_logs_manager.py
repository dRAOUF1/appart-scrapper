"""Tests unitaires de `scrape_logs/manager.py`.

`SearchLogManager` capture les logs d'un scrape dans un fichier par recherche.
Deux propriétés structurantes, toutes deux vérifiées ici :

  * le sink loguru posé par `start()` est **global au process** : il ne capture
    pas « les logs de ce scrape », il capture TOUT ce que le process journalise
    pendant qu'il est attaché (requêtes web comprises) ;
  * `read_tail(offset)` reçoit un offset **fourni par le client**
    (`/searches/<id>/logs/live?offset=...`, routes/web.py:485) et le passe
    directement à `file.seek()`.

Remplace tests/_legacy/test_log_manager.py (9 tests, aucun sur la portée du
sink, les offsets hostiles, ni la délégation à `storage.cleanup_old_logs`).
"""

from __future__ import annotations

import os
import time

import pytest
from freezegun import freeze_time
from loguru import logger

import scrape_logs.manager as manager
import scrape_logs.storage as storage
from scrape_logs.manager import SearchLogManager
from tests.helpers.factories import make_scrape_log_entry

FROZEN = "2026-07-26 12:00:00"


@pytest.fixture
def mgr():
    """Un manager pour la recherche 1 — `LOGS_DIR` est déjà dans tmp_path
    (fixture autouse `log_dirs`)."""
    return SearchLogManager(search_id=1)


def write_log_file(mgr_obj: SearchLogManager, content: str, binary: bytes | None = None) -> None:
    os.makedirs(manager.LOGS_DIR, exist_ok=True)
    mode, payload = ("wb", binary) if binary is not None else ("w", content)
    kwargs = {} if binary is not None else {"encoding": "utf-8"}
    with open(mgr_obj.log_file, mode, **kwargs) as f:
        f.write(payload)


# ---------------------------------------------------------------------------
# Chemin du fichier
# ---------------------------------------------------------------------------

class TestLogFilePath:
    @pytest.mark.parametrize("search_id", [1, 42, 999999], ids=["small", "typical", "large"])
    def test_the_file_is_named_after_the_search_id(self, search_id):
        mgr_obj = SearchLogManager(search_id=search_id)

        assert mgr_obj.log_file == os.path.join(manager.LOGS_DIR, f"search_{search_id}.log")

    def test_no_file_is_created_before_start(self, mgr):
        assert mgr.file_exists() is False
        assert mgr.read_all() == ""
        assert mgr.read_tail(0) == ("", 0)


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------

class TestStartStop:
    def test_start_creates_the_directory_and_captures_messages(self, mgr):
        mgr.start()
        logger.info("message pendant le scrape")
        content = mgr.stop()

        assert mgr.file_exists() is True
        assert "message pendant le scrape" in content
        assert "[LogManager] Capture démarrée pour search_id=1" in content

    def test_stop_detaches_the_sink_so_later_messages_are_not_captured(self, mgr):
        mgr.start()
        logger.info("pendant")
        mgr.stop()
        logger.info("après")

        content = mgr.read_all()
        assert "pendant" in content
        assert "après" not in content

    def test_stop_returns_the_whole_file_including_previous_runs(self, mgr):
        """`mode="a"` : le fichier de la recherche s'accumule d'un scrape sur
        l'autre. `stop()` renvoie donc TOUT le fichier, pas seulement le scrape
        qui vient de finir — c'est ce contenu complet qui est attaché comme
        « logs bruts » du dernier scrape par `ScrapeService`."""
        mgr.start()
        logger.info("premier scrape")
        mgr.stop()

        second = SearchLogManager(search_id=1)
        second.start()
        logger.info("second scrape")
        content = second.stop()

        assert "premier scrape" in content
        assert "second scrape" in content

    def test_stop_without_start_is_harmless(self, mgr):
        """`stop()` teste `if self._handler_id` : sans démarrage, il ne
        détache rien et rend une chaîne vide."""
        assert mgr.stop() == ""

    def test_stop_is_idempotent(self, mgr):
        """Appelé deux fois, `stop()` ne doit pas lever : `ScrapeService` peut
        passer par plusieurs chemins de sortie."""
        mgr.start()
        logger.info("contenu")
        first = mgr.stop()

        second = mgr.stop()

        assert "contenu" in first
        assert "contenu" in second
        assert mgr._handler_id is None

    def test_the_sink_is_global_and_captures_unrelated_process_activity(self, mgr):
        """Une requête web concurrente, un autre scrape, le scheduler : tout ce
        que le process journalise pendant la capture atterrit dans le fichier
        de CETTE recherche.

        Ce n'est pas un détail : les logs bruts consultables par l'utilisateur
        propriétaire de la recherche 1 contiennent donc des messages qui
        concernent d'autres utilisateurs (`logger.info` des routes, y compris
        des identifiants de recherche et des noms d'utilisateur). Comportement
        actuel figé ici — le corriger demande un `filter=` sur le sink, donc de
        propager un contexte (`logger.bind`) dans tout le pipeline.
        """
        mgr.start()
        logger.info("[Web] GET /searches/999 par bob")
        logger.warning("[Scheduler] recherche 42 planifiée")
        content = mgr.stop()

        assert "GET /searches/999 par bob" in content
        assert "recherche 42 planifiée" in content

    def test_two_managers_started_at_once_each_capture_everything(self):
        """Corollaire : deux scrapes simultanés (possible malgré le
        ThreadPoolExecutor à 1 worker, un scrape manuel pouvant être lancé
        pendant un scrape planifié) écrivent chacun l'intégralité des logs des
        deux."""
        first, second = SearchLogManager(search_id=1), SearchLogManager(search_id=2)
        first.start()
        second.start()
        logger.info("message unique")
        content_second = second.stop()
        content_first = first.stop()

        assert "message unique" in content_first
        assert "message unique" in content_second

    def test_debug_messages_are_captured_even_if_the_console_hides_them(self, mgr):
        """Le sink est posé à `level="DEBUG"` indépendamment du niveau
        configuré pour la console : les logs bruts sont plus détaillés que ce
        que l'exploitant voit passer, ce qui est tout l'intérêt."""
        mgr.start()
        logger.debug("détail utile au diagnostic")
        content = mgr.stop()

        assert "détail utile au diagnostic" in content
        assert "DEBUG" in content


# ---------------------------------------------------------------------------
# read_all / read_tail
# ---------------------------------------------------------------------------

class TestReadAll:
    def test_missing_file_reads_as_empty_string(self, mgr):
        assert mgr.read_all() == ""

    def test_invalid_bytes_are_replaced_rather_than_fatal(self, mgr):
        write_log_file(mgr, "", binary=b"debut \xff\xfe fin")

        content = mgr.read_all()

        assert content.startswith("debut ")
        assert content.endswith(" fin")


class TestReadTail:
    def test_only_the_new_content_is_returned_and_the_offset_advances(self, mgr):
        write_log_file(mgr, "ligne1\n")

        text1, offset1 = mgr.read_tail(0)
        assert (text1, offset1) == ("ligne1\n", 7)

        with open(mgr.log_file, "a", encoding="utf-8") as f:
            f.write("ligne2\n")

        text2, offset2 = mgr.read_tail(offset1)
        assert text2 == "ligne2\n"
        assert offset2 == 14

    def test_missing_file_returns_a_zero_offset(self, mgr):
        """Le client doit pouvoir interroger la route avant même que le scrape
        ait démarré : `("", 0)` le renvoie au début quand le fichier apparaît."""
        assert mgr.read_tail(0) == ("", 0)
        assert mgr.read_tail(500) == ("", 0)

    def test_an_offset_past_the_end_returns_nothing_and_echoes_the_offset(self, mgr):
        write_log_file(mgr, "court\n")

        assert mgr.read_tail(10_000) == ("", 10_000)

    def test_a_negative_offset_from_the_client_raises_valueerror(self, mgr):
        """# BUG : offset client non validé -> 500.

        routes/web.py:485 fait `to_int(request.args.get("offset", 0), 0)`, et
        `core.web_utils.to_int` accepte parfaitement les négatifs. `-1` arrive
        donc tel quel dans `f.seek(offset)`, qui lève
        `ValueError: negative seek position`. La route ne rattrape rien :
        `GET /searches/1/logs/live?offset=-1` rend un 500 avec une trace, sur
        une entrée entièrement contrôlée par le client.

        Un `max(offset, 0)` (ici ou dans la route) suffirait.
        """
        write_log_file(mgr, "contenu\n")

        with pytest.raises(ValueError, match="negative seek position"):
            mgr.read_tail(-1)

    def test_an_offset_inside_a_multibyte_character_does_not_raise(self, mgr):
        """L'hypothèse d'une `UnicodeDecodeError` est **infirmée** : le fichier
        est ouvert avec `errors="replace"`, donc un offset tombant au milieu
        d'un caractère UTF-8 produit un caractère de remplacement, pas une
        exception. Le suivi en direct affiche une bribe illisible, puis
        repart proprement.
        """
        write_log_file(mgr, "héllo wörld\n")  # 'é' occupe les octets 1 et 2

        text, offset = mgr.read_tail(2)

        assert text.startswith("�"), "octet orphelin remplacé, pas d'exception"
        assert text.endswith("llo wörld\n")
        assert offset == 14

    def test_the_returned_offset_is_a_byte_position_usable_as_the_next_input(self, mgr):
        """`f.tell()` en mode texte rend un « cookie » opaque, mais pour un
        flux UTF-8 lu jusqu'à la fin c'est bien la taille en OCTETS — et c'est
        ce que le client renvoie au coup d'après."""
        write_log_file(mgr, "déjà\n")  # 5 caractères, 7 octets

        _, offset = mgr.read_tail(0)

        assert offset == os.path.getsize(mgr.log_file) == 7


# ---------------------------------------------------------------------------
# cleanup_old_logs
# ---------------------------------------------------------------------------

class TestCleanupOldLogs:
    @staticmethod
    def age_file(path, days: float) -> None:
        stamp = time.time() - days * 86400
        os.utime(path, (stamp, stamp))

    @freeze_time(FROZEN)
    def test_files_older_than_retention_are_deleted_and_recent_ones_kept(self, monkeypatch):
        monkeypatch.setattr(manager, "cleanup_scrape_logs", lambda days: 0)
        os.makedirs(manager.LOGS_DIR, exist_ok=True)
        old = os.path.join(manager.LOGS_DIR, "search_100.log")
        recent = os.path.join(manager.LOGS_DIR, "search_200.log")
        for path in (old, recent):
            with open(path, "w", encoding="utf-8") as f:
                f.write("contenu")
        self.age_file(old, days=manager.RETENTION_DAYS + 1)
        self.age_file(recent, days=1)

        assert SearchLogManager.cleanup_old_logs() == 1

        assert not os.path.exists(old)
        assert os.path.exists(recent)

    @pytest.mark.parametrize(
        ("filename", "should_be_deleted"),
        [
            ("search_1.log", True),
            ("search_abc.log", True),
            ("search_.log", True),
            ("scrape_logs.log", False),
            ("search_1.log.gz", False),
            ("search_1.txt", False),
        ],
        ids=["numeric", "non_numeric", "empty_suffix", "other_prefix", "rotated_gz", "other_extension"],
    )
    @freeze_time(FROZEN)
    def test_only_search_star_dot_log_files_are_candidates(self, monkeypatch, filename, should_be_deleted):
        """Le glob est `search_*.log`. Les fichiers compressés par la rotation
        loguru (`search_1.log.gz`) échappent donc à ce nettoyage — c'est
        `retention="5 days"` du sink qui s'en charge, à condition qu'un sink
        soit attaché."""
        monkeypatch.setattr(manager, "cleanup_scrape_logs", lambda days: 0)
        os.makedirs(manager.LOGS_DIR, exist_ok=True)
        path = os.path.join(manager.LOGS_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        self.age_file(path, days=manager.RETENTION_DAYS + 1)

        deleted = SearchLogManager.cleanup_old_logs()

        assert deleted == (1 if should_be_deleted else 0)
        assert os.path.exists(path) is not should_be_deleted

    @freeze_time(FROZEN)
    def test_an_undeletable_file_is_skipped_without_being_counted(self, monkeypatch):
        """`except OSError: pass` : un fichier verrouillé n'interrompt pas la
        passe et n'est pas compté comme supprimé."""
        monkeypatch.setattr(manager, "cleanup_scrape_logs", lambda days: 0)
        os.makedirs(manager.LOGS_DIR, exist_ok=True)
        stubborn = os.path.join(manager.LOGS_DIR, "search_1.log")
        deletable = os.path.join(manager.LOGS_DIR, "search_2.log")
        for path in (stubborn, deletable):
            with open(path, "w", encoding="utf-8") as f:
                f.write("x")
            self.age_file(path, days=manager.RETENTION_DAYS + 1)

        real_remove = os.remove

        def refuse_the_first(path, *args, **kwargs):
            if path.endswith("search_1.log"):
                raise PermissionError(13, "Permission denied")
            return real_remove(path, *args, **kwargs)

        monkeypatch.setattr(os, "remove", refuse_the_first)

        assert SearchLogManager.cleanup_old_logs() == 1
        assert os.path.exists(stubborn)
        assert not os.path.exists(deletable)

    @freeze_time(FROZEN)
    def test_it_always_delegates_to_the_jsonl_cleanup_with_the_same_retention(self, monkeypatch):
        """Les deux stockages (fichiers `logs/search_*.log` et JSONL de
        `scrape_logs/`) sont purgés d'un seul appel, avec la MÊME rétention."""
        calls: list[int] = []
        monkeypatch.setattr(manager, "cleanup_scrape_logs", lambda days: calls.append(days) or 0)
        os.makedirs(manager.LOGS_DIR, exist_ok=True)

        assert SearchLogManager.cleanup_old_logs() == 0

        assert calls == [manager.RETENTION_DAYS] == [5]

    @freeze_time(FROZEN)
    def test_a_missing_logs_dir_also_skips_the_jsonl_cleanup(self, tmp_path, monkeypatch):
        """Le `return 0` anticipé court-circuite `cleanup_scrape_logs` : si
        `logs/` n'existe pas (premier démarrage, volume non monté), les
        métadonnées JSONL ne sont jamais purgées non plus. Couplage discret
        entre deux stockages indépendants."""
        calls: list[int] = []
        monkeypatch.setattr(manager, "cleanup_scrape_logs", lambda days: calls.append(days) or 0)
        monkeypatch.setattr(manager, "LOGS_DIR", str(tmp_path / "jamais_créé"))

        assert SearchLogManager.cleanup_old_logs() == 0

        assert calls == [], "le nettoyage des JSONL est sauté avec le reste"

    @freeze_time(FROZEN)
    def test_the_real_delegation_purges_the_jsonl_entries(self):
        """Sans double cette fois : la délégation purge réellement les entrées
        JSONL expirées (ici datées avec le même `utcnow()` naïf que le code de
        production, cf. le décalage de fuseau documenté dans
        tests/unit/test_scrape_logs_storage.py)."""
        from datetime import datetime, timedelta

        os.makedirs(manager.LOGS_DIR, exist_ok=True)
        storage.append_entry(
            1, make_scrape_log_entry(id=1, completed_at=datetime.utcnow() - timedelta(days=10))
        )
        storage.write_raw_log(1, 1, "ancien")

        assert SearchLogManager.cleanup_old_logs() == 0, "aucun fichier search_*.log à supprimer"
        assert storage.read_entries(1) == [], "mais le JSONL a bien été purgé"
