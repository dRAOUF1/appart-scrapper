"""ScrapeLogRepository : un repo de FICHIERS dont l'autorisation est en SQL.

Les logs de scrape ne sont plus en base : ils vivent dans
`logs/scrape_logs/search_<id>/` (voir scrape_logs/storage.py, dont les
primitives sont testées ailleurs). Ce repo est la couche qui les agrège — et
elle a une particularité qui la place ici plutôt que dans tests/unit/ :

    🔒 `get_scrape_log_raw(log_id, user_id)` est le SEUL contrôle
    d'autorisation du module, et il est en SQL. Un `log_id` est GLOBAL
    (`find_entry_any` balaie TOUS les répertoires de recherche, sans se
    soucier du propriétaire) : la seule chose qui empêche l'utilisateur A de
    lire le log de l'utilisateur B est un
    `SELECT id FROM searches WHERE id = %s AND user_id = %s`. Le prouver
    demande de vraies lignes en base — voir `TestRawLogAuthorization`.

Le reste du cycle (création, tri, comptage, statistiques, export, import) est
exercé de bout en bout, y compris sur les chemins où il casse : deux défauts
sont figés ici avec un commentaire `# BUG :`.

La fixture autouse `log_dirs` de tests/conftest.py redirige toute
l'arborescence vers `tmp_path`, et `reset_global_state` vide le registre de
verrous : le compteur d'identifiants repart donc de 1 à chaque test.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta

import pytest

from scrape_logs.storage import append_entry, get_metadata_path, read_raw_log
from tests.integration.conftest import insert_search

# ---------------------------------------------------------------------------
# 🔒 Le contrôle d'autorisation, en base
# ---------------------------------------------------------------------------

class TestRawLogAuthorization:
    def test_the_owner_reads_the_log_with_its_raw_content(self, storage, search):
        log_id = storage.scrape_logs.create_scrape_log(search["id"], "success", 12, 3)
        storage.scrape_logs.update_scrape_log_raw(log_id, "ligne 1\nligne 2")

        entry = storage.scrape_logs.get_scrape_log_raw(log_id, storage.searches.get_search(
            search["id"])["user_id"])

        assert entry["id"] == log_id
        assert entry["search_id"] == search["id"]
        assert entry["status"] == "success"
        assert entry["raw_logs"] == "ligne 1\nligne 2"

    def test_another_user_cannot_read_it_even_with_the_right_log_id(
        self, storage, user, other_user, sql,
    ):
        """🔒 Le test central. Bob crée une recherche et un log ; Alice connaît
        l'identifiant (ils sont séquentiels et globaux, donc devinables : 1, 2,
        3...) et le demande. `find_entry_any` le TROUVE — il balaie tous les
        répertoires — mais la requête SQL ne rend aucune ligne, donc la méthode
        rend `None`.

        Sans ce `AND user_id = %s`, n'importe quel utilisateur authentifié lirait
        les logs brutes de tous les autres : URLs de recherche, codes postaux,
        volumes d'annonces, et le contenu complet des pages scrapées.
        """
        theirs = insert_search(storage, other_user["id"], "Chez bob")
        log_id = storage.scrape_logs.create_scrape_log(theirs["id"], "success", 5, 5)
        storage.scrape_logs.update_scrape_log_raw(log_id, "secrets de bob")

        assert storage.scrape_logs.get_scrape_log_raw(log_id, user["id"]) is None

        # Le fichier existe bel et bien, et le propriétaire y accède : le refus
        # vient du contrôle, pas d'une absence de données.
        assert storage.scrape_logs.get_scrape_log_raw(log_id, other_user["id"])["raw_logs"] == "secrets de bob"
        assert sql.one(
            "SELECT COUNT(*) FROM searches WHERE id = %s AND user_id = %s", (theirs["id"], user["id"]),
        ) == 0

    def test_a_user_id_of_none_bypasses_the_check_entirely(self, storage, other_user):
        """`user_id=None` est le chemin ADMIN : aucune requête n'est émise et le
        log est rendu quel qu'en soit le propriétaire. Ce n'est pas un défaut —
        c'est ce que la page d'administration utilise — mais ça veut dire que
        toute route qui oublie de passer `user_id` ouvre une IDOR sans que rien
        ne bronche."""
        theirs = insert_search(storage, other_user["id"], "Chez bob")
        log_id = storage.scrape_logs.create_scrape_log(theirs["id"], "success")

        assert storage.scrape_logs.get_scrape_log_raw(log_id)["id"] == log_id
        assert storage.scrape_logs.get_scrape_log_raw(log_id, None)["id"] == log_id

    def test_an_unknown_log_id_returns_none_without_querying(self, storage, user):
        assert storage.scrape_logs.get_scrape_log_raw(999_999, user["id"]) is None
        assert storage.scrape_logs.get_scrape_log_raw(999_999) is None

    def test_a_deleted_search_makes_its_logs_unreadable_by_anyone(self, storage, user, search):
        """`delete_search` supprime le répertoire de logs : le contrôle SQL ne
        peut plus rien trouver, et `find_entry_any` non plus. Les deux barrières
        tombent dans le bon sens."""
        log_id = storage.scrape_logs.create_scrape_log(search["id"], "success")
        assert storage.scrape_logs.get_scrape_log_raw(log_id, user["id"]) is not None

        storage.searches.delete_search(search["id"])

        assert storage.scrape_logs.get_scrape_log_raw(log_id, user["id"]) is None
        assert storage.scrape_logs.get_scrape_log_raw(log_id) is None

    def test_a_log_whose_search_row_vanished_is_refused_to_the_named_user(self, storage, user, sql):
        """Le cas asymétrique : le fichier survit à la disparition de la ligne
        (suppression en SQL brut, comme un `TRUNCATE searches` depuis l'admin).
        Le contrôle SQL refuse alors l'accès à l'utilisateur nommé — mais le
        chemin admin (`user_id=None`), qui ne consulte pas la base, continue de
        servir le contenu."""
        orphan = insert_search(storage, user["id"], "Bientôt supprimée")
        log_id = storage.scrape_logs.create_scrape_log(orphan["id"], "success")
        storage.scrape_logs.update_scrape_log_raw(log_id, "contenu")
        sql.exec("DELETE FROM searches WHERE id = %s", (orphan["id"],))

        assert storage.scrape_logs.get_scrape_log_raw(log_id, user["id"]) is None
        assert storage.scrape_logs.get_scrape_log_raw(log_id)["raw_logs"] == "contenu"

    def test_a_log_id_present_in_two_searches_is_resolved_nondeterministically(
        self, storage, user, other_user,
    ):
        """# BUG (fragilité) : `find_entry_any` rend la PREMIÈRE correspondance
        trouvée en parcourant `os.listdir`, dont l'ordre n'est pas spécifié. Si
        un même `log_id` existe sous deux recherches — ce que `allocate_log_id`
        évite normalement, mais qu'un import mal remappé ou une restauration
        partielle de sauvegarde peut produire — alors le contrôle d'autorisation
        porte sur la recherche que le système de fichiers a rendue en premier.

        Concrètement : l'accès d'Alice à « son » log peut échouer parce que
        l'entrée homonyme de Bob a été trouvée d'abord, et réciproquement. On
        n'affirme donc pas QUI gagne (ce serait un test instable), mais qu'un
        seul des deux passe — c'est-à-dire que l'autorisation dépend d'un détail
        de système de fichiers.
        """
        mine = insert_search(storage, user["id"], "À moi")
        theirs = insert_search(storage, other_user["id"], "À bob")
        collision = {"id": 7, "search_id": mine["id"], "status": "success",
                     "started_at": datetime(2026, 7, 1, 10, 0, 0)}
        append_entry(mine["id"], collision)
        append_entry(theirs["id"], {**collision, "search_id": theirs["id"]})

        readable = [
            storage.scrape_logs.get_scrape_log_raw(7, user["id"]) is not None,
            storage.scrape_logs.get_scrape_log_raw(7, other_user["id"]) is not None,
        ]

        assert sum(readable) == 1, "un seul des deux propriétaires légitimes y accède"


# ---------------------------------------------------------------------------
# Création
# ---------------------------------------------------------------------------

class TestCreateScrapeLog:
    def test_a_log_gets_a_sequential_id_and_a_raw_file_path(self, storage, search, log_dirs):
        first = storage.scrape_logs.create_scrape_log(search["id"], "success", 12, 3)
        second = storage.scrape_logs.create_scrape_log(search["id"], "empty")

        assert (first, second) == (1, 2)
        assert (log_dirs / "scrape_logs" / f"search_{search['id']}" / "metadata.jsonl").exists()
        assert storage.scrape_logs.get_scrape_logs(search["id"])[0]["raw_logs_file"] == "raw/2.log"

    def test_ids_are_global_and_not_per_search(self, storage, user):
        """Le compteur est un fichier unique pour toute l'installation : deux
        recherches ne partagent jamais un identifiant, ce qui est précisément ce
        qui rend `find_entry_any` possible — et ce qui rend le contrôle
        d'autorisation nécessaire."""
        first = insert_search(storage, user["id"], "Première")
        second = insert_search(storage, user["id"], "Seconde")

        ids = [
            storage.scrape_logs.create_scrape_log(first["id"], "success"),
            storage.scrape_logs.create_scrape_log(second["id"], "success"),
            storage.scrape_logs.create_scrape_log(first["id"], "success"),
        ]

        assert ids == [1, 2, 3]

    def test_every_field_is_persisted(self, storage, search):
        storage.scrape_logs.create_scrape_log(
            search["id"], "error", listings_found=8, new_listings=2,
            error_message="timeout", details={"pages": 3, "source": "seloger"},
        )

        (log,) = storage.scrape_logs.get_scrape_logs(search["id"])
        assert log["search_id"] == search["id"]
        assert log["status"] == "error"
        assert log["listings_found"] == 8
        assert log["new_listings"] == 2
        assert log["error_message"] == "timeout"
        assert log["details"] == {"pages": 3, "source": "seloger"}

    def test_the_defaults_produce_a_complete_entry(self, storage, search):
        storage.scrape_logs.create_scrape_log(search["id"], "success")

        (log,) = storage.scrape_logs.get_scrape_logs(search["id"])
        assert log["listings_found"] == 0
        assert log["new_listings"] == 0
        assert log["error_message"] == ""
        assert log["details"] == {}

    def test_the_duration_is_measured_only_when_a_start_is_given(self, storage, search):
        """# Piège : sans `started_at`, `duration_sec` vaut 0 — pas None. La
        durée moyenne des statistiques est donc tirée vers le bas par tout appel
        qui a oublié de passer l'heure de départ, sans qu'on puisse distinguer
        « instantané » de « non mesuré »."""
        storage.scrape_logs.create_scrape_log(search["id"], "success")
        storage.scrape_logs.create_scrape_log(
            search["id"], "success", started_at=datetime.utcnow() - timedelta(seconds=30),
        )

        durations = sorted(log["duration_sec"] for log in storage.scrape_logs.get_scrape_logs(search["id"]))
        assert durations[0] == 0
        assert durations[1] >= 30

    def test_the_timestamps_round_trip_through_iso_serialization(self, storage, search):
        """L'entrée est écrite en JSONL : les `datetime` sont sérialisés en ISO
        puis reparsés à la lecture. Un aller-retour qui rendrait des chaînes
        casserait le tri (comparaison str/datetime) et l'affichage."""
        started = datetime(2026, 7, 1, 10, 0, 0)
        storage.scrape_logs.create_scrape_log(search["id"], "success", started_at=started)

        (log,) = storage.scrape_logs.get_scrape_logs(search["id"])
        assert log["started_at"] == started
        assert isinstance(log["completed_at"], datetime)

    def test_a_log_can_be_created_for_a_search_id_that_does_not_exist(self, storage):
        """Aucune vérification en base : le stockage est un système de fichiers.
        C'est ce qui permet à un scrape de journaliser son échec même si la
        recherche vient d'être supprimée — mais aussi ce qui laisse des
        répertoires orphelins."""
        assert storage.scrape_logs.create_scrape_log(999_999, "error") == 1

        assert storage.scrape_logs.count_scrape_logs(999_999) == 1


# ---------------------------------------------------------------------------
# Lecture, tri, filtrage, pagination
# ---------------------------------------------------------------------------

@pytest.fixture
def log_history(storage, search):
    """Cinq entrées à des dates connues, écrites dans le désordre.

    Le tri est fait en PYTHON (les entrées sont des lignes JSONL, pas des
    lignes SQL) : l'ordre d'écriture doit donc être différent de l'ordre
    attendu, sinon le test passerait même sans tri.
    """
    for offset_days, status in [(1, "success"), (4, "error"), (0, "empty"), (3, "success"), (2, "error")]:
        storage.scrape_logs.create_scrape_log(
            search["id"], status,
            listings_found=offset_days * 10, new_listings=offset_days,
            started_at=datetime(2026, 7, 10) - timedelta(days=offset_days),
        )
    return search


class TestGetScrapeLogs:
    def test_logs_come_back_most_recent_first(self, storage, log_history):
        logs = storage.scrape_logs.get_scrape_logs(log_history["id"])

        assert [log["status"] for log in logs] == ["empty", "success", "error", "success", "error"]
        starts = [log["started_at"] for log in logs]
        assert starts == sorted(starts, reverse=True)

    @pytest.mark.parametrize(
        ("status_filter", "expected"),
        [
            pytest.param("", 5, id="aucun-filtre"),
            pytest.param("success", 2, id="success"),
            pytest.param("error", 2, id="error"),
            pytest.param("empty", 1, id="empty"),
            pytest.param("inconnu", 0, id="statut-inexistant"),
            pytest.param("SUCCESS", 0, id="sensible-a-la-casse"),
        ],
    )
    def test_the_status_filter_matches_the_count(self, storage, log_history, status_filter, expected):
        """Même invariant de pagination que côté SQL : `get_scrape_logs` et
        `count_scrape_logs` filtrent chacun de leur côté, à l'identique."""
        search_id = log_history["id"]

        logs = storage.scrape_logs.get_scrape_logs(search_id, limit=100, status_filter=status_filter)
        count = storage.scrape_logs.count_scrape_logs(search_id, status_filter=status_filter)

        assert count == len(logs) == expected

    def test_the_filter_is_applied_before_the_sort_and_the_slice(self, storage, log_history):
        logs = storage.scrape_logs.get_scrape_logs(log_history["id"], limit=1, status_filter="success")

        assert len(logs) == 1
        assert logs[0]["started_at"] == datetime(2026, 7, 9)

    def test_pagination_walks_every_entry_exactly_once(self, storage, log_history):
        search_id = log_history["id"]
        seen = []

        for offset in (0, 2, 4):
            seen.extend(log["id"] for log in storage.scrape_logs.get_scrape_logs(search_id, limit=2, offset=offset))

        assert len(seen) == len(set(seen)) == 5

    def test_an_offset_past_the_end_is_empty(self, storage, log_history):
        assert storage.scrape_logs.get_scrape_logs(log_history["id"], offset=100) == []
        assert storage.scrape_logs.count_scrape_logs(log_history["id"]) == 5

    def test_a_search_without_any_log_yields_an_empty_list(self, storage, search):
        assert storage.scrape_logs.get_scrape_logs(search["id"]) == []
        assert storage.scrape_logs.count_scrape_logs(search["id"]) == 0
        assert storage.scrape_logs.get_scrape_logs(999_999) == []

    def test_an_entry_without_a_start_date_sorts_last_instead_of_crashing(self, storage, search):
        """`log.get("started_at") or datetime.min` : une entrée sans date (écrite
        par une version antérieure, ou importée d'une archive incomplète) ne doit
        pas faire lever le `sort` sur une comparaison `None < datetime`. Elle est
        traitée comme infiniment ancienne."""
        storage.scrape_logs.create_scrape_log(search["id"], "success", started_at=datetime(2026, 7, 1))
        append_entry(search["id"], {"id": 99, "search_id": search["id"], "status": "error"})

        logs = storage.scrape_logs.get_scrape_logs(search["id"])

        assert [log["id"] for log in logs] == [1, 99]
        assert logs[1].get("started_at") is None

    def test_a_timezone_aware_entry_breaks_the_sort(self, storage, search):
        """# BUG : le tri compare les `started_at` entre eux. Toutes les entrées
        produites par `create_scrape_log` sont NAÏVES (`datetime.utcnow()`), mais
        `_parse_entry` accepte n'importe quel ISO 8601 — y compris avec fuseau.
        Une seule entrée avec offset (`...+02:00`) suffit alors à faire lever
        `TypeError: can't compare offset-naive and offset-aware datetimes`, et la
        page de logs de cette recherche devient inaccessible **définitivement** :
        l'entrée fautive reste dans le fichier.

        Le chemin d'entrée réel est `import_scrape_logs`, qui reparse les dates de
        l'archive sans les normaliser. Comportement ACTUEL figé.
        """
        storage.scrape_logs.create_scrape_log(search["id"], "success")
        with open(get_metadata_path(search["id"]), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": 99, "search_id": search["id"], "status": "success",
                "started_at": "2026-07-01T10:00:00+02:00",
            }) + "\n")

        with pytest.raises(TypeError, match="offset-naive and offset-aware"):
            storage.scrape_logs.get_scrape_logs(search["id"])

        # Le comptage, lui, ne trie pas : il continue de fonctionner, ce qui rend
        # le symptôme incohérent (« 2 exécutions » affiché, liste en erreur).
        assert storage.scrape_logs.count_scrape_logs(search["id"]) == 2

    def test_a_corrupt_line_is_skipped_and_the_rest_is_readable(self, storage, search):
        """Le fichier est du JSONL écrit en append : une écriture interrompue
        laisse une ligne tronquée. Elle doit être ignorée, pas faire perdre tout
        l'historique."""
        storage.scrape_logs.create_scrape_log(search["id"], "success")
        with open(get_metadata_path(search["id"]), "a", encoding="utf-8") as f:
            f.write('{"id": 2, "status": "err\n')
        storage.scrape_logs.create_scrape_log(search["id"], "empty")

        assert [log["id"] for log in storage.scrape_logs.get_scrape_logs(search["id"])] == [2, 1]


# ---------------------------------------------------------------------------
# Statistiques
# ---------------------------------------------------------------------------

class TestScrapeStats:
    def test_the_three_statuses_are_counted_separately(self, storage, log_history):
        """`empty` (« a tourné, n'a rien trouvé ») est compté à part de `error`
        (« quelque chose a cassé ») : sans cette distinction, une recherche dont
        le périmètre n'a simplement aucune annonce disponible s'afficherait comme
        en panne."""
        stats = storage.scrape_logs.get_scrape_stats(log_history["id"])

        assert stats["total"] == 5
        assert stats["success_count"] == 2
        assert stats["error_count"] == 2
        assert stats["empty_count"] == 1
        assert stats["success_count"] + stats["error_count"] + stats["empty_count"] == stats["total"]

    def test_the_averages_are_computed_over_every_entry(self, storage, log_history):
        stats = storage.scrape_logs.get_scrape_stats(log_history["id"])

        # listings_found = 10, 40, 0, 30, 20  /  new_listings = 1, 4, 0, 3, 2
        assert stats["avg_listings"] == 20
        assert stats["avg_new"] == 2

    def test_the_last_scrape_summarises_the_most_recent_entry(self, storage, search):
        storage.scrape_logs.create_scrape_log(
            search["id"], "success", started_at=datetime(2026, 7, 1),
        )
        storage.scrape_logs.create_scrape_log(
            search["id"], "error", error_message="boom", started_at=datetime(2026, 7, 5),
        )

        stats = storage.scrape_logs.get_scrape_stats(search["id"])

        assert stats["last_scrape"] == {
            "status": "error",
            "started_at": datetime(2026, 7, 5),
            "error_message": "boom",
        }

    def test_no_log_at_all_gives_zeros_and_no_last_scrape(self, storage, search):
        """🔒 Le garde-fou contre la division par zéro : `sum(...) / total if
        total else 0`. Sans lui, la page de détail d'une recherche jamais scrapée
        — c'est-à-dire toute recherche fraîchement créée — rendrait un 500."""
        stats = storage.scrape_logs.get_scrape_stats(search["id"])

        assert stats == {
            "total": 0, "success_count": 0, "error_count": 0, "empty_count": 0,
            "avg_listings": 0, "avg_new": 0, "avg_duration": 0, "last_scrape": None,
        }

    def test_missing_numeric_fields_count_as_zero_instead_of_raising(self, storage, search):
        """`log.get("listings_found") or 0` : une entrée ancienne ou importée peut
        ne pas porter le champ, ou le porter à `None`. La moyenne doit rester
        calculable."""
        storage.scrape_logs.create_scrape_log(search["id"], "success", listings_found=10, new_listings=4)
        append_entry(search["id"], {
            "id": 99, "search_id": search["id"], "status": "success",
            "started_at": datetime(2026, 7, 1), "listings_found": None,
        })

        stats = storage.scrape_logs.get_scrape_stats(search["id"])

        assert stats["total"] == 2
        assert stats["avg_listings"] == 5
        assert stats["avg_new"] == 2
        assert stats["avg_duration"] == 0

    def test_an_unknown_status_is_counted_in_the_total_only(self, storage, search):
        """Les trois compteurs ne couvrent pas forcément le total : un statut
        inattendu gonfle `total` sans apparaître ailleurs, et le taux de réussite
        affiché est donc faux plutôt qu'absent."""
        storage.scrape_logs.create_scrape_log(search["id"], "running")

        stats = storage.scrape_logs.get_scrape_stats(search["id"])

        assert stats["total"] == 1
        assert (stats["success_count"], stats["error_count"], stats["empty_count"]) == (0, 0, 0)


class TestLatestScrapeLogId:
    def test_it_returns_the_id_of_the_most_recent_entry(self, storage, search):
        storage.scrape_logs.create_scrape_log(search["id"], "success", started_at=datetime(2026, 7, 5))
        recent = storage.scrape_logs.create_scrape_log(
            search["id"], "success", started_at=datetime(2026, 7, 9),
        )
        storage.scrape_logs.create_scrape_log(search["id"], "success", started_at=datetime(2026, 7, 1))

        assert storage.scrape_logs.get_latest_scrape_log_id(search["id"]) == recent

    def test_the_most_recent_is_not_the_last_written(self, storage, log_history):
        """Le maximum est pris sur `started_at`, pas sur l'ordre d'écriture ni sur
        l'identifiant : l'entrée la plus récente du jeu d'essai est la 3ᵉ créée."""
        assert storage.scrape_logs.get_latest_scrape_log_id(log_history["id"]) == 3

    def test_without_any_log_it_returns_none(self, storage, search):
        assert storage.scrape_logs.get_latest_scrape_log_id(search["id"]) is None
        assert storage.scrape_logs.get_latest_scrape_log_id(999_999) is None


# ---------------------------------------------------------------------------
# Logs brutes
# ---------------------------------------------------------------------------

class TestUpdateScrapeLogRaw:
    def test_the_raw_content_is_written_next_to_the_metadata(self, storage, search, log_dirs):
        log_id = storage.scrape_logs.create_scrape_log(search["id"], "success")

        assert storage.scrape_logs.update_scrape_log_raw(log_id, "trace complète") is True

        raw_path = log_dirs / "scrape_logs" / f"search_{search['id']}" / "raw" / f"{log_id}.log"
        assert raw_path.read_text(encoding="utf-8") == "trace complète"
        assert read_raw_log(search["id"], log_id) == "trace complète"

    def test_the_search_is_resolved_from_the_log_id_alone(self, storage, user):
        """La signature ne prend pas de `search_id` : c'est `find_entry_any` qui
        le retrouve. C'est pratique (l'appelant n'a que l'identifiant de log) et
        c'est aussi la raison pour laquelle un `log_id` seul suffit à écrire dans
        le répertoire de n'importe quelle recherche — aucun contrôle de
        propriétaire sur ce chemin, contrairement à la lecture."""
        theirs = insert_search(storage, user["id"], "Ailleurs")
        log_id = storage.scrape_logs.create_scrape_log(theirs["id"], "success")

        assert storage.scrape_logs.update_scrape_log_raw(log_id, "écrit sans search_id") is True

        assert read_raw_log(theirs["id"], log_id) == "écrit sans search_id"

    def test_rewriting_replaces_the_previous_content(self, storage, search):
        log_id = storage.scrape_logs.create_scrape_log(search["id"], "success")
        storage.scrape_logs.update_scrape_log_raw(log_id, "premier essai")

        storage.scrape_logs.update_scrape_log_raw(log_id, "second essai")

        assert read_raw_log(search["id"], log_id) == "second essai"

    def test_an_unknown_log_id_returns_false(self, storage, search):
        storage.scrape_logs.create_scrape_log(search["id"], "success")

        assert storage.scrape_logs.update_scrape_log_raw(999_999, "nulle part") is False

    def test_a_log_without_raw_file_reads_back_as_an_empty_string(self, storage, user, search):
        """Le fichier brut n'est écrit que si le scrape appelle
        `update_scrape_log_raw` : la lecture doit rendre `""` et non lever, sinon
        la page de détail casse sur toute exécution interrompue avant la fin."""
        log_id = storage.scrape_logs.create_scrape_log(search["id"], "error")

        assert storage.scrape_logs.get_scrape_log_raw(log_id, user["id"])["raw_logs"] == ""


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------

class TestExport:
    def test_the_archive_carries_the_metadata_the_summary_and_the_raw_logs(self, storage, log_history):
        search_id = log_history["id"]
        storage.scrape_logs.update_scrape_log_raw(1, "trace de la 1")

        zip_path = storage.scrape_logs.export_scrape_logs(search_id)

        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            metadata = json.loads(zf.read("metadata.json"))
            summary = json.loads(zf.read("summary.json"))
            assert zf.read("raw_logs/1.log").decode("utf-8") == "trace de la 1"

        assert "metadata.json" in names and "summary.json" in names
        assert len(metadata) == 5
        assert {entry["id"] for entry in metadata} == {1, 2, 3, 4, 5}
        assert all(isinstance(entry["started_at"], str) for entry in metadata), "dates sérialisées en ISO"
        assert summary["search_id"] == search_id
        assert (summary["total"], summary["success_count"], summary["error_count"], summary["empty_count"]) == (
            5, 2, 2, 1,
        )
        assert summary["format_version"] == 1
        # Seule la 1 a un fichier brut : les autres ne sont pas des entrées vides
        # dans l'archive, elles sont absentes.
        assert {name for name in names if name.startswith("raw_logs/")} == {"raw_logs/1.log"}

    def test_exporting_a_search_without_logs_produces_a_valid_empty_archive(self, storage, search):
        zip_path = storage.scrape_logs.export_scrape_logs(search["id"])

        with zipfile.ZipFile(zip_path) as zf:
            assert json.loads(zf.read("metadata.json")) == []
            assert json.loads(zf.read("summary.json"))["total"] == 0
            assert json.loads(zf.read("summary.json"))["avg_listings"] == 0

    def test_the_archive_name_carries_the_search_id(self, storage, search):
        """Le nom inclut un horodatage à la SECONDE : deux exports de la même
        recherche dans la même seconde écrivent le même fichier, le second
        écrasant le premier. Sans conséquence fonctionnelle (le contenu est
        identique), épinglé pour mémoire."""
        zip_path = storage.scrape_logs.export_scrape_logs(search["id"])

        assert f"search_{search['id']}_" in zip_path
        assert zip_path.endswith(".zip")


class TestImport:
    def test_a_round_trip_into_a_wiped_search_restores_every_entry(self, storage, log_history, log_dirs):
        """Le cas de la restauration : on exporte, on efface le répertoire, on
        réimporte. Les identifiants d'origine sont réutilisés tels quels — plus
        rien ne les porte ailleurs, donc rien à remapper."""
        search_id = log_history["id"]
        storage.scrape_logs.update_scrape_log_raw(1, "trace de la 1")
        zip_path = storage.scrape_logs.export_scrape_logs(search_id)
        from scrape_logs.storage import delete_search_logs
        delete_search_logs(search_id)
        assert storage.scrape_logs.count_scrape_logs(search_id) == 0

        result = storage.scrape_logs.import_scrape_logs(search_id, zip_path)

        assert result == {"imported": 5, "skipped": 0, "remapped": 0}
        assert storage.scrape_logs.count_scrape_logs(search_id) == 5
        assert {log["id"] for log in storage.scrape_logs.get_scrape_logs(search_id)} == {1, 2, 3, 4, 5}
        assert read_raw_log(search_id, 1) == "trace de la 1"

    def test_reimporting_over_existing_entries_skips_them_all(self, storage, log_history):
        """L'import est idempotent : relancer la même archive ne duplique rien."""
        search_id = log_history["id"]
        zip_path = storage.scrape_logs.export_scrape_logs(search_id)

        result = storage.scrape_logs.import_scrape_logs(search_id, zip_path)

        assert result == {"imported": 0, "skipped": 5, "remapped": 0}
        assert storage.scrape_logs.count_scrape_logs(search_id) == 5

    def test_importing_into_another_search_requires_an_explicit_override(self, storage, user, log_history):
        """L'archive porte le `search_id` d'origine : l'importer ailleurs est
        probablement une erreur de manipulation, donc refusé par défaut avec un
        code que la route traduit en confirmation."""
        zip_path = storage.scrape_logs.export_scrape_logs(log_history["id"])
        target = insert_search(storage, user["id"], "Cible")

        with pytest.raises(ValueError, match="override_required"):
            storage.scrape_logs.import_scrape_logs(target["id"], zip_path)

        assert storage.scrape_logs.count_scrape_logs(target["id"]) == 0

    def test_with_the_override_the_ids_are_remapped_to_avoid_collisions(self, storage, user, log_history):
        """🔒 Le remappage est ce qui préserve l'unicité globale des `log_id` —
        c'est-à-dire ce qui empêche la collision documentée dans
        `TestRawLogAuthorization`. Les entrées existent déjà sous la recherche
        d'origine, donc chacune reçoit un identifiant tout neuf."""
        source_id = log_history["id"]
        storage.scrape_logs.update_scrape_log_raw(1, "trace de la 1")
        zip_path = storage.scrape_logs.export_scrape_logs(source_id)
        target = insert_search(storage, user["id"], "Cible")

        result = storage.scrape_logs.import_scrape_logs(target["id"], zip_path, allow_override=True)

        assert result == {"imported": 5, "skipped": 0, "remapped": 5}
        new_ids = {log["id"] for log in storage.scrape_logs.get_scrape_logs(target["id"])}
        assert new_ids == {6, 7, 8, 9, 10}
        # La source est intacte, et le contenu brut a suivi sous son nouvel
        # identifiant.
        assert {log["id"] for log in storage.scrape_logs.get_scrape_logs(source_id)} == {1, 2, 3, 4, 5}
        assert read_raw_log(target["id"], 6) == "trace de la 1"

    def test_the_imported_entries_are_reattached_to_the_target_search(self, storage, user, log_history):
        zip_path = storage.scrape_logs.export_scrape_logs(log_history["id"])
        target = insert_search(storage, user["id"], "Cible")

        storage.scrape_logs.import_scrape_logs(target["id"], zip_path, allow_override=True)

        logs = storage.scrape_logs.get_scrape_logs(target["id"])
        assert {log["search_id"] for log in logs} == {target["id"]}
        assert {log["raw_logs_file"] for log in logs} == {f"raw/{log['id']}.log" for log in logs}

    def test_the_imported_owner_can_read_the_raw_log_and_the_others_cannot(
        self, storage, user, other_user, log_history,
    ):
        """L'import déplace aussi la barrière d'autorisation : après remappage,
        c'est le propriétaire de la recherche CIBLE qui a accès, et lui seul."""
        storage.scrape_logs.update_scrape_log_raw(1, "trace de la 1")
        zip_path = storage.scrape_logs.export_scrape_logs(log_history["id"])
        target = insert_search(storage, other_user["id"], "Chez bob")

        storage.scrape_logs.import_scrape_logs(target["id"], zip_path, allow_override=True)

        new_id = min(log["id"] for log in storage.scrape_logs.get_scrape_logs(target["id"]))
        assert storage.scrape_logs.get_scrape_log_raw(new_id, other_user["id"])["raw_logs"] == "trace de la 1"
        assert storage.scrape_logs.get_scrape_log_raw(new_id, user["id"]) is None

    def test_the_import_is_audited_on_disk(self, storage, user, log_history, log_dirs):
        zip_path = storage.scrape_logs.export_scrape_logs(log_history["id"])
        target = insert_search(storage, user["id"], "Cible")

        storage.scrape_logs.import_scrape_logs(
            target["id"], zip_path, allow_override=True, performed_by="admin",
        )

        audit = (log_dirs / "scrape_logs" / "imports.log").read_text(encoding="utf-8")
        assert f"search_id={target['id']}" in audit
        assert "imported=5" in audit
        assert "remapped=5" in audit
        assert "override=True" in audit
        assert "by=admin" in audit

    def test_an_archive_without_metadata_is_refused(self, storage, search, tmp_path):
        bad_zip = tmp_path / "vide.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("autre.txt", "rien")

        with pytest.raises(ValueError, match="metadata.json manquant"):
            storage.scrape_logs.import_scrape_logs(search["id"], str(bad_zip))

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            pytest.param('{"pas": "une liste"}', "format attendu liste", id="objet-au-lieu-de-liste"),
            pytest.param("pas du json", "metadata.json invalide", id="json-invalide"),
        ],
    )
    def test_a_malformed_metadata_is_refused_with_a_message(self, storage, search, tmp_path, payload, message):
        bad_zip = tmp_path / "malforme.zip"
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("metadata.json", payload)

        with pytest.raises(ValueError, match=message):
            storage.scrape_logs.import_scrape_logs(search["id"], str(bad_zip))

        assert storage.scrape_logs.count_scrape_logs(search["id"]) == 0

    def test_an_empty_metadata_list_imports_nothing_without_requiring_an_override(
        self, storage, search, tmp_path,
    ):
        """`source_search_id` est lu sur la PREMIÈRE entrée : une liste vide n'en
        a aucune, donc le contrôle d'origine est court-circuité. Sans effet ici
        (rien à importer), mais c'est pourquoi le contrôle porte sur une seule
        entrée et non sur toutes."""
        empty_zip = tmp_path / "vide.zip"
        with zipfile.ZipFile(empty_zip, "w") as zf:
            zf.writestr("metadata.json", "[]")

        assert storage.scrape_logs.import_scrape_logs(search["id"], str(empty_zip)) == {
            "imported": 0, "skipped": 0, "remapped": 0,
        }

    def test_entries_without_an_id_or_of_the_wrong_type_are_ignored(self, storage, search, tmp_path):
        """Robustesse sur une archive bricolée à la main : les entrées
        inutilisables sont sautées sans compter ni lever."""
        mixed_zip = tmp_path / "mixte.zip"
        with zipfile.ZipFile(mixed_zip, "w") as zf:
            zf.writestr("metadata.json", json.dumps([
                {"id": 42, "search_id": search["id"], "status": "success",
                 "started_at": "2026-07-01T10:00:00"},
                {"search_id": search["id"], "status": "success"},   # sans id
                "pas un dict",
                None,
            ]))

        result = storage.scrape_logs.import_scrape_logs(search["id"], str(mixed_zip))

        assert result == {"imported": 1, "skipped": 0, "remapped": 0}
        assert [log["id"] for log in storage.scrape_logs.get_scrape_logs(search["id"])] == [42]
