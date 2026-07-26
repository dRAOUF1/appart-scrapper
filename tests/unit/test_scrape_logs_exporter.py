"""Tests unitaires de `scrape_logs/exporter.py`.

Ce module est le seul point d'entrée par lequel des données extérieures
(archives ZIP téléversées par un utilisateur) entrent dans le stockage des
logs. Trois routes l'exposent : `/api/searches/<id>/logs/import`,
`/admin/...` et la page web. D'où l'attention portée ici à :

  * la **chaîne magique `"override_required"`**, comparée par `==` dans les
    trois routes : c'est un contrat d'interface, pas un message ;
  * ce que le code NE vérifie PAS (taille décompressée, homogénéité de
    l'archive, échappement du journal d'audit) ;
  * la **parité** entre les agrégats de `summary.json` et ceux de
    `ScrapeLogRepository.get_scrape_stats`, dupliqués à l'identique dans deux
    modules.

Remplace tests/_legacy/test_log_exporter.py (7 tests, aucun sur le
dédoublonnage d'ids, la réallocation, l'audit ou la robustesse de l'archive).
"""

from __future__ import annotations

import json
import os
import zipfile
from datetime import UTC, datetime

import pytest
from freezegun import freeze_time
from loguru import logger

import scrape_logs.exporter as exporter
import scrape_logs.storage as storage
from repositories.scrape_log_repo import ScrapeLogRepository
from tests.helpers.factories import make_scrape_log_entry

FROZEN = "2026-07-26 14:30:05"


# ---------------------------------------------------------------------------
# Outillage
# ---------------------------------------------------------------------------

@pytest.fixture
def log_messages():
    """Messages loguru émis pendant le test (l'audit échoue en silence)."""
    messages: list[str] = []
    sink_id = logger.add(lambda msg: messages.append(msg.record["message"]), level="DEBUG")
    yield messages
    logger.remove(sink_id)


def make_archive(path, metadata, raw_logs=None, extra_files=None) -> str:
    """Une archive d'import fabriquée à la main.

    `metadata` peut être un objet JSON-sérialisable ou une chaîne brute (pour
    tester un JSON invalide). Passer `None` omet complètement metadata.json.
    """
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if metadata is not None:
            payload = metadata if isinstance(metadata, str) else json.dumps(metadata)
            zf.writestr("metadata.json", payload)
        for log_id, content in (raw_logs or {}).items():
            zf.writestr(f"raw_logs/{log_id}.log", content)
        for name, content in (extra_files or {}).items():
            zf.writestr(name, content)
    return str(path)


def zip_contents(zip_path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return {
            "names": set(zf.namelist()),
            "metadata": json.loads(zf.read("metadata.json")),
            "summary": json.loads(zf.read("summary.json")),
        }


def audit_lines() -> list[str]:
    if not os.path.exists(exporter.IMPORT_LOG_FILE):
        return []
    with open(exporter.IMPORT_LOG_FILE, encoding="utf-8") as f:
        return f.read().splitlines()


def seed_logs(search_id: int, entries: list[dict], raw: dict | None = None) -> None:
    for entry in entries:
        storage.append_entry(search_id, entry)
    for log_id, content in (raw or {}).items():
        storage.write_raw_log(search_id, log_id, content)


# ---------------------------------------------------------------------------
# export_search_logs
# ---------------------------------------------------------------------------

class TestExportNaming:
    @freeze_time(FROZEN)
    def test_the_archive_is_named_after_the_search_and_the_utc_second(self):
        path = exporter.export_search_logs(7)

        assert os.path.basename(path) == "search_7_20260726_143005.zip"
        assert os.path.dirname(path) == exporter.EXPORTS_DIR
        assert os.path.exists(path)

    @freeze_time(FROZEN)
    def test_two_exports_in_the_same_second_overwrite_each_other(self):
        """# BUG (borné) : la granularité du nom est la SECONDE.

        exporter.py:53-55 n'utilise que `%Y%m%d_%H%M%S` : deux exports de la
        même recherche dans la même seconde (deux clics, ou deux appels API)
        produisent le même chemin, et le second écrase silencieusement le
        premier — y compris si le premier est en cours de téléchargement.

        Sans conséquence grave (l'export est reproductible), mais le fichier
        rendu à l'utilisateur n'est pas forcément celui qu'il a demandé.
        """
        seed_logs(1, [make_scrape_log_entry(id=1, status="success")])
        first = exporter.export_search_logs(1)

        seed_logs(1, [make_scrape_log_entry(id=2, status="error")])
        second = exporter.export_search_logs(1)

        assert first == second
        assert len(os.listdir(exporter.EXPORTS_DIR)) == 1
        assert [e["id"] for e in zip_contents(second)["metadata"]] == [1, 2]

    @freeze_time(FROZEN)
    def test_the_exports_directory_is_created_on_demand(self):
        assert not os.path.exists(exporter.EXPORTS_DIR)

        exporter.export_search_logs(1)

        assert os.path.isdir(exporter.EXPORTS_DIR)


class TestExportContent:
    @freeze_time(FROZEN)
    def test_metadata_summary_and_existing_raw_logs_are_included(self):
        seed_logs(
            1,
            [make_scrape_log_entry(id=1, status="success"), make_scrape_log_entry(id=2, status="error")],
            raw={1: "logs bruts du scrape 1"},
        )

        content = zip_contents(exporter.export_search_logs(1))

        assert content["names"] == {"metadata.json", "summary.json", "raw_logs/1.log"}, (
            "le log brut absent du disque (id=2) ne doit pas créer d'entrée vide"
        )
        assert [e["id"] for e in content["metadata"]] == [1, 2]

    @freeze_time(FROZEN)
    def test_dates_are_serialized_as_iso_strings(self):
        started = datetime(2026, 7, 1, 10, 0, 0)
        seed_logs(1, [make_scrape_log_entry(id=1, started_at=started)])

        entry = zip_contents(exporter.export_search_logs(1))["metadata"][0]

        assert entry["started_at"] == "2026-07-01T10:00:00"
        assert entry["completed_at"] == "2026-07-01T10:00:30"

    @freeze_time(FROZEN)
    def test_an_entry_without_id_is_kept_in_metadata_but_has_no_raw_log(self):
        seed_logs(1, [{"status": "error", "error_message": "crash avant allocation"}])

        content = zip_contents(exporter.export_search_logs(1))

        assert content["names"] == {"metadata.json", "summary.json"}
        assert content["metadata"][0]["status"] == "error"

    @freeze_time(FROZEN)
    def test_summary_carries_the_format_version(self):
        """`format_version` est le seul point d'accroche pour faire évoluer le
        format sans casser les archives déjà exportées."""
        summary = zip_contents(exporter.export_search_logs(1))["summary"]

        assert summary["format_version"] == 1
        assert summary["exported_at"] == "2026-07-26T14:30:05"

    @freeze_time(FROZEN)
    def test_an_empty_search_exports_zeroed_aggregates(self):
        summary = zip_contents(exporter.export_search_logs(999))["summary"]

        assert summary["search_id"] == 999
        assert summary["total"] == 0
        assert (summary["avg_listings"], summary["avg_new"], summary["avg_duration"]) == (0, 0, 0)
        assert (summary["success_count"], summary["error_count"], summary["empty_count"]) == (0, 0, 0)


class TestSummaryParityWithScrapeStats:
    """`summary.json` et `ScrapeLogRepository.get_scrape_stats` calculent les
    MÊMES agrégats, dans deux modules différents (exporter.py:57-63 et
    repositories/scrape_log_repo.py:61-71). Ce test est le seul garde-fou
    contre leur divergence : sans lui, corriger une moyenne d'un côté
    seulement passerait au vert.
    """

    AGGREGATES = ("total", "success_count", "error_count", "empty_count", "avg_listings", "avg_new", "avg_duration")

    @pytest.mark.parametrize(
        ("entries", "case"),
        [
            (
                [
                    {"id": 1, "status": "success", "listings_found": 10, "new_listings": 4, "duration_sec": 12.5},
                    {"id": 2, "status": "error", "listings_found": 0, "new_listings": 0, "duration_sec": 1.0},
                    {"id": 3, "status": "empty", "listings_found": 0, "new_listings": 0, "duration_sec": 3.5},
                ],
                "un log de chaque statut",
            ),
            (
                [
                    {"id": 1, "status": "success", "listings_found": None, "new_listings": None, "duration_sec": None},
                    {"id": 2, "status": "success"},
                ],
                "champs nuls ou absents (colonnes nullables)",
            ),
            (
                [{"id": 1, "status": "inconnu", "listings_found": 3, "new_listings": 1, "duration_sec": 2}],
                "statut hors nomenclature : compté nulle part mais dans le total",
            ),
        ],
        ids=["mixed_statuses", "null_fields", "unknown_status"],
    )
    @freeze_time(FROZEN)
    def test_the_two_duplicated_computations_agree(self, entries, case):
        seed_logs(1, entries)

        summary = zip_contents(exporter.export_search_logs(1))["summary"]
        stats = ScrapeLogRepository("postgresql://fake/fake").get_scrape_stats(1)

        assert {k: summary[k] for k in self.AGGREGATES} == {k: stats[k] for k in self.AGGREGATES}, case

    @freeze_time(FROZEN)
    def test_averages_are_computed_over_all_logs_not_only_the_successful_ones(self):
        """Détail porteur de sens : les moyennes divisent par le nombre TOTAL
        de logs, échecs inclus. Une recherche qui échoue souvent affiche donc
        une moyenne d'annonces plus basse — c'est voulu, mais il faut le savoir
        pour lire les stats."""
        seed_logs(
            1,
            [
                {"id": 1, "status": "success", "listings_found": 10, "new_listings": 2, "duration_sec": 10},
                {"id": 2, "status": "error", "listings_found": 0, "new_listings": 0, "duration_sec": 0},
            ],
        )

        summary = zip_contents(exporter.export_search_logs(1))["summary"]

        assert summary["avg_listings"] == 5.0
        assert summary["avg_new"] == 1.0
        assert summary["avg_duration"] == 5.0

    @freeze_time(FROZEN)
    def test_last_scrape_is_only_in_the_repository_view(self):
        """`get_scrape_stats` expose `last_scrape`, `summary.json` non : la
        parité porte sur les agrégats, pas sur la forme complète."""
        seed_logs(1, [make_scrape_log_entry(id=1)])

        summary = zip_contents(exporter.export_search_logs(1))["summary"]
        stats = ScrapeLogRepository("postgresql://fake/fake").get_scrape_stats(1)

        assert "last_scrape" not in summary
        assert stats["last_scrape"]["status"] == "success"


# ---------------------------------------------------------------------------
# import_search_logs — validation de l'archive
# ---------------------------------------------------------------------------

class TestImportValidation:
    def test_missing_metadata_is_rejected(self, tmp_path):
        path = make_archive(tmp_path / "sans_metadata.zip", None, extra_files={"autre.json": "{}"})

        with pytest.raises(ValueError, match="Archive invalide: metadata.json manquant"):
            exporter.import_search_logs(1, path)

    @pytest.mark.parametrize(
        ("payload", "case"),
        [
            ("pas du json", "texte libre"),
            ("[{'id': 1}]", "quotes simples (Python, pas JSON)"),
            ("[{\"id\": 1},]", "virgule finale"),
            ("", "fichier vide"),
        ],
        ids=["plain_text", "python_repr", "trailing_comma", "empty"],
    )
    def test_unparsable_metadata_is_rejected(self, tmp_path, payload, case):
        path = make_archive(tmp_path / "casse.zip", payload)

        with pytest.raises(ValueError, match=r"metadata\.json invalide") as excinfo:
            exporter.import_search_logs(1, path)

        assert excinfo.value.__cause__ is not None, (
            f"la cause JSON d'origine doit être chaînée ({case})"
        )

    @pytest.mark.parametrize(
        ("payload", "case"),
        [
            ({"id": 1}, "un objet au lieu d'une liste"),
            ("42", "un nombre"),
            ('"texte"', "une chaîne"),
            ("null", "null"),
        ],
        ids=["object", "number", "string", "null"],
    )
    def test_metadata_that_is_not_a_list_is_rejected(self, tmp_path, payload, case):
        path = make_archive(tmp_path / "forme.zip", payload)

        with pytest.raises(ValueError, match="format attendu liste"):
            exporter.import_search_logs(1, path)

    def test_metadata_that_is_not_a_list_says_so_explicitly(self, tmp_path):
        """Message distinct de celui d'un JSON illisible : les deux causes ne
        se diagnostiquent pas pareil côté utilisateur."""
        path = make_archive(tmp_path / "forme.zip", {"id": 1})

        with pytest.raises(ValueError, match=r"^metadata\.json invalide: format attendu liste$"):
            exporter.import_search_logs(1, path)

    def test_a_validation_failure_imports_nothing_and_leaves_no_audit_trail(self, tmp_path):
        """Une archive refusée ne doit rien écrire — mais elle ne laisse pas de
        trace non plus : `_append_import_audit` est appelé APRÈS le bloc
        `with`, donc les tentatives refusées (mauvais format, override
        manquant) sont invisibles dans le journal d'audit. À garder en tête
        pour toute investigation : l'audit ne recense que les succès.
        """
        path = make_archive(tmp_path / "cassé.zip", {"pas": "une liste"})

        with pytest.raises(ValueError, match="format attendu liste"):
            exporter.import_search_logs(1, path, performed_by="attaquant")

        assert storage.read_entries(1) == []
        assert audit_lines() == []

    def test_an_empty_archive_is_a_valid_no_op(self, tmp_path):
        """Liste vide : pas de `source_search_id`, donc pas de contrôle
        d'override, et rien à importer."""
        path = make_archive(tmp_path / "vide.zip", [])

        assert exporter.import_search_logs(1, path) == {"imported": 0, "skipped": 0, "remapped": 0}
        assert audit_lines() != [], "un import vide reste un événement auditable"


# ---------------------------------------------------------------------------
# import_search_logs — le contrat "override_required"
# ---------------------------------------------------------------------------

class TestOverrideContract:
    """`"override_required"` est comparé par `==` dans les trois routes qui
    importent (routes/api.py:352, routes/admin.py:308, routes/web.py:608) pour
    répondre 409 et proposer la confirmation. Le message EST l'interface :
    l'enrichir d'un contexte casserait les trois appelants en silence.
    """

    def test_the_exception_message_is_exactly_the_magic_string(self, tmp_path):
        path = make_archive(tmp_path / "autre.zip", [{"id": 1, "search_id": 2, "status": "success"}])

        with pytest.raises(ValueError, match="^override_required$") as excinfo:
            exporter.import_search_logs(1, path)

        assert str(excinfo.value) == "override_required", (
            "trois routes comparent ce message avec ==, il ne doit ni être traduit ni enrichi"
        )

    def test_allow_override_lets_the_import_through_and_rewrites_the_search_id(self, tmp_path):
        path = make_archive(
            tmp_path / "autre.zip",
            [{"id": 1, "search_id": 2, "status": "success"}],
            raw_logs={1: "logs de la recherche 2"},
        )

        result = exporter.import_search_logs(1, path, allow_override=True)

        assert result == {"imported": 1, "skipped": 0, "remapped": 0}
        entry = storage.read_entries(1)[0]
        assert entry["search_id"] == 1, "l'entrée est réaffectée à la recherche cible"
        assert storage.read_raw_log(1, 1) == "logs de la recherche 2"

    @pytest.mark.parametrize(
        ("source_search_id", "case"),
        [
            (1, "même recherche"),
            (None, "search_id absent de l'entrée"),
        ],
        ids=["same_search", "no_source_search_id"],
    )
    def test_no_override_needed_when_there_is_no_mismatch(self, tmp_path, source_search_id, case):
        entry = {"id": 1, "status": "success"}
        if source_search_id is not None:
            entry["search_id"] = source_search_id
        path = make_archive(tmp_path / "ok.zip", [entry])

        assert exporter.import_search_logs(1, path)["imported"] == 1, case

    def test_only_the_first_entry_is_checked_so_a_mixed_archive_slips_through(self, tmp_path):
        """# BUG : le contrôle d'origine ne lit que `metadata[0]`.

        exporter.py:106-111 déduit le `search_id` source de la PREMIÈRE entrée
        seulement. Une archive dont la première entrée vise la bonne recherche
        et les suivantes une autre passe donc sans `allow_override` : les
        entrées étrangères sont importées en silence, leur `search_id` réécrit
        à la valeur cible.

        Conséquence concrète : l'utilisateur A peut injecter dans SA recherche
        les logs de la recherche de B (contenu brut compris) sans jamais voir
        de demande de confirmation. Le rempart des routes (409 + confirmation
        explicite) est contournable en réordonnant l'archive.
        """
        path = make_archive(
            tmp_path / "hétérogène.zip",
            [
                {"id": 1, "search_id": 1, "status": "success"},
                {"id": 2, "search_id": 999, "status": "error", "error_message": "log d'une autre recherche"},
            ],
        )

        result = exporter.import_search_logs(1, path)

        assert result["imported"] == 2, "aucune demande d'override n'a été déclenchée"
        assert [e["search_id"] for e in storage.read_entries(1)] == [1, 1]


# ---------------------------------------------------------------------------
# import_search_logs — dédoublonnage, réallocation, logs bruts
# ---------------------------------------------------------------------------

class TestImportMerge:
    def test_entries_already_present_are_skipped_not_duplicated(self, tmp_path):
        seed_logs(1, [{"id": 1, "search_id": 1, "status": "success"}])
        path = make_archive(
            tmp_path / "réimport.zip",
            [
                {"id": 1, "search_id": 1, "status": "success"},
                {"id": 2, "search_id": 1, "status": "error"},
            ],
        )

        result = exporter.import_search_logs(1, path)

        assert result == {"imported": 1, "skipped": 1, "remapped": 0}
        assert [e["id"] for e in storage.read_entries(1)] == [1, 2]

    def test_reimporting_the_same_archive_twice_is_idempotent(self, tmp_path):
        path = make_archive(tmp_path / "a.zip", [{"id": 1, "search_id": 1, "status": "success"}])

        first = exporter.import_search_logs(1, path)
        second = exporter.import_search_logs(1, path)

        assert (first["imported"], second["imported"]) == (1, 0)
        assert second["skipped"] == 1
        assert len(storage.read_entries(1)) == 1

    def test_an_id_owned_by_another_search_is_reallocated(self, tmp_path):
        """Un id est global : s'il est déjà pris par une AUTRE recherche,
        l'entrée importée reçoit un id neuf du compteur, et son log brut suit."""
        seed_logs(5, [{"id": 42, "search_id": 5, "status": "success"}])
        storage._write_counter(100)
        path = make_archive(
            tmp_path / "collision.zip",
            [{"id": 42, "search_id": 1, "status": "error"}],
            raw_logs={42: "logs bruts importés"},
        )

        result = exporter.import_search_logs(1, path)

        assert result == {"imported": 1, "skipped": 0, "remapped": 1}
        entry = storage.read_entries(1)[0]
        assert entry["id"] == 101, "l'id vient de allocate_log_id"
        assert entry["raw_logs_file"] == "raw/101.log"
        assert storage.read_raw_log(1, 101) == "logs bruts importés"
        # La recherche 5 n'a pas bougé.
        assert [e["id"] for e in storage.read_entries(5)] == [42]

    def test_raw_logs_file_is_rewritten_even_without_reallocation(self, tmp_path):
        path = make_archive(tmp_path / "a.zip", [{"id": 3, "search_id": 1, "raw_logs_file": "raw/999.log"}])

        exporter.import_search_logs(1, path)

        assert storage.read_entries(1)[0]["raw_logs_file"] == "raw/3.log", (
            "le pointeur du log brut doit être recalculé, pas repris de l'archive"
        )

    @pytest.mark.parametrize(
        ("entry", "case"),
        [
            ("pas un dict", "élément non-dict"),
            (42, "élément numérique"),
            ({"status": "success"}, "entrée sans id"),
            ({"id": None, "status": "success"}, "id nul"),
        ],
        ids=["string_item", "number_item", "no_id", "null_id"],
    )
    def test_unusable_entries_are_silently_ignored(self, tmp_path, entry, case):
        """L'entrée valide est placée en PREMIER : voir le test suivant pour ce
        qui arrive quand c'est l'entrée inutilisable qui ouvre la liste."""
        path = make_archive(tmp_path / "mixte.zip", [{"id": 1, "search_id": 1, "status": "success"}, entry])

        result = exporter.import_search_logs(1, path)

        assert result["imported"] == 1, case
        assert result["skipped"] == 0, "les entrées inutilisables ne sont comptées nulle part"
        assert [e["id"] for e in storage.read_entries(1)] == [1]

    @pytest.mark.parametrize(
        ("first", "case"),
        [("pas un dict", "chaîne"), (42, "nombre"), ([], "liste imbriquée"), (None, "null")],
        ids=["string", "number", "list", "null"],
    )
    def test_a_non_dict_first_entry_crashes_with_an_attribute_error(self, tmp_path, first, case):
        """# BUG : `metadata[0].get(...)` sans vérifier le type de l'élément.

        exporter.py:108 lit le `search_id` source sur `metadata[0]` en
        supposant un dict, alors que la boucle d'import, elle, se protège par
        `isinstance(entry, dict)` (ligne 120). Une archive dont la première
        entrée n'est pas un objet JSON lève donc une `AttributeError` au lieu
        du `ValueError("Archive invalide: ...")` prévu pour les archives
        malformées.

        Conséquence : les trois routes d'import ne rattrapent que `ValueError`
        (routes/api.py:352 et suivantes) — l'utilisateur reçoit un 500 avec une
        trace serveur, au lieu du 400 explicite qui existe déjà juste à côté.
        """
        path = make_archive(tmp_path / "premier.zip", [first, {"id": 1, "search_id": 1}])

        with pytest.raises(AttributeError, match="has no attribute 'get'"):
            exporter.import_search_logs(1, path)

        assert storage.read_entries(1) == [], f"rien n'est importé ({case})"
        assert audit_lines() == []

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-07-01T10:00:00", datetime(2026, 7, 1, 10, 0, 0)),
            ("2026-07-01T10:00:00+00:00", datetime(2026, 7, 1, 10, 0, 0, tzinfo=UTC)),
            ("pas-une-date", None),
            (None, None),
            (12345, None),
        ],
        ids=["naive_iso", "aware_iso", "unparsable", "null", "number"],
    )
    def test_dates_are_normalized_and_unparsable_ones_become_none(self, tmp_path, value, expected):
        """Contrairement à `storage._parse_entry`, l'importateur remplace une
        date illisible par `None` — ce qui rend l'entrée immortelle vis-à-vis
        de `cleanup_old_logs` (voir test_scrape_logs_storage.py) mais évite le
        mélange de types qui casse le tri."""
        path = make_archive(tmp_path / "dates.zip", [{"id": 1, "search_id": 1, "started_at": value}])

        exporter.import_search_logs(1, path)

        assert storage.read_entries(1)[0]["started_at"] == expected

    def test_a_missing_raw_log_becomes_an_empty_file(self, tmp_path):
        path = make_archive(tmp_path / "sans_raw.zip", [{"id": 1, "search_id": 1}])

        exporter.import_search_logs(1, path)

        assert storage.read_raw_log(1, 1) == ""
        assert os.path.exists(storage.get_raw_path(1, 1)), (
            "un fichier vide est créé quand même : la consultation ne plante pas"
        )

    def test_undecodable_raw_log_bytes_are_replaced_not_fatal(self, tmp_path):
        path = tmp_path / "binaire.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("metadata.json", json.dumps([{"id": 1, "search_id": 1}]))
            zf.writestr("raw_logs/1.log", b"debut \xff\xfe fin")

        exporter.import_search_logs(1, str(path))

        content = storage.read_raw_log(1, 1)
        assert content.startswith("debut ")
        assert content.endswith(" fin")

    def test_an_unreadable_zip_member_yields_an_empty_raw_log(self, tmp_path, monkeypatch):
        """Le `except Exception: raw_text = ""` couvre une archive dont un
        membre est corrompu (CRC invalide) : l'entrée est importée quand même,
        sans ses logs bruts."""
        path = make_archive(tmp_path / "crc.zip", [{"id": 1, "search_id": 1}], raw_logs={1: "contenu"})
        real_read = zipfile.ZipFile.read

        def fail_on_raw_logs(self, name, *args, **kwargs):
            if str(name).startswith("raw_logs/"):
                raise zipfile.BadZipFile("Bad CRC-32 for file 'raw_logs/1.log'")
            return real_read(self, name, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "read", fail_on_raw_logs)

        assert exporter.import_search_logs(1, path)["imported"] == 1
        assert storage.read_raw_log(1, 1) == ""

    def test_export_then_import_into_a_fresh_search_round_trips(self, tmp_path):
        """Le scénario réel de bout en bout : sauvegarde puis restauration."""
        with freeze_time(FROZEN):
            seed_logs(
                3,
                [
                    make_scrape_log_entry(id=1, search_id=3, status="success"),
                    make_scrape_log_entry(id=2, search_id=3, status="empty"),
                ],
                raw={1: "logs du scrape 1", 2: "logs du scrape 2"},
            )
            zip_path = exporter.export_search_logs(3)
            storage.delete_search_logs(3)

            result = exporter.import_search_logs(3, zip_path)

        assert result == {"imported": 2, "skipped": 0, "remapped": 0}
        assert [e["id"] for e in storage.read_entries(3)] == [1, 2]
        assert storage.read_raw_log(3, 2) == "logs du scrape 2"


class TestImportResourceLimits:
    def test_no_limit_on_the_decompressed_size(self, tmp_path):
        """# BUG : aucun contrôle de la taille DÉCOMPRESSÉE (zip bomb).

        Les trois routes plafonnent l'archive à 200 Mo (routes/api.py:337,
        routes/admin.py:289, routes/web.py:587) — mais sur sa taille
        *compressée*. `import_search_logs` lit ensuite chaque membre en
        mémoire (`zf.read(...)`, exporter.py:99 et 146) puis l'écrit sur le
        disque, sans jamais consulter `ZipInfo.file_size` ni borner le cumul.

        Un log brut de zéros a un ratio de compression d'environ 1000:1 : 200
        Mo autorisés à l'entrée valent ~200 Go écrits. Le test se contente de
        4 Mo pour rester rapide, et mesure le ratio réellement obtenu.

        Le point rassurant, à l'inverse : il n'y a **pas** de zip-slip
        possible. Le code ne fait aucun `extractall` et n'utilise jamais les
        noms de l'archive comme chemins de sortie — les chemins d'écriture
        sont recalculés par `storage.get_raw_path(search_id, new_id)`.
        """
        bomb_size = 4 * 1024 * 1024
        path = make_archive(
            tmp_path / "bombe.zip",
            [{"id": 1, "search_id": 1}],
            raw_logs={1: "0" * bomb_size},
        )
        compressed = os.path.getsize(path)

        assert exporter.import_search_logs(1, path)["imported"] == 1

        written = os.path.getsize(storage.get_raw_path(1, 1))
        assert written == bomb_size, "les 4 Mo décompressés sont écrits sans broncher"
        assert compressed * 100 < written, (
            f"ratio de compression obtenu : {written / compressed:.0f}:1 — "
            "le plafond de 200 Mo des routes ne borne donc rien du tout"
        )

    def test_a_path_traversal_name_in_the_archive_is_never_used_as_a_path(self, tmp_path):
        """Corollaire du point ci-dessus : un membre nommé
        `raw_logs/../../../etc/passwd.log` est simplement ignoré, puisque le
        code ne cherche que `raw_logs/{id}.log` et écrit à un chemin qu'il
        calcule lui-même."""
        path = make_archive(
            tmp_path / "slip.zip",
            [{"id": 1, "search_id": 1}],
            extra_files={"raw_logs/../../../evil.log": "charge utile"},
        )

        assert exporter.import_search_logs(1, path)["imported"] == 1

        assert not os.path.exists(tmp_path / "evil.log")
        assert storage.read_raw_log(1, 1) == ""


# ---------------------------------------------------------------------------
# _append_import_audit
# ---------------------------------------------------------------------------

class TestImportAudit:
    @freeze_time(FROZEN)
    def test_a_successful_import_is_recorded_with_its_counters(self, tmp_path):
        path = make_archive(tmp_path / "a.zip", [{"id": 1, "search_id": 1}])

        exporter.import_search_logs(1, path, performed_by="alice")

        assert audit_lines() == [
            "2026-07-26T14:30:05 | search_id=1 | imported=1 | skipped=0 | "
            "remapped=0 | override=False | by=alice"
        ]

    @freeze_time(FROZEN)
    def test_each_import_appends_a_line(self, tmp_path):
        path = make_archive(tmp_path / "a.zip", [{"id": 1, "search_id": 1}])

        exporter.import_search_logs(1, path, performed_by="alice")
        exporter.import_search_logs(1, path, performed_by="bob", allow_override=True)

        lines = audit_lines()
        assert len(lines) == 2
        assert "by=bob" in lines[1]
        assert "override=True" in lines[1]
        assert "skipped=1" in lines[1]

    @freeze_time(FROZEN)
    def test_an_unwritable_audit_file_does_not_fail_the_import(self, tmp_path, monkeypatch, log_messages):
        """L'audit est best-effort : un disque plein ne doit pas annuler un
        import déjà écrit. Revers de la médaille : un import peut réussir sans
        laisser de trace, avec pour seul témoin un warning."""
        os.makedirs(exporter.IMPORT_LOG_FILE)
        path = make_archive(tmp_path / "a.zip", [{"id": 1, "search_id": 1}])

        assert exporter.import_search_logs(1, path, performed_by="alice")["imported"] == 1

        assert any("Failed to write audit log" in m for m in log_messages)

    @pytest.mark.parametrize(
        ("performed_by", "case"),
        [
            (
                "attaquant | search_id=1 | imported=0 | skipped=0 | remapped=0 | override=False | by=admin",
                "faux champs sur la même ligne",
            ),
            (
                "attaquant\n2026-01-01T00:00:00 | search_id=1 | imported=9999 | skipped=0 | "
                "remapped=0 | override=True | by=admin",
                "faux enregistrement complet sur une nouvelle ligne",
            ),
        ],
        ids=["field_injection", "record_injection"],
    )
    @freeze_time(FROZEN)
    def test_the_username_is_not_escaped_and_can_forge_audit_records(
        self, tmp_path, performed_by, case
    ):
        """# BUG (sécurité) : `performed_by` est concaténé sans échappement.

        exporter.py:176-179 construit une ligne pipe-délimitée par
        f-string. `performed_by` est un *username*, choisi librement par
        l'utilisateur : `POST /api/users` crée un compte sans authentification
        et sans contrainte de caractères. Un username contenant `|` ou `\\n`
        injecte donc des champs — ou des enregistrements entiers — dans le
        journal d'audit des imports.

        Impact : le seul journal qui trace « qui a importé quoi dans quelle
        recherche » devient falsifiable a posteriori par n'importe qui, et
        l'attribution d'un import litigieux peut être déplacée sur `admin`.

        Correctifs possibles : journaliser en JSON Lines (`json.dumps` échappe
        `|` et `\\n`), ou refuser ces caractères dans les usernames.
        """
        path = make_archive(tmp_path / "a.zip", [{"id": 1, "search_id": 1}])

        exporter.import_search_logs(1, path, performed_by=performed_by)

        lines = audit_lines()
        forged = [line for line in lines if line.endswith("by=admin")]
        assert forged, f"injection non reproduite ({case})"
        assert len(lines) == 1 + performed_by.count("\n"), (
            "chaque saut de ligne du username crée un enregistrement supplémentaire"
        )
