"""Tests unitaires de `scrape_logs/storage.py`.

Ce module est la base de données des logs de scrape : un JSONL par recherche,
un fichier de compteur pour les ids, et des fichiers de logs bruts. Il n'y a ni
transaction ni contrainte d'unicité — toutes les garanties sont dans ce code,
donc tout ce qui suit est du contrat, pas du détail d'implémentation :

  * l'allocation d'ids est protégée par un verrou **de process** (deux workers
    gunicorn produisent des ids dupliqués) ;
  * un compteur illisible repart à 0, ce qui réutilise des ids déjà pris ;
  * `find_entry_any` balaie TOUTES les recherches : un log_id est global, donc
    l'autorisation ne peut être portée que par l'appelant ;
  * `cleanup_old_logs` SUPPRIME des fichiers, et son cutoff mélange datetime
    naïf et epoch.

Remplace tests/_legacy/test_log_storage.py (13 tests, aucun sur la
concurrence, le compteur, les erreurs d'E/S ni le décalage de fuseau).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta

import pytest
from freezegun import freeze_time
from loguru import logger

import scrape_logs.storage as storage
from repositories.scrape_log_repo import ScrapeLogRepository
from tests.helpers.factories import make_scrape_log_entry

FROZEN = "2026-07-26 12:00:00"


# ---------------------------------------------------------------------------
# Outillage
# ---------------------------------------------------------------------------

@pytest.fixture
def log_messages():
    """Collecte les messages loguru émis pendant le test.

    `storage` ne lève presque jamais : il *avertit* et continue. Sans lire ces
    messages, un test ne distingue pas « ignoré proprement » de « perdu
    silencieusement ».
    """
    messages: list[str] = []
    sink_id = logger.add(lambda msg: messages.append(msg.record["message"]), level="DEBUG")
    yield messages
    logger.remove(sink_id)


def read_counter_file() -> dict:
    with open(storage.COUNTER_FILE, encoding="utf-8") as f:
        return json.load(f)


def raw_lines(search_id: int) -> list[str]:
    """Le JSONL brut, tel qu'il est sur le disque (sans passer par le parseur)."""
    with open(storage.get_metadata_path(search_id), encoding="utf-8") as f:
        return f.read().splitlines()


# ---------------------------------------------------------------------------
# allocate_log_id / compteur
# ---------------------------------------------------------------------------

class TestAllocateLogId:
    def test_ids_are_allocated_strictly_increasing_from_one(self):
        assert [storage.allocate_log_id() for _ in range(3)] == [1, 2, 3]
        assert read_counter_file() == {"last_id": 3}

    def test_the_counter_survives_a_process_restart(self):
        """Le compteur est persistant : c'est tout ce qui empêche un
        redémarrage de réattribuer des ids déjà utilisés."""
        storage.allocate_log_id()
        storage.allocate_log_id()

        # Simule un nouveau process : rien en mémoire, seul le fichier subsiste.
        assert storage._read_counter() == 2
        assert storage.allocate_log_id() == 3

    def test_concurrent_threads_never_get_the_same_id(self):
        """Garantie réelle : thread-safe. 32 threads, 32 ids distincts."""
        ids: list[int] = []
        ids_lock = threading.Lock()
        start = threading.Barrier(8)

        def allocate():
            start.wait(timeout=10)
            for _ in range(4):
                value = storage.allocate_log_id()
                with ids_lock:
                    ids.append(value)

        threads = [threading.Thread(target=allocate) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert sorted(ids) == list(range(1, 33)), "un id a été attribué deux fois"

    def test_allocation_is_not_process_safe_and_hands_out_duplicate_ids(self, monkeypatch):
        """# BUG : `_COUNTER_LOCK` est un verrou de process, pas de fichier.

        scrape_logs/storage.py:96-101 fait lire-incrémenter-écrire sous un
        `threading.Lock`. Deux workers gunicorn (le déploiement peut passer à
        plusieurs à tout moment, cf. le verrou consultatif de main.py) ont
        chacun leur propre verrou : rien ne sérialise leurs accès au fichier.

        Ce test émule ce cas en neutralisant le verrou — l'entrelacement obtenu
        est exactement celui de deux process — et fige le résultat : deux
        allocations simultanées rendent le MÊME id.

        L'écriture est sérialisée artificiellement : sans ça, les deux
        « process » se disputent le même fichier temporaire, ce qui est un
        second défaut, isolé dans le test suivant.
        """
        real_read = storage._read_counter
        real_write = storage._write_counter
        rendezvous = threading.Barrier(2)
        write_lock = threading.Lock()

        def read_then_wait_for_the_other_process():
            value = real_read()
            rendezvous.wait(timeout=10)  # l'autre « process » a lu la même valeur
            return value

        def serialized_write(value):
            with write_lock:
                real_write(value)

        class NoSharedLock:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(storage, "_COUNTER_LOCK", NoSharedLock())
        monkeypatch.setattr(storage, "_read_counter", read_then_wait_for_the_other_process)
        monkeypatch.setattr(storage, "_write_counter", serialized_write)

        allocated: list[int] = []
        results_lock = threading.Lock()

        def worker():
            value = storage.allocate_log_id()
            with results_lock:
                allocated.append(value)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert allocated == [1, 1], "deux process obtiennent le même id (comportement actuel)"
        assert read_counter_file() == {"last_id": 1}

    def test_two_processes_writing_the_counter_collide_on_a_shared_temp_file(self, monkeypatch):
        """# BUG : `_write_counter` écrit dans un `counter.json.tmp` FIXE.

        storage.py:88 dérive le chemin temporaire du seul nom du compteur : deux
        écrivains concurrents utilisent le même fichier. Le perdant de la course
        voit son fichier temporaire déjà renommé par l'autre et son
        `os.replace` lève `FileNotFoundError` — non rattrapée, elle remonte
        jusqu'à `create_scrape_log`, et le scrape entier est loggé en erreur
        alors que seul le compteur était en conflit.

        Observé pour de vrai en écrivant ce fichier : la version threadée du
        test précédent, sans sérialisation de l'écriture, échouait sur cette
        `FileNotFoundError`. Ici la course est rendue déterministe en simulant
        l'autre process qui renomme le fichier partagé le premier.

        Un `tempfile.mkstemp` dans le même répertoire (nom unique par process)
        supprimerait ce mode de panne — l'atomicité de `os.replace` étant, elle,
        parfaitement correcte.
        """
        real_replace = os.replace
        real_remove = os.remove

        def the_other_process_renamed_it_first(src, dst, *args, **kwargs):
            real_remove(src)
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", the_other_process_renamed_it_first)

        with pytest.raises(FileNotFoundError, match=r"counter\.json\.tmp"):
            storage.allocate_log_id()


class TestReadCounter:
    def test_missing_file_starts_at_zero(self):
        assert not os.path.exists(storage.COUNTER_FILE)
        assert storage._read_counter() == 0

    @pytest.mark.parametrize(
        ("payload", "case"),
        [
            ("pas du json", "JSON invalide"),
            ('{"last_id": "abc"}', "last_id non numérique (ValueError sur int())"),
            ("", "fichier vide"),
            ('{"autre": 3}', "clé last_id absente"),
        ],
        ids=["invalid_json", "non_numeric", "empty_file", "missing_key"],
    )
    def test_unreadable_counter_falls_back_to_zero(self, payload, case):
        os.makedirs(os.path.dirname(storage.COUNTER_FILE), exist_ok=True)
        with open(storage.COUNTER_FILE, "w", encoding="utf-8") as f:
            f.write(payload)

        assert storage._read_counter() == 0, case

    def test_an_unreadable_counter_is_a_directory_too(self):
        """`os.path.exists` est vrai pour un répertoire : l'ouverture lève une
        `IsADirectoryError` (une `OSError`), rattrapée comme les autres."""
        os.makedirs(storage.COUNTER_FILE)

        assert storage._read_counter() == 0

    def test_a_corrupted_counter_reuses_ids_and_destroys_an_existing_log(self):
        """# BUG : compteur illisible -> retour à 0 -> écrasement de logs.

        scrape_logs/storage.py:82-83 rattrape toute erreur de lecture du
        compteur en renvoyant 0. Les ids repartent donc de 1 alors que les logs
        1..N existent toujours : le fichier de log brut de l'ancien log est
        écrasé, et son entrée de métadonnées devient un doublon.

        Un `_read_counter` qui rendrait le plus grand id présent dans les JSONL
        (ou une erreur franche) éviterait la perte. Comportement actuel figé
        ici, avec la corruption qu'il produit.
        """
        for log_id in (1, 2, 3):
            assert storage.allocate_log_id() == log_id
            storage.append_entry(1, make_scrape_log_entry(id=log_id, status="success"))
            storage.write_raw_log(1, log_id, f"logs bruts du scrape {log_id}")

        with open(storage.COUNTER_FILE, "w", encoding="utf-8") as f:
            f.write("{tronqué")

        recycled = storage.allocate_log_id()
        assert recycled == 1, "le compteur est repassé à 0 : l'id 1 est réattribué"

        storage.append_entry(1, make_scrape_log_entry(id=recycled, status="error"))
        storage.write_raw_log(1, recycled, "logs bruts du NOUVEAU scrape")

        # Les logs bruts de l'ancien scrape 1 sont définitivement perdus.
        assert storage.read_raw_log(1, 1) == "logs bruts du NOUVEAU scrape"
        # Et deux entrées portent désormais l'id 1 : `find_entry` rend la
        # première, donc les métadonnées affichées ne correspondent plus au
        # contenu brut consultable.
        duplicated = [e for e in storage.read_entries(1) if e["id"] == 1]
        assert [e["status"] for e in duplicated] == ["success", "error"]
        assert storage.find_entry(1, 1)["status"] == "success"


class TestWriteCounter:
    def test_write_is_atomic_and_leaves_no_temporary_file(self):
        storage._write_counter(17)

        assert read_counter_file() == {"last_id": 17}
        assert not os.path.exists(f"{storage.COUNTER_FILE}.tmp"), (
            "le fichier temporaire doit être renommé, pas laissé sur le disque"
        )

    def test_creates_the_log_directory_if_needed(self, tmp_path, monkeypatch):
        target = tmp_path / "arborescence" / "absente"
        monkeypatch.setattr(storage, "SCRAPE_LOGS_DIR", str(target))
        monkeypatch.setattr(storage, "COUNTER_FILE", str(target / "counter.json"))

        storage._write_counter(1)

        assert read_counter_file() == {"last_id": 1}


# ---------------------------------------------------------------------------
# Sérialisation / lecture des entrées
# ---------------------------------------------------------------------------

class TestSerializeAndParse:
    @pytest.mark.parametrize(
        ("value", "case"),
        [
            (datetime(2026, 1, 1, 12, 0, 0), "naïf"),
            (datetime(2026, 1, 1, 12, 0, 0, 123456), "avec microsecondes"),
            (datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC), "aware UTC"),
        ],
        ids=["naive", "microseconds", "aware"],
    )
    def test_datetime_round_trips_through_iso(self, value, case):
        storage.append_entry(1, {"id": 1, "started_at": value, "completed_at": value})

        entry = storage.read_entries(1)[0]

        assert entry["started_at"] == value, case
        assert entry["completed_at"] == value, case

    def test_only_the_two_date_fields_are_converted(self):
        """Un champ qui *ressemble* à une date mais n'est pas `started_at` /
        `completed_at` reste une chaîne : la conversion est nominative."""
        storage.append_entry(1, {"id": 1, "found_at": "2026-01-01T12:00:00", "details": {"a": 1}})

        entry = storage.read_entries(1)[0]

        assert entry["found_at"] == "2026-01-01T12:00:00"
        assert entry["details"] == {"a": 1}

    @pytest.mark.parametrize(
        "bad_value",
        ["pas-une-date", "2026-13-45T99:00:00", "hier"],
        ids=["texte", "date_impossible", "mot"],
    )
    def test_an_unparsable_date_is_left_as_a_string(self, bad_value):
        """`_parse_entry` avale la `ValueError` et laisse la valeur brute : le
        type du champ dépend donc du contenu du fichier."""
        storage.append_entry(1, {"id": 1, "started_at": bad_value})

        assert storage.read_entries(1)[0]["started_at"] == bad_value

    def test_a_string_date_crashes_the_consumer_that_sorts_by_started_at(self):
        """# BUG : type non garanti + tri = TypeError chez le consommateur.

        `_parse_entry` (storage.py:70-71) laisse une date illisible sous forme
        de chaîne, et `ScrapeLogRepository.get_scrape_logs`
        (repositories/scrape_log_repo.py:51) trie sur
        `log.get("started_at") or datetime.min`. Une seule ligne corrompue —
        éditée à la main, tronquée par un disque plein, ou importée depuis une
        archive — rend donc la page de logs inaccessible (500), pour toutes les
        entrées de la recherche.

        Un `_parse_entry` qui écarterait la valeur illisible (ou une clé de tri
        défensive côté repo) suffirait. Comportement actuel figé ici.
        """
        storage.append_entry(1, make_scrape_log_entry(id=1))
        storage.append_entry(1, {"id": 2, "started_at": "corrompu", "status": "success"})

        entries = storage.read_entries(1)
        assert [type(e["started_at"]).__name__ for e in entries] == ["datetime", "str"]

        repo = ScrapeLogRepository("postgresql://fake/fake")
        with pytest.raises(TypeError, match="not supported between instances of"):
            repo.get_scrape_logs(1)

    def test_all_dates_unparsable_sorts_without_crashing(self):
        """Contrepoint : si TOUTES les dates sont des chaînes, la comparaison
        est homogène et le tri passe — le crash n'arrive qu'en cas de mélange,
        ce qui le rend dépendant des données et difficile à reproduire."""
        storage.append_entry(1, {"id": 1, "started_at": "aaa", "status": "success"})
        storage.append_entry(1, {"id": 2, "started_at": "bbb", "status": "error"})

        logs = ScrapeLogRepository("postgresql://fake/fake").get_scrape_logs(1)

        assert [log["id"] for log in logs] == [2, 1]


class TestReadEntries:
    def test_no_file_means_no_entries(self):
        assert storage.read_entries(999) == []

    def test_entries_keep_their_insertion_order(self):
        for log_id in (5, 3, 9):
            storage.append_entry(1, make_scrape_log_entry(id=log_id))

        assert [e["id"] for e in storage.read_entries(1)] == [5, 3, 9]

    @pytest.mark.parametrize(
        ("filler", "case"),
        [("\n", "ligne vide"), ("   \n", "ligne d'espaces"), ("\n\n\n", "lignes vides multiples")],
        ids=["empty", "blank", "multiple"],
    )
    def test_blank_lines_are_ignored(self, filler, case):
        storage.append_entry(1, make_scrape_log_entry(id=1))
        with open(storage.get_metadata_path(1), "a", encoding="utf-8") as f:
            f.write(filler)
        storage.append_entry(1, make_scrape_log_entry(id=2))

        assert [e["id"] for e in storage.read_entries(1)] == [1, 2], case

    def test_a_corrupted_line_is_skipped_with_a_warning(self, log_messages):
        storage.append_entry(1, make_scrape_log_entry(id=1))
        with open(storage.get_metadata_path(1), "a", encoding="utf-8") as f:
            f.write("{tronqué au milieu\n")
        storage.append_entry(1, make_scrape_log_entry(id=2))

        entries = storage.read_entries(1)

        assert [e["id"] for e in entries] == [1, 2], "une ligne illisible ne doit pas tout perdre"
        assert any("Ligne corrompue ignorée" in m for m in log_messages), (
            "la perte doit être tracée, pas silencieuse"
        )


class TestWriteEntries:
    def test_rewrite_replaces_the_whole_file_atomically(self):
        for log_id in (1, 2, 3):
            storage.append_entry(1, make_scrape_log_entry(id=log_id))

        storage.write_entries(1, [make_scrape_log_entry(id=2)])

        assert [e["id"] for e in storage.read_entries(1)] == [2]
        assert len(raw_lines(1)) == 1
        assert not os.path.exists(f"{storage.get_metadata_path(1)}.tmp")

    def test_writing_nothing_empties_the_file(self):
        storage.append_entry(1, make_scrape_log_entry(id=1))

        storage.write_entries(1, [])

        assert storage.read_entries(1) == []
        assert raw_lines(1) == []

    def test_a_generator_is_accepted(self):
        """La signature annonce un `Iterable` : un générateur ne doit pas être
        consommé deux fois ni ignoré."""
        storage.write_entries(1, (make_scrape_log_entry(id=i) for i in (7, 8)))

        assert [e["id"] for e in storage.read_entries(1)] == [7, 8]


# ---------------------------------------------------------------------------
# Recherche d'entrées
# ---------------------------------------------------------------------------

class TestFindEntry:
    def test_finds_the_entry_of_the_given_search(self):
        storage.append_entry(1, make_scrape_log_entry(id=5, status="success"))
        storage.append_entry(1, make_scrape_log_entry(id=6, status="error"))

        assert storage.find_entry(1, 6)["status"] == "error"

    @pytest.mark.parametrize(
        ("search_id", "log_id", "case"),
        [
            (1, 999, "id inconnu dans une recherche existante"),
            (42, 5, "recherche sans aucun log"),
        ],
        ids=["unknown_id", "unknown_search"],
    )
    def test_returns_none_when_there_is_no_match(self, search_id, log_id, case):
        storage.append_entry(1, make_scrape_log_entry(id=5))

        assert storage.find_entry(search_id, log_id) is None, case

    def test_find_entry_any_scans_every_search_directory(self):
        """Un log_id est GLOBAL : `find_entry_any` le retrouve sans savoir à
        quelle recherche il appartient. C'est pratique pour les routes admin —
        et c'est précisément pour ça que le contrôle « ce log appartient-il à
        cet utilisateur ? » doit être fait par l'appelant (voir
        ScrapeLogRepository.get_scrape_log_raw et son paramètre user_id)."""
        storage.append_entry(1, make_scrape_log_entry(id=1))
        storage.append_entry(7, make_scrape_log_entry(id=42, search_id=7))

        assert storage.find_entry_any(42)[0] == 7
        assert storage.find_entry_any(42)[1]["id"] == 42

    def test_find_entry_any_returns_none_when_the_root_is_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "SCRAPE_LOGS_DIR", str(tmp_path / "jamais_créé"))

        assert storage.find_entry_any(1) is None

    @pytest.mark.parametrize(
        "intruder",
        ["exports", "search_abc", "search_", "autre_chose"],
        ids=["exports_dir", "non_numeric_suffix", "empty_suffix", "unrelated"],
    )
    def test_directories_that_are_not_searches_are_skipped(self, intruder):
        """`exports/` cohabite avec les `search_<id>/`, et un répertoire au nom
        inattendu ne doit pas faire échouer le balayage."""
        storage.append_entry(3, make_scrape_log_entry(id=9, search_id=3))
        os.makedirs(os.path.join(storage.SCRAPE_LOGS_DIR, intruder), exist_ok=True)

        assert storage.find_entry_any(9)[0] == 3
        assert storage.find_entry_any(12345) is None


# ---------------------------------------------------------------------------
# Logs bruts
# ---------------------------------------------------------------------------

class TestRawLogs:
    def test_write_then_read_round_trip(self):
        assert storage.write_raw_log(1, 5, "contenu\nsur deux lignes") is True

        assert storage.read_raw_log(1, 5) == "contenu\nsur deux lignes"

    def test_missing_raw_log_reads_as_empty_string(self):
        assert storage.read_raw_log(1, 999) == ""

    def test_undecodable_characters_are_replaced_instead_of_raising(self):
        """`errors="replace"` à l'écriture : un log brut contenant un surrogate
        (ce que produit un décodage HTML bancal en amont) doit être écrit
        quand même — perdre le caractère est préférable à perdre le log."""
        assert storage.write_raw_log(1, 5, "avant \ud800 après") is True

        content = storage.read_raw_log(1, 5)
        assert content.startswith("avant ")
        assert content.endswith(" après")
        assert "\ud800" not in content

    def test_write_failure_returns_false_with_a_warning(self, log_messages):
        """Un répertoire là où le fichier devrait être : `open(..., "w")` lève
        une `IsADirectoryError`. L'appelant reçoit False, pas une exception."""
        os.makedirs(storage.get_raw_path(1, 5))

        assert storage.write_raw_log(1, 5, "peu importe") is False
        assert any("Échec écriture raw log" in m for m in log_messages)

    def test_read_failure_returns_empty_string(self):
        os.makedirs(storage.get_raw_path(1, 5))

        assert storage.read_raw_log(1, 5) == ""

    def test_invalid_bytes_on_disk_are_read_with_replacement_characters(self):
        os.makedirs(storage.get_raw_dir(1), exist_ok=True)
        with open(storage.get_raw_path(1, 5), "wb") as f:
            f.write(b"ok \xff\xfe fin")

        content = storage.read_raw_log(1, 5)

        assert content.startswith("ok ")
        assert content.endswith(" fin")
        assert "�" in content


# ---------------------------------------------------------------------------
# delete_search_logs
# ---------------------------------------------------------------------------

class TestDeleteSearchLogs:
    def test_removes_metadata_raw_logs_and_the_directory_itself(self):
        storage.append_entry(1, make_scrape_log_entry(id=1))
        storage.write_raw_log(1, 1, "contenu")
        search_dir = storage.get_search_dir(1)
        assert os.path.exists(search_dir)

        storage.delete_search_logs(1)

        assert not os.path.exists(search_dir)

    def test_leaves_the_other_searches_alone(self):
        storage.append_entry(1, make_scrape_log_entry(id=1))
        storage.append_entry(2, make_scrape_log_entry(id=2, search_id=2))

        storage.delete_search_logs(1)

        assert [e["id"] for e in storage.read_entries(2)] == [2]

    def test_missing_directory_is_a_silent_no_op(self):
        storage.delete_search_logs(999)  # ne doit pas lever

        assert storage.read_entries(999) == []

    def test_a_file_that_cannot_be_removed_leaves_a_partially_deleted_search(
        self, monkeypatch, log_messages
    ):
        """# BUG (borné) : suppression partielle, sans signal à l'appelant.

        `delete_search_logs` (storage.py:198-218) rattrape une `OSError` par
        fichier et ne renvoie rien : si un seul fichier résiste, la recherche
        est supprimée en base mais son répertoire de logs subsiste, à moitié
        vidé, et l'appelant croit la suppression complète. Seul un warning en
        témoigne.
        """
        storage.append_entry(1, make_scrape_log_entry(id=1))
        storage.write_raw_log(1, 1, "log brut verrouillé")
        real_remove = os.remove

        def refuse_the_raw_log(path, *args, **kwargs):
            if path.endswith("1.log"):
                raise PermissionError(13, "Permission denied")
            return real_remove(path, *args, **kwargs)

        monkeypatch.setattr(os, "remove", refuse_the_raw_log)

        storage.delete_search_logs(1)  # aucune exception, aucun retour

        assert os.path.exists(storage.get_raw_path(1, 1)), "le fichier récalcitrant reste"
        assert os.path.exists(storage.get_search_dir(1)), "le répertoire reste donc aussi"
        assert not os.path.exists(storage.get_metadata_path(1)), "le JSONL, lui, a bien été supprimé"
        assert any("Échec suppression" in m for m in log_messages)


# ---------------------------------------------------------------------------
# cleanup_old_logs
# ---------------------------------------------------------------------------

class TestCleanupOldLogs:
    @freeze_time(FROZEN)
    def test_returns_zero_when_the_root_directory_is_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "SCRAPE_LOGS_DIR", str(tmp_path / "absent"))

        assert storage.cleanup_old_logs() == 0

    @freeze_time(FROZEN)
    def test_keeps_recent_entries_and_deletes_the_old_ones(self):
        now = datetime.utcnow()
        storage.append_entry(1, make_scrape_log_entry(id=1, completed_at=now - timedelta(days=1)))
        storage.append_entry(1, make_scrape_log_entry(id=2, completed_at=now - timedelta(days=10)))
        storage.write_raw_log(1, 1, "récent")
        storage.write_raw_log(1, 2, "ancien")

        assert storage.cleanup_old_logs(retention_days=5) == 1

        assert [e["id"] for e in storage.read_entries(1)] == [1]
        assert storage.read_raw_log(1, 1) == "récent"
        assert storage.read_raw_log(1, 2) == "", "le log brut ancien a été supprimé"

    @freeze_time(FROZEN)
    def test_completed_at_wins_over_started_at(self):
        """L'âge se mesure sur la fin du scrape quand elle est connue : un
        scrape démarré il y a 10 jours et terminé hier reste consultable."""
        now = datetime.utcnow()
        storage.append_entry(
            1,
            {
                "id": 1,
                "started_at": now - timedelta(days=10),
                "completed_at": now - timedelta(days=1),
            },
        )

        assert storage.cleanup_old_logs(retention_days=5) == 0
        assert [e["id"] for e in storage.read_entries(1)] == [1]

    @freeze_time(FROZEN)
    def test_started_at_is_used_when_the_scrape_never_completed(self):
        storage.append_entry(1, {"id": 1, "started_at": datetime.utcnow() - timedelta(days=10)})
        storage.write_raw_log(1, 1, "scrape interrompu")

        assert storage.cleanup_old_logs(retention_days=5) == 1
        assert storage.read_entries(1) == []

    @pytest.mark.parametrize(
        ("entry", "case"),
        [
            ({"id": 1}, "aucune date"),
            ({"id": 1, "started_at": None, "completed_at": None}, "dates nulles"),
            ({"id": 1, "started_at": "date-illisible"}, "date restée en chaîne"),
        ],
        ids=["no_date", "null_dates", "unparsable_date"],
    )
    @freeze_time(FROZEN)
    def test_an_entry_without_a_usable_date_is_kept_forever(self, entry, case):
        """Sans date exploitable, `ts is None` et l'entrée est CONSERVÉE. Choix
        prudent (on ne supprime pas ce qu'on ne sait pas dater), mais qui rend
        ces entrées immortelles : elles s'accumulent."""
        storage.append_entry(1, entry)

        assert storage.cleanup_old_logs(retention_days=5) == 0, case
        assert len(storage.read_entries(1)) == 1, case

    @freeze_time(FROZEN)
    def test_the_jsonl_is_not_rewritten_when_nothing_expired(self, monkeypatch):
        """`if kept != entries` : pas de réécriture inutile du JSONL. Ce n'est
        pas cosmétique — `cleanup_old_logs` est appelé à CHAQUE scrape (via
        `SearchLogManager.cleanup_old_logs`), et réécrire le fichier à chaque
        fois multiplierait les fenêtres de corruption."""
        storage.append_entry(1, make_scrape_log_entry(id=1, completed_at=datetime.utcnow()))
        calls: list[int] = []
        real_write = storage.write_entries
        monkeypatch.setattr(
            storage,
            "write_entries",
            lambda sid, entries: (calls.append(sid), real_write(sid, entries))[0],
        )

        assert storage.cleanup_old_logs(retention_days=5) == 0
        assert calls == [], "aucune entrée expirée : le fichier ne doit pas être touché"

        storage.append_entry(1, make_scrape_log_entry(id=2, completed_at=datetime.utcnow() - timedelta(days=9)))
        storage.cleanup_old_logs(retention_days=5)
        assert calls == [1], "une entrée expirée : réécriture attendue"

    @freeze_time(FROZEN)
    def test_the_return_value_counts_deleted_files_not_expired_entries(self):
        """Trois entrées expirées, un seul log brut sur le disque -> renvoie 1.

        La valeur de retour est affichée comme « anciens fichiers supprimés » :
        elle ne mesure PAS le nombre d'entrées purgées. Un scrape ancien sans
        log brut (échec avant l'attachement, cf. ScrapeService) est purgé sans
        être compté.
        """
        old = datetime.utcnow() - timedelta(days=10)
        for log_id in (1, 2, 3):
            storage.append_entry(1, make_scrape_log_entry(id=log_id, completed_at=old))
        storage.write_raw_log(1, 2, "seul log brut présent")

        assert storage.cleanup_old_logs(retention_days=5) == 1
        assert storage.read_entries(1) == []

    @freeze_time(FROZEN)
    def test_an_entry_without_id_is_purged_without_touching_any_file(self):
        """`log_id is None` -> `continue` : l'entrée disparaît du JSONL mais
        aucun fichier n'est cherché (il n'y a pas de chemin à construire)."""
        storage.append_entry(1, {"status": "error", "completed_at": datetime.utcnow() - timedelta(days=10)})

        assert storage.cleanup_old_logs(retention_days=5) == 0
        assert storage.read_entries(1) == []

    @freeze_time(FROZEN)
    def test_every_search_directory_is_processed(self):
        old = datetime.utcnow() - timedelta(days=10)
        for search_id in (1, 2):
            storage.append_entry(search_id, make_scrape_log_entry(id=search_id, completed_at=old))
            storage.write_raw_log(search_id, search_id, "ancien")
        # Deux intrus : un nom au suffixe non numérique et le répertoire
        # `exports/`, qui cohabite avec les `search_<id>/`.
        os.makedirs(os.path.join(storage.SCRAPE_LOGS_DIR, "search_pas_un_id"), exist_ok=True)
        os.makedirs(storage.EXPORTS_DIR, exist_ok=True)

        assert storage.cleanup_old_logs(retention_days=5) == 2

        assert storage.read_entries(1) == []
        assert storage.read_entries(2) == []

    @freeze_time(FROZEN)
    def test_zero_retention_deletes_everything_dated(self):
        storage.append_entry(1, make_scrape_log_entry(id=1, completed_at=datetime.utcnow() - timedelta(seconds=1)))
        storage.write_raw_log(1, 1, "il y a une seconde")

        assert storage.cleanup_old_logs(retention_days=0) == 1
        assert storage.read_entries(1) == []


class TestCleanupTimezoneSkew:
    """Le cutoff de `cleanup_old_logs` dépend du fuseau de la MACHINE.

    storage.py:224 fait `datetime.utcnow().timestamp() - retention*86400`.
    `utcnow()` rend un datetime **naïf** dont les composantes sont de l'UTC,
    mais `.timestamp()` sur un naïf l'interprète comme une heure **locale** :
    l'epoch obtenu est décalé du décalage horaire de la machine.

    Pourquoi l'ancien test ne pouvait pas le voir : il datait ses entrées avec
    le même `datetime.utcnow()` naïf, dont le `.timestamp()` subissait le même
    décalage — les deux erreurs s'annulaient. Le décalage n'est visible que
    face à un datetime **aware** (une archive importée, ou une colonne
    TIMESTAMPTZ), dont le `.timestamp()` est, lui, exact.

    Le fuseau de la machine est ici émulé par `tz_offset` de freezegun, qui
    reproduit exactement la sémantique de `.timestamp()` sur un naïf.
    """

    @freeze_time(FROZEN, tz_offset=5.5)
    def test_east_of_utc_keeps_expired_logs_longer_than_retention(self):
        """# BUG : machine à UTC+5:30 -> cutoff avancé de 5 h 30.

        Une entrée aware vieille de 5 jours et 4 heures — donc au-delà des 5
        jours de rétention — est CONSERVÉE, parce que le cutoff a été calculé
        5 h 30 trop tôt.
        """
        expired = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)  # 5 j 4 h avant le gel
        storage.append_entry(1, make_scrape_log_entry(id=1, completed_at=expired))
        storage.write_raw_log(1, 1, "devrait être purgé")

        assert storage.cleanup_old_logs(retention_days=5) == 0
        assert [e["id"] for e in storage.read_entries(1)] == [1], (
            "l'entrée dépasse la rétention mais survit (comportement actuel, buggé)"
        )

    @freeze_time(FROZEN, tz_offset=-4)
    def test_west_of_utc_deletes_logs_that_are_still_within_retention(self):
        """# BUG, direction destructrice : machine à UTC-4 -> cutoff repoussé
        de 4 h, et une entrée aware vieille de seulement 4 j 22 h est
        SUPPRIMÉE avant la fin de sa rétention. C'est une perte de données
        provoquée par la seule configuration de fuseau de l'hôte.
        """
        still_valid = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)  # 4 j 22 h avant le gel
        storage.append_entry(1, make_scrape_log_entry(id=1, completed_at=still_valid))
        storage.write_raw_log(1, 1, "encore dans la rétention")

        assert storage.cleanup_old_logs(retention_days=5) == 1
        assert storage.read_entries(1) == [], (
            "l'entrée était encore dans la rétention mais a été purgée (comportement actuel, buggé)"
        )

    @freeze_time(FROZEN, tz_offset=5.5)
    def test_naive_entries_are_immune_because_both_shifts_cancel_out(self):
        """Contrepoint indispensable : les entrées écrites par le code de prod
        (`datetime.utcnow()`, naïf) ne sont PAS affectées, leur `.timestamp()`
        étant décalé exactement comme le cutoff. C'est ce qui masque le bug en
        production tant qu'aucune date aware ne circule.
        """
        naive_expired = datetime(2026, 7, 21, 8, 0)
        naive_valid = datetime(2026, 7, 21, 14, 0)
        storage.append_entry(1, make_scrape_log_entry(id=1, completed_at=naive_expired))
        storage.append_entry(1, make_scrape_log_entry(id=2, completed_at=naive_valid))
        storage.write_raw_log(1, 1, "expiré")
        storage.write_raw_log(1, 2, "valide")

        assert storage.cleanup_old_logs(retention_days=5) == 1
        assert [e["id"] for e in storage.read_entries(1)] == [2]
